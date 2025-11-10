"""Aggregate control dashboard with streaming gRPC for reduced latency."""

from __future__ import annotations

import argparse
import csv
import gc
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple, cast

import grpc
import numpy as np
import yaml

try:
    import dearpygui.dearpygui as dpg  # type: ignore[import]
except ImportError as exc:
    raise RuntimeError("DearPyGui must be installed to run the aggregate UI") from exc


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_EXTRA_PATHS = [
    _REPO_ROOT / "Camera",
    _REPO_ROOT / "Arduino" / "rpc",
    _REPO_ROOT / "SLM" / "slm-control-server",
    _REPO_ROOT / "services",  # Add services for slm_config import
    _REPO_ROOT / "ExperimentScripts",  # Add experiment scripts
]
for _path in _EXTRA_PATHS:
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Deferred imports
from Camera.main_gui import (  # type: ignore  # noqa: E402
    AppState as ImageAppState,
    ImageClient,
    DEFAULT_DISPLAY_SCALE,
    DEFAULT_TEXTURE_SIZE,
    _resample_for_display,
)
from Arduino.rpc.grpc_client_streaming import DueStreamingClient  # type: ignore  # noqa: E402

if TYPE_CHECKING:
    import hologram_pb2 as holo_pb2  # type: ignore  # noqa: E402
    import hologram_pb2_grpc as holo_pb2_grpc  # type: ignore  # noqa: E402

try:
    import hologram_pb2 as holo_pb2  # type: ignore  # noqa: E402
    import hologram_pb2_grpc as holo_pb2_grpc  # type: ignore  # noqa: E402
except ModuleNotFoundError:
    import importlib
    _slm_control = _REPO_ROOT / "SLM" / "slm-control-server"
    if _slm_control.is_dir() and str(_slm_control) not in sys.path:
        sys.path.insert(0, str(_slm_control))
    holo_pb2 = cast(Any, importlib.import_module("hologram_pb2"))
    holo_pb2_grpc = cast(Any, importlib.import_module("hologram_pb2_grpc"))

HoloCommand = Any
HoloAck = Any
HoloStub = Any

# SLM Config imports
sys.path.insert(0, str(_REPO_ROOT / "SLM"))
from slm_config.slm_config_manager import SlmConfigManager, SlmConfig  # type: ignore  # noqa: E402
from slm_config.slm_feature_config_manager import (  # type: ignore  # noqa: E402
    SlmFeatureConfigManager,
    SlmFeatureConfig,
)
from slm_config.slm_finetuning_manager import (  # type: ignore  # noqa: E402
    FineTuningManager,
    FineTuningResult,
    FineTuningSample,
)

# Tracking Config imports
import sys
sys.path.insert(0, str(_REPO_ROOT / "Camera"))
from tracking_config.tracking_config_manager import TrackingConfigManager, TrackingConfig  # type: ignore  # noqa: E402

# Experiment Scripts imports
from base_script import ExperimentScript, ExperimentContext  # type: ignore  # noqa: E402
from script_manager import ScriptManager  # type: ignore  # noqa: E402
from experiment_params_manager import ExperimentParamsManager  # type: ignore  # noqa: E402


# Configuration Management
@dataclass
class DashboardConfig:
    """Dashboard UI configuration."""
    name: str
    description: str
    created_at: str
    viewport: Dict[str, Any]
    windows: Dict[str, Dict[str, Any]]
    docking_layout: Optional[str]  # DearPyGui ini file content for complete docking state
    image_display: Dict[str, Any]
    slm_visualization: Dict[str, Any]
    hardware_monitoring: Dict[str, Any]
    image_metrics: Dict[str, Any]
    storage: Dict[str, Any]
    monitoring: Dict[str, Any]
    experiment: Dict[str, Any]
    theme: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "viewport": self.viewport,
            "windows": self.windows,
            "docking_layout": self.docking_layout,
            "image_display": self.image_display,
            "slm_visualization": self.slm_visualization,
            "hardware_monitoring": self.hardware_monitoring,
            "image_metrics": self.image_metrics,
            "storage": self.storage,
            "monitoring": self.monitoring,
            "experiment": self.experiment,
            "theme": self.theme,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DashboardConfig":
        """Create from dictionary."""
        return DashboardConfig(
            name=data.get("name", "unnamed"),
            description=data.get("description", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            viewport=data.get("viewport", {}),
            windows=data.get("windows", {}),
            docking_layout=data.get("docking_layout", None),
            image_display=data.get("image_display", {}),
            slm_visualization=data.get("slm_visualization", {}),
            hardware_monitoring=data.get("hardware_monitoring", {}),
            image_metrics=data.get("image_metrics", {}),
            storage=data.get("storage", {}),
            monitoring=data.get("monitoring", {}),
            experiment=data.get("experiment", {}),
            theme=data.get("theme", {}),
        )


class DashboardConfigManager:
    """Manages dashboard UI configurations."""
    
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self._create_default_config()
        self._load_configs()
    
    def _create_default_config(self) -> None:
        """Create default configuration file."""
        default_config = {
            "active_config": "default",
            "configs": {
                "default": {
                    "name": "default",
                    "description": "Default dashboard configuration",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "viewport": {"width": 2560, "height": 1400, "title": "Tweezer Control & Monitoring"},
                    "windows": {},
                    "image_display": {
                        "display_mode": "overlay",
                        "show_tile_grid": False,
                        "zoom": 1.0,
                        "use_mass_colormap": True,
                        "mass_cutoff": 600.0,
                        "below_cutoff_color": [255, 255, 255],
                        "above_cutoff_color": [255, 0, 0],
                        "circle_scale": 1.0,
                    },
                    "slm_visualization": {
                        "circle_color": [255, 0, 0, 255],
                        "circle_radius": 15.0,
                        "circle_thickness": 2,
                    },
                    "hardware_monitoring": {
                        "history_limit": 1000,
                        "show_temperature": True,
                        "show_humidity": True,
                        "show_analog_channels": True,
                    },
                    "image_metrics": {
                        "history_limit": 1000,
                        "show_latency": True,
                        "show_processing": True,
                        "show_render": True,
                        "show_features": True,
                        "show_save": True,
                        "show_compression": True,
                    },
                    "storage": {
                        "auto_save_raw": False,
                        "auto_save_overlay": False,
                        "save_hdf5": False,
                        "target_fps": 30.0,
                    },
                    "monitoring": {
                        "interval_seconds": 1.0,
                        "auto_start": False,
                    },
                    "experiment": {
                        "move_time_min": 1.0,
                        "move_time_max": 3.0,
                        "distance_min": 50.0,
                        "distance_max": 200.0,
                        "delay": 0.5,
                        "separation": 100.0,
                        "edge_margin": 50.0,
                        "slm_refresh": 0.1,
                    },
                    "theme": {
                        "hardware_color": [220, 80, 80, 255],
                        "image_color": [80, 200, 90, 255],
                        "slm_color": [230, 180, 60, 255],
                        "status_connected": [80, 220, 90, 255],
                        "status_disconnected": [220, 60, 60, 255],
                        "text_primary": [230, 230, 230, 255],
                        "text_secondary": [180, 180, 180, 255],
                    },
                }
            }
        }
        with open(self.config_path, "w") as f:
            yaml.safe_dump(default_config, f, default_flow_style=False, sort_keys=False)
    
    def _load_configs(self) -> None:
        """Load all configurations from file."""
        try:
            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f)
                self.active_config_name = data.get("active_config", "default")
                self.configs = {
                    name: DashboardConfig.from_dict(cfg)
                    for name, cfg in data.get("configs", {}).items()
                }
        except Exception as exc:
            logging.error(f"Failed to load dashboard configs: {exc}")
            self.active_config_name = "default"
            self.configs = {}
    
    def _save_configs(self) -> None:
        """Save all configurations to file."""
        try:
            data = {
                "active_config": self.active_config_name,
                "configs": {name: cfg.to_dict() for name, cfg in self.configs.items()}
            }
            with open(self.config_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception as exc:
            logging.error(f"Failed to save dashboard configs: {exc}")
    
    def get_active_config(self) -> Optional[DashboardConfig]:
        """Get the currently active configuration."""
        return self.configs.get(self.active_config_name)
    
    def save_config(self, name: str, config: DashboardConfig) -> bool:
        """Save a configuration."""
        try:
            self.configs[name] = config
            self._save_configs()
            return True
        except Exception as exc:
            logging.error(f"Failed to save config '{name}': {exc}")
            return False
    
    def load_config(self, name: str) -> Optional[DashboardConfig]:
        """Load a configuration by name."""
        return self.configs.get(name)
    
    def set_active_config(self, name: str) -> bool:
        """Set the active configuration."""
        if name in self.configs:
            self.active_config_name = name
            self._save_configs()
            return True
        return False
    
    def delete_config(self, name: str) -> bool:
        """Delete a configuration (cannot delete 'default')."""
        if name == "default":
            logging.warning("Cannot delete default configuration")
            return False
        if name in self.configs:
            del self.configs[name]
            if self.active_config_name == name:
                self.active_config_name = "default"
            self._save_configs()
            return True
        return False
    
    def list_configs(self) -> List[str]:
        """List all configuration names."""
        return sorted(self.configs.keys())


@dataclass
class DacChannelSpec:
    name: str
    pin: int
    unit: str
    conversion: float
    min_value: Optional[float]
    max_value: Optional[float]
    label: str


@dataclass
class AnalogChannelSpec:
    name: str
    pin: int
    unit: str
    conversion: float
    label: str
    pair_graph_code: Optional[int] = None


@dataclass
class Shtc3Spec:
    name: str
    bus: int
    address: int
    frequency_khz: int
    unit: str = "DEGREE CELSIUS"
    label: str = ""


@dataclass
class SlmPoint:
    x: float
    y: float
    z: float
    intensity: float


@dataclass
class SlmFeatureState:
    apodization_enabled: bool
    apodization_strength: float
    z_focus_enabled: bool
    z_focus_offset: float
    z_focus_scale: float
    default_point_z: float


@dataclass
class FineTuningState:
    """State for active fine-tuning session."""
    active: bool = False
    paused: bool = False
    progress: float = 0.0  # 0.0 to 1.0
    current_sample: int = 0
    total_samples: int = 0
    margin_pixels: float = 50.0
    sample_area_min_x: float = 0.0
    sample_area_min_y: float = 0.0
    sample_area_max_x: float = 0.0
    sample_area_max_y: float = 0.0
    sample_points: List[Tuple[float, float]] = field(default_factory=list)  # Target SLM positions
    matched_particles: List[Tuple[float, float]] = field(default_factory=list)  # Detected positions
    errors: List[float] = field(default_factory=list)  # Distance errors
    current_target: Optional[Tuple[float, float]] = None  # Current pinning target
    current_match: Optional[Tuple[float, float]] = None  # Current matched particle
    animation_phase: float = 0.0  # For pulsing animations (0-1)
    base_config_name: str = ""
    show_visualization: bool = True
    base_affine_params: Dict[str, float] = field(default_factory=dict)
    current_track_history: List[Tuple[float, float]] = field(default_factory=list)
    manual_sample_points: List[Tuple[float, float]] = field(default_factory=list)
    manual_matched_particles: List[Tuple[float, float]] = field(default_factory=list)
    manual_errors: List[float] = field(default_factory=list)


@dataclass(frozen=True)
class EndpointConfig:
    host: str
    port: int

    def display(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class AggregateUI:
    """UI component references."""
    texture_registry: int
    texture_id: int
    texture_size: Tuple[int, int]  # Current texture dimensions (width, height)
    image_item: int
    cursor_label: int
    slm_points_label: int
    slm_ack_label: int
    due_status_label: int
    dac_items: Dict[str, Tuple[int, int]]  # name -> (value_label, increment_input)
    analog_labels: Dict[str, int]
    shtc3_labels: Dict[str, int]
    shtc3_display_labels: Dict[str, str]  # Display text for SHTC3 sensors ('temp' -> 'Ambient Temp')
    image_connect_button: int
    due_connect_button: int
    slm_connect_button: int
    slm_send_button: Optional[int] = None
    slm_clear_button: Optional[int] = None
    slm_affine_inputs: Dict[str, int] = None  # type: ignore  # param_name -> input_id
    slm_config_combo: Optional[int] = None  # SLM configuration dropdown
    slm_config_save_button: Optional[int] = None
    slm_config_load_button: Optional[int] = None
    slm_config_set_default_button: Optional[int] = None
    slm_config_reset_button: Optional[int] = None
    slm_config_delete_button: Optional[int] = None
    slm_points_list_group: Optional[int] = None  # Container for point list items
    slm_circle_color_picker: Optional[int] = None
    slm_circle_size_slider: Optional[int] = None
    slm_circle_thickness_slider: Optional[int] = None
    feature_apodization_checkbox: Optional[int] = None
    feature_apodization_strength_slider: Optional[int] = None
    feature_z_focus_checkbox: Optional[int] = None
    feature_z_focus_offset_slider: Optional[int] = None
    feature_z_focus_scale_slider: Optional[int] = None
    feature_default_point_z_slider: Optional[int] = None
    feature_config_combo: Optional[int] = None
    feature_config_save_button: Optional[int] = None
    feature_config_load_button: Optional[int] = None
    feature_config_set_default_button: Optional[int] = None
    feature_config_reset_button: Optional[int] = None
    feature_config_delete_button: Optional[int] = None
    image_connection_status_label: Optional[int] = None
    due_connection_status_label: Optional[int] = None
    slm_connection_status_label: Optional[int] = None
    image_target_label: Optional[int] = None
    due_target_label: Optional[int] = None
    slm_target_label: Optional[int] = None
    image_bit_depth_combo: Optional[int] = None  # Bit depth selection dropdown
    # Image server metrics
    image_sequence_text: Optional[int] = None
    image_latency_text: Optional[int] = None
    image_processing_text: Optional[int] = None
    image_detection_text: Optional[int] = None
    image_request_latency_text: Optional[int] = None
    image_render_latency_text: Optional[int] = None
    # Image metrics window text displays
    image_metrics_sequence_text: Optional[int] = None
    image_metrics_latency_text: Optional[int] = None
    image_metrics_processing_text: Optional[int] = None
    image_metrics_render_text: Optional[int] = None
    image_metrics_features_text: Optional[int] = None
    # Image display controls
    display_mode_combo: Optional[int] = None
    tile_grid_checkbox: Optional[int] = None
    zoom_slider: Optional[int] = None
    use_colormap_checkbox: Optional[int] = None
    mass_cutoff_input: Optional[int] = None
    below_color_picker: Optional[int] = None
    above_color_picker: Optional[int] = None
    circle_scale_slider: Optional[int] = None
    # Image saving controls
    auto_save_raw_checkbox: Optional[int] = None
    auto_save_overlay_checkbox: Optional[int] = None
    save_hdf5_checkbox: Optional[int] = None
    storage_target_fps_input: Optional[int] = None
    save_overlay_button: Optional[int] = None
    raw_dir_display: Optional[int] = None
    log_dir_display: Optional[int] = None
    overlay_dir_display: Optional[int] = None
    hdf5_path_display: Optional[int] = None
    storage_format_text: Optional[int] = None
    save_text: Optional[int] = None
    storage_ratio_text: Optional[int] = None
    storage_codec_text: Optional[int] = None
    storage_bytes_text: Optional[int] = None
    storage_throttle_text: Optional[int] = None
    storage_message_text: Optional[int] = None
    # Image tracking parameters
    tracking_params_path_text: Optional[int] = None
    tracking_apply_button: Optional[int] = None
    tracking_reset_button: Optional[int] = None
    tracking_inputs: Dict[str, int] = None  # type: ignore
    # Tracking configuration management
    tracking_config_combo: Optional[int] = None
    tracking_config_save_button: Optional[int] = None
    tracking_config_load_button: Optional[int] = None
    tracking_config_set_default_button: Optional[int] = None
    tracking_config_reset_button: Optional[int] = None
    tracking_config_delete_button: Optional[int] = None
    # SLM metrics
    slm_last_command_text: Optional[int] = None
    slm_generation_text: Optional[int] = None
    slm_roundtrip_text: Optional[int] = None
    # Plot series IDs and sliders - SHTC3 sensors (kept for backward compatibility)
    temp_series: Optional[int] = None
    humidity_series: Optional[int] = None
    temp_x_axis: Optional[int] = None
    temp_y_axis: Optional[int] = None
    humidity_x_axis: Optional[int] = None
    humidity_y_axis: Optional[int] = None
    # Dynamic analog plot storage
    # Maps channel name -> series_id for plots
    analog_plot_series: Dict[str, int] = None  # type: ignore
    # Maps pair_graph_code (or channel name for unpaired) -> (x_axis_id, y_axis_id)
    analog_plot_axes: Dict[str, Tuple[int, int]] = None  # type: ignore
    hardware_history_slider: Optional[int] = None
    img_latency_series: Optional[int] = None
    img_processing_series: Optional[int] = None
    img_render_series: Optional[int] = None
    img_features_series: Optional[int] = None
    img_save_series: Optional[int] = None
    img_compression_series: Optional[int] = None
    img_latency_x_axis: Optional[int] = None
    img_latency_y_axis: Optional[int] = None
    img_processing_x_axis: Optional[int] = None
    img_processing_y_axis: Optional[int] = None
    img_render_x_axis: Optional[int] = None
    img_render_y_axis: Optional[int] = None
    img_features_x_axis: Optional[int] = None
    img_features_y_axis: Optional[int] = None
    img_save_x_axis: Optional[int] = None
    img_save_y_axis: Optional[int] = None
    img_compression_x_axis: Optional[int] = None
    img_compression_y_axis: Optional[int] = None
    image_history_slider: Optional[int] = None
    # Monitoring controls
    monitoring_path_text: Optional[int] = None
    monitoring_interval_input: Optional[int] = None
    monitoring_start_button: Optional[int] = None
    # Experiment script controls
    experiment_script_combo: Optional[int] = None
    experiment_script_start_button: Optional[int] = None
    experiment_script_stop_button: Optional[int] = None
    experiment_script_pause_button: Optional[int] = None
    experiment_script_reload_button: Optional[int] = None
    experiment_script_status_text: Optional[int] = None
    experiment_script_info_text: Optional[int] = None
    # Experiment script parameters - Dynamic system
    experiment_params_container: Optional[int] = None
    """Container for dynamically generated parameter controls"""
    experiment_param_widgets: Dict[str, int] = field(default_factory=dict)
    """Maps parameter names to their widget IDs"""
    # Experiment parameters configuration management
    experiment_params_combo: Optional[int] = None
    experiment_params_save_button: Optional[int] = None
    experiment_params_load_button: Optional[int] = None
    experiment_params_set_default_button: Optional[int] = None
    experiment_params_delete_button: Optional[int] = None
    # Dashboard UI configuration management
    ui_config_combo: Optional[int] = None
    ui_config_save_button: Optional[int] = None
    ui_config_load_button: Optional[int] = None
    ui_config_set_default_button: Optional[int] = None
    ui_config_delete_button: Optional[int] = None
    # Fine-tuning controls
    finetuning_start_button: Optional[int] = None
    finetuning_stop_button: Optional[int] = None
    finetuning_pause_button: Optional[int] = None
    finetuning_progress_bar: Optional[int] = None
    finetuning_status_text: Optional[int] = None
    finetuning_margin_input: Optional[int] = None
    finetuning_samples_input: Optional[int] = None
    finetuning_visualization_checkbox: Optional[int] = None
    finetuning_config_combo: Optional[int] = None
    finetuning_config_delete_button: Optional[int] = None
    finetuning_metadata_text: Optional[int] = None
    manual_sample_count_text: Optional[int] = None
    manual_status_text: Optional[int] = None
    manual_capture_button: Optional[int] = None
    manual_apply_button: Optional[int] = None
    manual_clear_button: Optional[int] = None
    # Window tags for layout management
    connection_window: Optional[int] = None
    ui_config_window: Optional[int] = None
    monitoring_window: Optional[int] = None
    experiment_script_window: Optional[int] = None
    viewer_window: Optional[int] = None
    image_display_window: Optional[int] = None
    image_saving_window: Optional[int] = None
    env_window: Optional[int] = None
    hardware_window: Optional[int] = None
    image_metrics_window: Optional[int] = None
    slm_metrics_window: Optional[int] = None
    tracking_window: Optional[int] = None
    slm_window: Optional[int] = None


def _snake_to_label(name: str) -> str:
    """Convert snake_case to Title Case."""
    parts = [part for part in name.replace("-", "_").split("_") if part]
    return " ".join(part.capitalize() for part in parts) if parts else name


def _resolve_pin_identifier(identifier: str) -> int:
    """Resolve pin identifier to pin number."""
    ident = identifier.strip().upper()
    if ident.startswith("DAC"):
        suffix = ident[3:]
        if suffix == "0":
            return 66
        if suffix == "1":
            return 67
        raise ValueError(f"Unknown DAC pin identifier: {identifier!r}")
    if ident.startswith("A") and ident[1:].isdigit():
        return int(ident[1:])
    if ident.startswith("D") and ident[1:].isdigit():
        return int(ident[1:])
    if ident.isdigit():
        return int(ident)
    raise ValueError(f"Unsupported pin identifier: {identifier!r}")


def _load_pin_config(config_path: Path) -> Dict[str, Any]:
    """Load pin configuration from JSON."""
    if not config_path.exists():
        logging.warning("Pin config not found: %s", config_path)
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logging.exception("Failed to load pin config: %s", exc)
        return {}


def _build_channel_specs(config: Dict[str, Any]) -> Tuple[List[DacChannelSpec], List[AnalogChannelSpec], Dict[str, str]]:
    """Build channel specifications from configuration.
    
    Returns:
        Tuple of (dac_specs, analog_specs, shtc3_labels) where shtc3_labels is a dict
        mapping 'temp' and 'humidity' to their display labels.
    """
    dac_specs: List[DacChannelSpec] = []
    analog_specs: List[AnalogChannelSpec] = []
    shtc3_labels: Dict[str, str] = {}
    shtc3_spec: Optional[Shtc3Spec] = None
    
    for name, entry in config.items():
        if not isinstance(entry, dict):
            continue
        
        sensor_type = entry.get("sensor")
        if sensor_type and str(sensor_type).upper() == "SHTC3":
            # Use alias if available, otherwise convert name to label
            label = entry.get("alias", _snake_to_label(name))
            quantity = entry.get("quantity", "").lower()
            
            # Store label based on quantity type
            if quantity == "temperature":
                shtc3_labels["temp"] = label
            elif quantity == "humidity":
                shtc3_labels["humidity"] = label
            
            # Keep the first SHTC3 spec for connection purposes
            if shtc3_spec is None:
                shtc3_spec = Shtc3Spec(
                    name=name,
                    bus=entry.get("bus", 0),
                    address=entry.get("address", 0x70),
                    frequency_khz=entry.get("frequency_khz", 400),
                    unit=entry.get("unit", "DEGREE CELSIUS"),
                    label=label,
                )
            continue
        
        pin_label = entry.get("pin")
        if not pin_label:
            continue
        
        try:
            pin = _resolve_pin_identifier(pin_label)
        except ValueError:
            continue
        
        kind = entry.get("kind", "")
        unit = entry.get("unit", "V")
        conversion = float(entry.get("conversion", 1.0))
        # Use alias if available, otherwise convert name to label
        label = entry.get("alias", _snake_to_label(name))
        
        if kind == "dac_pin":
            dac_specs.append(DacChannelSpec(
                name=name,
                pin=pin,
                unit=unit,
                conversion=conversion,
                min_value=entry.get("min_value"),
                max_value=entry.get("max_value"),
                label=label,
            ))
        elif kind == "analog_read":
            analog_specs.append(AnalogChannelSpec(
                name=name,
                pin=pin,
                unit=unit,
                conversion=conversion,
                label=label,
                pair_graph_code=entry.get("pair_graph_code"),
            ))
    
    # Set default labels if not found in config
    if "temp" not in shtc3_labels:
        shtc3_labels["temp"] = "Temperature"
    if "humidity" not in shtc3_labels:
        shtc3_labels["humidity"] = "Humidity"
    
    return dac_specs, analog_specs, shtc3_labels


DEFAULT_SERVICES_CONFIG = Path(__file__).resolve().with_name("services_config.yaml")


def _load_services_config(config_path: Path) -> Dict[str, Any]:
    """Load the Tweezer services YAML configuration."""
    try:
        with config_path.open("r", encoding="utf-8") as cfg_file:
            data = yaml.safe_load(cfg_file) or {}
            if not isinstance(data, dict):
                logging.warning("Unexpected services config structure in %s", config_path)
                return {}
            return data
    except FileNotFoundError:
        logging.warning("Services config not found: %s", config_path)
    except Exception as exc:
        logging.exception("Failed to load services config %s: %s", config_path, exc)
    return {}


def _normalize_host(host: str) -> str:
    """Normalize host strings to avoid unusable values."""
    host = (host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _parse_bind(value: Optional[str], *, default_host: str, default_port: int) -> Tuple[str, int]:
    """Parse a host:port bind string."""
    if not value:
        return default_host, default_port
    parts = str(value).split(":", maxsplit=1)
    if len(parts) != 2:
        logging.warning("Invalid bind value %r; using defaults %s:%d", value, default_host, default_port)
        return default_host, default_port
    host = _normalize_host(parts[0]) or default_host
    try:
        port = int(parts[1])
    except ValueError:
        logging.warning("Invalid port in bind value %r; using default %d", value, default_port)
        port = default_port
    return host, port


def _load_dashboard_endpoints(config_path: Path) -> Dict[str, EndpointConfig]:
    """Extract connection endpoints for the dashboard from services YAML."""
    services_cfg = _load_services_config(config_path).get("services", {})

    image_defaults = EndpointConfig("127.0.0.1", 50053)
    image_args = services_cfg.get("image_server", {}).get("args", {}) if isinstance(services_cfg, dict) else {}
    image_host = _normalize_host(str(image_args.get("host", image_defaults.host))) if image_args else image_defaults.host
    image_port = image_args.get("port", image_defaults.port) if isinstance(image_args, dict) else image_defaults.port
    try:
        image_port = int(image_port)
    except (TypeError, ValueError):
        logging.warning("Invalid image server port %r; using default %d", image_port, image_defaults.port)
        image_port = image_defaults.port
    image_endpoint = EndpointConfig(image_host, image_port)

    due_defaults = EndpointConfig("127.0.0.1", 50052)
    due_args = services_cfg.get("arduino_grpc", {}).get("args", {}) if isinstance(services_cfg, dict) else {}
    due_host = _normalize_host(str(due_args.get("host", due_defaults.host))) if isinstance(due_args, dict) else due_defaults.host
    due_port = due_args.get("port", due_defaults.port) if isinstance(due_args, dict) else due_defaults.port
    try:
        due_port = int(due_port)
    except (TypeError, ValueError):
        logging.warning("Invalid Due server port %r; using default %d", due_port, due_defaults.port)
        due_port = due_defaults.port
    due_endpoint = EndpointConfig(due_host, due_port)

    slm_defaults = EndpointConfig("127.0.0.1", 50054)
    slm_args = services_cfg.get("slm_generator", {}).get("args", {}) if isinstance(services_cfg, dict) else {}
    if isinstance(slm_args, dict):
        slm_bind = slm_args.get("bind")
        slm_host, slm_port = _parse_bind(slm_bind, default_host=slm_defaults.host, default_port=slm_defaults.port)
    else:
        slm_host, slm_port = slm_defaults.host, slm_defaults.port
    slm_endpoint = EndpointConfig(slm_host, slm_port)

    return {
        "image": image_endpoint,
        "due": due_endpoint,
        "slm": slm_endpoint,
    }


class DueManagerStreaming:
    """Manager for streaming Arduino Due connection."""
    
    def __init__(self) -> None:
        self.client: Optional[DueStreamingClient] = None
        self.connected = False
        self.telemetry_data: Dict[str, float] = {}
        self.telemetry_lock = threading.Lock()

    def connect(self, target: str, *, shtc3_spec: Optional[Shtc3Spec]) -> None:
        """Connect to streaming server."""
        if self.connected:
            return
        
        self.client = DueStreamingClient(target)
        
        def telemetry_callback(timestamp: str, measurements: Dict[str, Any]) -> None:
            with self.telemetry_lock:
                self.telemetry_data.update(measurements)
        
        self.client.set_telemetry_callback(telemetry_callback)
        self.client.connect()
        
        # Initialize
        self.client.set_voltage_mode(True)
        self.client.set_vref(adc_vref=3.3, dac_vref=3.3)
        
        if shtc3_spec:
            try:
                self.client.i2c_begin(shtc3_spec.bus, shtc3_spec.frequency_khz)
                logging.info("Initialized I2C bus %d at %d kHz", shtc3_spec.bus, shtc3_spec.frequency_khz)
            except Exception as exc:
                logging.exception("Failed to initialize I2C: %s", exc)
        
        self.connected = True
        logging.info("Connected to Due streaming server at %s", target)

    def disconnect(self) -> None:
        """Disconnect from server."""
        if not self.connected:
            return
        
        if self.client:
            self.client.shutdown()
            self.client = None
        
        self.connected = False
        self.telemetry_data.clear()
        logging.info("Disconnected from Due streaming server")

    def shutdown(self) -> None:
        """Shutdown manager."""
        self.disconnect()

    def get_telemetry(self) -> Dict[str, float]:
        """Get latest telemetry data."""
        with self.telemetry_lock:
            return dict(self.telemetry_data)

    def write_dac(self, spec: DacChannelSpec, value: float) -> None:
        """Write DAC value."""
        if not self.connected or not self.client:
            raise RuntimeError("Not connected to Due")
        self.client.analog_write(spec.pin, value)


class SLMClient:
    """SLM client for streaming tweezer commands."""
    
    def __init__(self) -> None:
        self.channel: Optional[grpc.Channel] = None
        self.stub: Optional[HoloStub] = None
        self.connected = False
        self.target = ""
        self.last_ack: Optional[HoloAck] = None
        self._stream_call: Optional[Any] = None
        self._response_thread: Optional[threading.Thread] = None
        self._should_stop = False

    def connect(self, target: str) -> None:
        self.target = target
        self.channel = grpc.insecure_channel(target)
        self.stub = holo_pb2_grpc.ControlServiceStub(self.channel)
        self.connected = True
        self._should_stop = False
        
        # Start streaming connection
        if self.stub is not None:
            self._stream_call = self.stub.StreamCommands(self._command_iterator())
            self._response_thread = threading.Thread(target=self._consume_responses, daemon=True)
            self._response_thread.start()
        
        logging.info("Connected to SLM generator at %s", target)

    def disconnect(self) -> None:
        self._should_stop = True
        if self._response_thread:
            self._response_thread.join(timeout=1.0)
        if self.channel:
            self.channel.close()
        self.connected = False
        self._stream_call = None
        logging.info("Disconnected from SLM generator")

    def shutdown(self) -> None:
        self.disconnect()

    def _command_iterator(self) -> Any:
        """Generator that yields commands from the queue."""
        import queue
        self._command_queue: queue.Queue = queue.Queue()
        while not self._should_stop:
            try:
                cmd = self._command_queue.get(timeout=0.1)
                if cmd is None:
                    break
                yield cmd
            except queue.Empty:
                continue

    def _consume_responses(self) -> None:
        """Consume acknowledgement responses from the stream."""
        try:
            if self._stream_call is None:
                return
            for ack in self._stream_call:
                self.last_ack = ack
                logging.debug("SLM ack: stage=%s command_id=%s", ack.stage, ack.command_id)
        except Exception as exc:
            logging.exception("Error consuming SLM responses: %s", exc)

    def send_command(
        self,
        points: List[SlmPoint],
        affine_params: Optional[Dict[str, float]] = None,
        feature_state: Optional[SlmFeatureState] = None,
    ) -> None:
        """Send a tweezer command with points and optional affine parameters."""
        if not self.connected or self._stream_call is None:
            logging.warning("Cannot send command: not connected to SLM")
            return
        
        import uuid
        from google.protobuf import timestamp_pb2
        
        command_id = uuid.uuid4().hex
        command = holo_pb2.TweezerCommand(command_id=command_id)
        
        # Set timestamp
        now = datetime.now(timezone.utc)
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(now)
        command.requested_at.CopyFrom(ts)
        
        # Add points
        for pt in points:
            point = command.points.add()
            point.x = pt.x
            point.y = pt.y
            point.z = pt.z
            point.intensity = pt.intensity

        if feature_state is not None:
            apod_strength = max(min(feature_state.apodization_strength, 1.0), 0.0)
            ctrl_apod = command.points.add()
            ctrl_apod.x = math.nan
            ctrl_apod.y = 0.0
            ctrl_apod.z = apod_strength
            ctrl_apod.intensity = apod_strength if feature_state.apodization_enabled else -apod_strength

            focus_scale = max(feature_state.z_focus_scale, 0.0)
            ctrl_focus = command.points.add()
            ctrl_focus.x = math.nan
            ctrl_focus.y = 1.0
            ctrl_focus.z = feature_state.z_focus_offset
            ctrl_focus.intensity = focus_scale if feature_state.z_focus_enabled else -focus_scale
        
        # Set affine parameters if provided
        if affine_params:
            command.affine.translate_x = affine_params.get("slm_x0", 0.0)
            command.affine.translate_y = affine_params.get("slm_y0", 0.0)
            command.affine.translate_z = affine_params.get("cam_x0", 0.0)
            command.affine.rotate_x_deg = affine_params.get("cam_y0", 0.0)
            
            command.affine.rotate_y_deg = affine_params.get("slm_x1", 0.0)
            command.affine.rotate_z_deg = affine_params.get("slm_y1", 0.0)
            command.affine.scale_x = affine_params.get("cam_x1", 0.0)
            command.affine.scale_y = affine_params.get("cam_y1", 0.0)
            
            command.affine.scale_z = affine_params.get("slm_x2", 0.0)
            command.affine.shear_xy = affine_params.get("slm_y2", 0.0)
            command.affine.shear_yz = affine_params.get("cam_x2", 0.0)
            command.affine.shear_xz = affine_params.get("cam_y2", 0.0)
        
        # Put command in queue for streaming
        try:
            self._command_queue.put(command, timeout=0.1)
        except Exception as exc:
            logging.exception("Failed to queue SLM command: %s", exc)


def _load_dashboard_defaults() -> Dict[str, Any]:
    """Load dashboard defaults from services config."""
    try:
        services_cfg = _load_services_config(DEFAULT_SERVICES_CONFIG)
        dashboard_cfg = services_cfg.get("dashboard", {})
        return dashboard_cfg
    except Exception as exc:
        logging.warning(f"Could not load dashboard config, using hardcoded defaults: {exc}")
        return {}


# Load dashboard configuration
_DASHBOARD_CONFIG = _load_dashboard_defaults()

# Affine transformation fields
AFFINE_FIELDS = _DASHBOARD_CONFIG.get("affine_fields", [
    "translate_x", "translate_y", "translate_z",
    "rotate_x_deg", "rotate_y_deg", "rotate_z_deg",
    "scale_x", "scale_y", "scale_z",
    "shear_xy", "shear_yz", "shear_xz",
])

# Image display defaults
DEFAULT_SLM_WIDTH = _DASHBOARD_CONFIG.get("image", {}).get("default_slm_width", 1920)
DEFAULT_SLM_HEIGHT = _DASHBOARD_CONFIG.get("image", {}).get("default_slm_height", 1152)
DEFAULT_POINT_INTENSITY = _DASHBOARD_CONFIG.get("image", {}).get("default_point_intensity", 0.9)
POINT_SELECTION_RADIUS_PX = _DASHBOARD_CONFIG.get("image", {}).get("point_selection_radius_px", 18.0)

# SLM defaults
SLM_SEND_DEBOUNCE_SECONDS = _DASHBOARD_CONFIG.get("slm", {}).get("send_debounce_seconds", 0.0333)

# Monitoring defaults
DEFAULT_METRICS_HISTORY = _DASHBOARD_CONFIG.get("monitoring", {}).get("default_metrics_history", 1000)
MOVING_AVERAGE_WINDOW = _DASHBOARD_CONFIG.get("monitoring", {}).get("moving_average_window", 50)

# UI Layout Constants
_dimensions = _DASHBOARD_CONFIG.get("dimensions", {})
PLOT_HEIGHT = _dimensions.get("plot_height", 200)
HARDWARE_WINDOW_HEIGHT = _dimensions.get("hardware_window_height", 900)
IMAGE_METRICS_WINDOW_HEIGHT = _dimensions.get("image_metrics_window_height", 600)
SLM_METRICS_WINDOW_HEIGHT = _dimensions.get("slm_metrics_window_height", 300)

# Color Scheme - Distinct section colors
_colors = _DASHBOARD_CONFIG.get("colors", {})
HARDWARE_COLOR = tuple(_colors.get("hardware", [220, 80, 80, 255]))
IMAGE_COLOR = tuple(_colors.get("image", [80, 200, 90, 255]))
SLM_COLOR = tuple(_colors.get("slm", [230, 180, 60, 255]))
STATUS_CONNECTED = tuple(_colors.get("status_connected", [80, 220, 90, 255]))
STATUS_DISCONNECTED = tuple(_colors.get("status_disconnected", [220, 60, 60, 255]))
TEXT_PRIMARY = tuple(_colors.get("text_primary", [230, 230, 230, 255]))
TEXT_SECONDARY = tuple(_colors.get("text_secondary", [180, 180, 180, 255]))


def _compute_moving_average(values: Iterable[float], window: int = MOVING_AVERAGE_WINDOW) -> List[float]:
    """Compute moving average of values with given window size.
    
    Args:
        values: Input values to average
        window: Window size for moving average (default: MOVING_AVERAGE_WINDOW)
    
    Returns:
        List of averaged values (same length as input)
    """
    values_array = np.array(list(values))
    if len(values_array) < window:
        # Not enough data for full window, return cumulative average
        result = np.cumsum(values_array) / np.arange(1, len(values_array) + 1)
        return result.tolist()
    
    # Use numpy convolve for efficient moving average
    weights = np.ones(window) / window
    averaged = np.convolve(values_array, weights, mode='valid')
    
    # Pad the beginning with cumulative averages to maintain length
    prefix = np.cumsum(values_array[:window-1]) / np.arange(1, window)
    return np.concatenate([prefix, averaged]).tolist()


class AggregateControllerStreaming:
    """Main controller with streaming support."""
    
    def __init__(
        self,
        image_state: ImageAppState,
        image_client: ImageClient,
        dac_specs: Sequence[DacChannelSpec],
        analog_specs: Sequence[AnalogChannelSpec],
        shtc3_spec: Optional[Shtc3Spec],
        *,
        due_endpoint: EndpointConfig,
        slm_endpoint: EndpointConfig,
        slm_config_manager: SlmConfigManager,
        slm_feature_config_manager: SlmFeatureConfigManager,
        tracking_config_manager: TrackingConfigManager,
        dashboard_config_manager: DashboardConfigManager,
    ) -> None:
        self.image_state = image_state
        self.image_client = image_client
        self.due_manager = DueManagerStreaming()
        self.slm_client = SLMClient()
        self.image_endpoint = EndpointConfig(image_state.host, image_state.port)
        self.due_endpoint = due_endpoint
        self.slm_endpoint = slm_endpoint
        
        # Image bit depth setting (8, 12, or 16)
        self.image_bit_depth = 16  # Default to 16-bit
        
        self.dac_specs = {spec.name: spec for spec in dac_specs}
        self.analog_specs = {spec.name: spec for spec in analog_specs}
        self.shtc3_spec = shtc3_spec
        
        self.dac_values: Dict[str, float] = {}
        self.analog_values: Dict[str, float] = {}
        self.shtc3_values: Dict[str, float] = {}
        
        self.slm_points: List[SlmPoint] = []
        self.slm_dirty = False
        self.slm_last_send = 0.0
        self.slm_ack_messages: deque[str] = deque(maxlen=5)
        
        # SLM Configuration Manager
        self.slm_config_manager = slm_config_manager
        
        # Load current SLM configuration
        current_config = self.slm_config_manager.get_current_config()
        self.slm_affine_params = current_config.get_legacy_params()

        # Feature configuration manager
        self.slm_feature_config_manager = slm_feature_config_manager
        current_feature_config = self.slm_feature_config_manager.get_current_config()
        self.slm_feature_config = SlmFeatureConfig.from_dict(current_feature_config.to_dict())
        self.slm_feature_state = SlmFeatureState(
            apodization_enabled=self.slm_feature_config.apodization_enabled,
            apodization_strength=self.slm_feature_config.apodization_strength,
            z_focus_enabled=self.slm_feature_config.z_focus_enabled,
            z_focus_offset=self.slm_feature_config.z_focus_offset,
            z_focus_scale=self.slm_feature_config.z_focus_scale,
            default_point_z=self.slm_feature_config.default_point_z,
        )
        
        # Tracking Configuration Manager
        self.tracking_config_manager = tracking_config_manager
        
        # Load current tracking configuration
        current_tracking_config = self.tracking_config_manager.get_current_config()
        self.current_tracking_params = current_tracking_config.get_detection_params()
        
        # Dashboard Configuration Manager
        self.dashboard_config_manager = dashboard_config_manager
        self.saved_window_layout: Optional[Dict[str, Dict[str, Any]]] = None
        
        self.dragging_point_index: Optional[int] = None
        self.mouse_pos_image: Tuple[float, float] = (0.0, 0.0)  # Mouse position in image coordinates
        
        # Circle visualization settings (RGBA, 0-255)
        self.circle_color: Tuple[int, int, int, int] = (0, 255, 0, 255)  # Green
        self.circle_radius: float = 15.0  # pixels
        self.circle_thickness: float = 2.0  # pixels
        
        self.ui: Optional[AggregateUI] = None
        
        # Metrics
        self.metrics_history_limit = DEFAULT_METRICS_HISTORY
        self.analog_history: Dict[str, Tuple[deque[float], deque[float]]] = {}
        
        for name in self.analog_specs:
            self.analog_history[name] = (deque(maxlen=self.metrics_history_limit), deque(maxlen=self.metrics_history_limit))
        
        # Hardware telemetry history (timestamps and values)
        self.temp_history: Tuple[deque[float], deque[float]] = (deque(maxlen=self.metrics_history_limit), deque(maxlen=self.metrics_history_limit))
        self.humidity_history: Tuple[deque[float], deque[float]] = (deque(maxlen=self.metrics_history_limit), deque(maxlen=self.metrics_history_limit))
        
        # Monitoring state
        self.monitoring_active = False
        self.monitoring_interval = 5.0  # seconds, minimum 5s
        self.monitoring_thread: Optional[threading.Thread] = None
        self.monitoring_folder: Optional[Path] = None
        self.monitoring_files: Dict[str, Path] = {}  # type -> file path
        self.monitoring_lock = threading.Lock()
        
        # Experiment script manager
        scripts_dir = Path(__file__).parent.parent / "ExperimentScripts"
        self.script_manager = ScriptManager(scripts_dir)
        self.experiment_log_messages: deque[str] = deque(maxlen=100)
        
        # Experiment params manager
        self.experiment_params_manager = ExperimentParamsManager(scripts_dir)
        
        # Fine-tuning manager and state
        slm_config_dir = self.slm_config_manager.config_dir
        self.finetuning_manager = FineTuningManager(slm_config_dir)
        self.finetuning_state = FineTuningState()
        self.finetuning_thread: Optional[threading.Thread] = None
        self.manual_calibration_samples: List[FineTuningSample] = []
        self.manual_calibration_lock = threading.Lock()
        
        # Frame counter for periodic GC (collect every 300 frames ~= every 5 seconds at 60fps)
        self._frame_counter = 0
        self._gc_interval = 300

    def set_ui(self, ui: AggregateUI) -> None:
        self.ui = ui
        self._update_feature_controls_ui()
        self._update_manual_calibration_ui()
        self._set_manual_status("Capture manual samples to augment auto-tuning", TEXT_SECONDARY)

    # Image bit depth management
    
    def set_image_bit_depth(self, bit_depth: int) -> None:
        """Set the image bit depth for normalization (8, 12, or 16)."""
        if bit_depth not in [8, 12, 16]:
            logging.warning(f"Invalid bit depth {bit_depth}, must be 8, 12, or 16")
            return
        self.image_bit_depth = bit_depth
        self.image_state.image_bit_depth = bit_depth
        logging.info(f"Image bit depth set to {bit_depth}-bit")

    # Connection management
    
    def connect_image(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        host = host or self.image_endpoint.host
        port = port if port is not None else self.image_endpoint.port
        self.image_state.host = host
        self.image_state.port = port
        self.image_endpoint = EndpointConfig(host, port)
        self.image_client.connect(host, port)
        logging.info("Connected to image server at %s:%d", host, port)
        
        # Apply tracking configuration to server after connection
        self.apply_tracking_config_to_server()

    def disconnect_image(self) -> None:
        self.image_client.disconnect()
        # Collect garbage after disconnect to free network buffers
        gc.collect(generation=0)

    def connect_due(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        host = host or self.due_endpoint.host
        port = port if port is not None else self.due_endpoint.port
        target = f"{host}:{port}"
        self.due_endpoint = EndpointConfig(host, port)
        self.due_manager.connect(target, shtc3_spec=self.shtc3_spec)

    def disconnect_due(self) -> None:
        self.due_manager.disconnect()
        # Collect garbage after disconnect to free network buffers
        gc.collect(generation=0)

    def connect_slm(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        host = host or self.slm_endpoint.host
        port = port if port is not None else self.slm_endpoint.port
        self.slm_endpoint = EndpointConfig(host, port)
        target = f"{host}:{port}"
        self.slm_client.connect(target)

    def disconnect_slm(self) -> None:
        self.slm_client.disconnect()
        # Collect garbage after disconnect to free network buffers
        gc.collect(generation=0)

    # DAC control
    
    def _get_dac_spec(self, name: str) -> DacChannelSpec:
        spec = self.dac_specs.get(name)
        if not spec:
            raise ValueError(f"Unknown DAC channel: {name}")
        return spec

    def _clamp_dac_value(self, spec: DacChannelSpec, value: float) -> float:
        if spec.min_value is not None:
            value = max(value, spec.min_value)
        if spec.max_value is not None:
            value = min(value, spec.max_value)
        return value

    def adjust_dac(self, name: str, delta: float) -> float:
        current = self.dac_values.get(name, 0.0)
        new_value = current + delta
        self.set_dac_value(name, new_value)
        return self.dac_values[name]

    def set_dac_value(self, name: str, value: float) -> None:
        spec = self._get_dac_spec(name)
        clamped = self._clamp_dac_value(spec, value)
        # MISSING: Convert from physical units to voltage
        voltage = clamped / spec.conversion  # e.g., 25°C / 10 = 2.5V
        self.due_manager.write_dac(spec, voltage)
        self.dac_values[name] = clamped

    # SLM point management
    
    def add_point(self, x: float, y: float) -> None:
        """Add a new SLM point at the given image coordinates."""
        self.slm_points.append(
            SlmPoint(x, y, self.slm_feature_state.default_point_z, DEFAULT_POINT_INTENSITY)
        )
        self._mark_slm_dirty()
        logging.info("Added SLM point at (%.1f, %.1f), total: %d", x, y, len(self.slm_points))

    def move_point(self, index: int, x: float, y: float) -> None:
        """Move an existing SLM point to new coordinates."""
        if 0 <= index < len(self.slm_points):
            point = self.slm_points[index]
            self.slm_points[index] = SlmPoint(x, y, point.z, point.intensity)
            self._mark_slm_dirty()

    def remove_point(self, index: int) -> None:
        """Remove an SLM point by index."""
        if 0 <= index < len(self.slm_points):
            self.slm_points.pop(index)
            self._mark_slm_dirty()
            logging.info("Removed SLM point at index %d, remaining: %d", index, len(self.slm_points))

    def clear_points(self) -> None:
        """Clear all SLM points."""
        point_count = len(self.slm_points)
        self.slm_points.clear()
        self._mark_slm_dirty()
        # If we had many points, collect garbage to free memory
        if point_count > 50:
            gc.collect(generation=0)
        logging.info("Cleared all SLM points")

    def _mark_slm_dirty(self) -> None:
        """Mark SLM state as dirty to trigger a send."""
        self.slm_dirty = True

    def force_send_slm(self) -> None:
        """Force immediate send of SLM points."""
        if not self.slm_client.connected:
            logging.warning("Cannot send SLM command: not connected")
            return
        
        try:
            self.slm_client.send_command(
                self.slm_points,
                self.slm_affine_params,
                feature_state=self.slm_feature_state,
            )
            self.slm_last_send = time.time()
            self.slm_dirty = False
            logging.info("Sent %d SLM points", len(self.slm_points))
        except Exception as exc:
            logging.exception("Failed to send SLM command: %s", exc)
    
    def update_slm_affine(self, param_name: str, value: float) -> None:
        """Update a single affine parameter."""
        if param_name in self.slm_affine_params:
            self.slm_affine_params[param_name] = value
            logging.debug("Updated affine parameter %s = %.2f", param_name, value)
    
    # SLM Configuration Management
    
    def save_slm_config(self, name: str, description: str = "") -> bool:
        """Save current SLM configuration with given name."""
        try:
            # Create new config based on current parameters
            config = self.slm_config_manager.create_config(name, description)
            config.update_from_legacy_params(self.slm_affine_params)
            
            # Update the config in the manager
            self.slm_config_manager.update_config(name, config)
            logging.info("Saved SLM configuration: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to save SLM configuration %s: %s", name, exc)
            return False
    
    def load_slm_config(self, name: str) -> bool:
        """Load SLM configuration by name."""
        try:
            config = self.slm_config_manager.get_config(name)
            if config is None:
                logging.warning("SLM configuration not found: %s", name)
                return False
            
            # Update current parameters
            self.slm_affine_params = config.get_legacy_params()
            
            # Update UI if available
            if self.ui is not None:
                self._update_slm_affine_ui()
            
            logging.info("Loaded SLM configuration: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to load SLM configuration %s: %s", name, exc)
            return False
    
    def set_default_slm_config(self, name: str) -> bool:
        """Set a configuration as the default."""
        try:
            success = self.slm_config_manager.set_current_config(name)
            if success:
                logging.info("Set default SLM configuration: %s", name)
            else:
                logging.warning("Failed to set default SLM configuration: %s", name)
            return success
        except Exception as exc:
            logging.error("Failed to set default SLM configuration %s: %s", name, exc)
            return False
    
    def reset_slm_config_to_default(self) -> None:
        """Reset SLM configuration to default (all zeros)."""
        try:
            self.slm_config_manager.reset_to_default()
            self.load_slm_config("default")
            logging.info("Reset SLM configuration to default")
        except Exception as exc:
            logging.error("Failed to reset SLM configuration to default: %s", exc)
    
    def delete_slm_config(self, name: str) -> bool:
        """Delete an SLM configuration."""
        try:
            success = self.slm_config_manager.delete_config(name)
            if success:
                logging.info("Deleted SLM configuration: %s", name)
            else:
                logging.warning("Failed to delete SLM configuration: %s (may be default or not exist)", name)
            return success
        except Exception as exc:
            logging.error("Failed to delete SLM configuration %s: %s", name, exc)
            return False
    
    def list_slm_configs(self) -> List[str]:
        """Get list of available SLM configurations."""
        return self.slm_config_manager.list_configs()
    
    def get_current_slm_config_name(self) -> str:
        """Get name of current SLM configuration."""
        return self.slm_config_manager.get_current_config_name()
    
    def _update_slm_affine_ui(self) -> None:
        """Update SLM affine parameter UI with current values."""
        if self.ui is None or self.ui.slm_affine_inputs is None:
            return
        
        try:
            for param_name, input_id in self.ui.slm_affine_inputs.items():
                if param_name in self.slm_affine_params:
                    dpg.set_value(input_id, self.slm_affine_params[param_name])
        except Exception as exc:
            logging.error("Failed to update SLM affine UI: %s", exc)

    # SLM Feature Configuration Management

    def list_feature_configs(self) -> List[str]:
        return self.slm_feature_config_manager.list_configs()

    def get_current_feature_config_name(self) -> str:
        return self.slm_feature_config_manager.get_current_config_name()

    def save_feature_config(self, name: str, description: str = "") -> bool:
        try:
            config = self.slm_feature_config_manager.create_config(name, description)
            self._apply_feature_state_to_config(config)
            self.slm_feature_config_manager.update_config(name, config)
            logging.info("Saved feature configuration: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to save feature configuration %s: %s", name, exc)
            return False

    def load_feature_config(self, name: str) -> bool:
        try:
            config = self.slm_feature_config_manager.get_config(name)
            if config is None:
                logging.warning("Feature configuration not found: %s", name)
                return False
            self.slm_feature_config_manager.set_current_config(name)
            self._load_feature_state_from_config(config)
            if self.ui:
                self._update_feature_controls_ui()
                self._update_point_list()
            self._notify_feature_change()
            logging.info("Loaded feature configuration: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to load feature configuration %s: %s", name, exc)
            return False

    def set_default_feature_config(self, name: str) -> bool:
        if not self.slm_feature_config_manager.set_current_config(name):
            logging.warning("Feature configuration not found: %s", name)
            return False
        return self.load_feature_config(name)

    def reset_feature_config_to_default(self) -> None:
        self.slm_feature_config_manager.reset_to_default()
        self.load_feature_config("default")

    def delete_feature_config(self, name: str) -> bool:
        if not self.slm_feature_config_manager.delete_config(name):
            logging.warning("Cannot delete feature configuration: %s", name)
            return False
        logging.info("Deleted feature configuration: %s", name)
        return True

    def _apply_feature_state_to_config(self, config: SlmFeatureConfig) -> None:
        config.apodization_enabled = self.slm_feature_state.apodization_enabled
        config.apodization_strength = self.slm_feature_state.apodization_strength
        config.z_focus_enabled = self.slm_feature_state.z_focus_enabled
        config.z_focus_offset = self.slm_feature_state.z_focus_offset
        config.z_focus_scale = self.slm_feature_state.z_focus_scale
        config.default_point_z = self.slm_feature_state.default_point_z

    def _load_feature_state_from_config(self, config: SlmFeatureConfig) -> None:
        copied = SlmFeatureConfig.from_dict(config.to_dict())
        previous_default = self.slm_feature_state.default_point_z if hasattr(self, "slm_feature_state") else None
        self.slm_feature_config = copied
        self.slm_feature_state = SlmFeatureState(
            apodization_enabled=copied.apodization_enabled,
            apodization_strength=copied.apodization_strength,
            z_focus_enabled=copied.z_focus_enabled,
            z_focus_offset=copied.z_focus_offset,
            z_focus_scale=copied.z_focus_scale,
            default_point_z=copied.default_point_z,
        )
        self._apply_default_z_to_points(previous_default, copied.default_point_z)

    def _apply_default_z_to_points(self, previous_default: Optional[float], new_default: float) -> None:
        if previous_default is None:
            for idx, point in enumerate(self.slm_points):
                self.slm_points[idx] = SlmPoint(point.x, point.y, new_default, point.intensity)
            return

        for idx, point in enumerate(self.slm_points):
            if abs(point.z - previous_default) < 1e-6:
                self.slm_points[idx] = SlmPoint(point.x, point.y, new_default, point.intensity)

    def _update_feature_controls_ui(self) -> None:
        if not self.ui:
            return
        try:
            if self.ui.feature_apodization_checkbox is not None:
                dpg.set_value(self.ui.feature_apodization_checkbox, self.slm_feature_state.apodization_enabled)
            if self.ui.feature_apodization_strength_slider is not None:
                dpg.set_value(self.ui.feature_apodization_strength_slider, self.slm_feature_state.apodization_strength)
            if self.ui.feature_z_focus_checkbox is not None:
                dpg.set_value(self.ui.feature_z_focus_checkbox, self.slm_feature_state.z_focus_enabled)
            if self.ui.feature_z_focus_offset_slider is not None:
                dpg.set_value(self.ui.feature_z_focus_offset_slider, self.slm_feature_state.z_focus_offset)
            if self.ui.feature_z_focus_scale_slider is not None:
                dpg.set_value(self.ui.feature_z_focus_scale_slider, self.slm_feature_state.z_focus_scale)
            if self.ui.feature_default_point_z_slider is not None:
                dpg.set_value(self.ui.feature_default_point_z_slider, self.slm_feature_state.default_point_z)
            if self.ui.feature_config_combo is not None:
                dpg.configure_item(
                    self.ui.feature_config_combo,
                    items=self.list_feature_configs(),
                    default_value=self.get_current_feature_config_name(),
                )
        except Exception as exc:
            logging.error("Failed to update feature controls UI: %s", exc)

    def _notify_feature_change(self, immediate: bool = False) -> None:
        self._mark_slm_dirty()
        if immediate and self.slm_client.connected:
            self.force_send_slm()

    def set_apodization_enabled(self, enabled: bool) -> None:
        value = bool(enabled)
        self.slm_feature_state.apodization_enabled = value
        self.slm_feature_config.apodization_enabled = value
        self._notify_feature_change(immediate=True)

    def set_apodization_strength(self, strength: float) -> None:
        clamped = max(0.0, min(float(strength), 1.0))
        self.slm_feature_state.apodization_strength = clamped
        self.slm_feature_config.apodization_strength = clamped
        self._notify_feature_change(immediate=False)

    def set_z_focus_enabled(self, enabled: bool) -> None:
        value = bool(enabled)
        self.slm_feature_state.z_focus_enabled = value
        self.slm_feature_config.z_focus_enabled = value
        self._notify_feature_change(immediate=True)

    def set_z_focus_offset(self, offset: float) -> None:
        prev = self.slm_feature_state.z_focus_offset
        self.slm_feature_state.z_focus_offset = float(offset)
        self.slm_feature_config.z_focus_offset = float(offset)
        # Update existing points that still match the previous offset
        for idx, point in enumerate(self.slm_points):
            if abs(point.z - prev) < 1e-6:
                self.slm_points[idx] = SlmPoint(point.x, point.y, float(offset), point.intensity)
        if self.ui:
            self._update_point_list()
        self._notify_feature_change(immediate=False)

    def set_z_focus_scale(self, scale: float) -> None:
        value = max(0.0, float(scale))
        self.slm_feature_state.z_focus_scale = value
        self.slm_feature_config.z_focus_scale = value
        self._notify_feature_change(immediate=False)

    def set_default_point_z(self, value: float) -> None:
        prev_default = self.slm_feature_state.default_point_z
        self.slm_feature_state.default_point_z = float(value)
        self.slm_feature_config.default_point_z = float(value)
        for idx, point in enumerate(self.slm_points):
            if abs(point.z - prev_default) < 1e-6:
                self.slm_points[idx] = SlmPoint(point.x, point.y, float(value), point.intensity)
        if self.ui:
            self._update_point_list()
        self._notify_feature_change(immediate=False)
    
    # Tracking Configuration Management
    
    def save_tracking_config(self, name: str, description: str = "") -> bool:
        """Save current tracking configuration with given name."""
        try:
            # Create new config based on current parameters
            config = self.tracking_config_manager.create_config(name, description)
            config.update_from_detection_params(self.current_tracking_params)
            
            # Update the config in the manager
            self.tracking_config_manager.update_config(name, config)
            logging.info("Saved tracking configuration: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to save tracking configuration %s: %s", name, exc)
            return False
    
    def load_tracking_config(self, name: str) -> bool:
        """Load tracking configuration by name."""
        try:
            config = self.tracking_config_manager.get_config(name)
            if config is None:
                logging.warning("Tracking configuration not found: %s", name)
                return False
            
            # Update current parameters
            self.current_tracking_params = config.get_detection_params()
            
            # Update UI if available
            if self.ui is not None:
                self._update_tracking_params_ui()
            
            logging.info("Loaded tracking configuration: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to load tracking configuration %s: %s", name, exc)
            return False
    
    def set_default_tracking_config(self, name: str) -> bool:
        """Set a configuration as the default."""
        try:
            success = self.tracking_config_manager.set_current_config(name)
            if success:
                logging.info("Set default tracking configuration: %s", name)
            else:
                logging.warning("Failed to set default tracking configuration: %s", name)
            return success
        except Exception as exc:
            logging.error("Failed to set default tracking configuration %s: %s", name, exc)
            return False
    
    def reset_tracking_config_to_default(self) -> None:
        """Reset tracking configuration to default."""
        try:
            self.tracking_config_manager.reset_to_default()
            self.load_tracking_config("default")
            logging.info("Reset tracking configuration to default")
        except Exception as exc:
            logging.error("Failed to reset tracking configuration to default: %s", exc)
    
    def delete_tracking_config(self, name: str) -> bool:
        """Delete a tracking configuration."""
        try:
            success = self.tracking_config_manager.delete_config(name)
            if success:
                logging.info("Deleted tracking configuration: %s", name)
            else:
                logging.warning("Failed to delete tracking configuration: %s (may be default or not exist)", name)
            return success
        except Exception as exc:
            logging.error("Failed to delete tracking configuration %s: %s", name, exc)
            return False
    
    def list_tracking_configs(self) -> List[str]:
        """Get list of available tracking configurations."""
        return self.tracking_config_manager.list_configs()
    
    def get_current_tracking_config_name(self) -> str:
        """Get name of current tracking configuration."""
        return self.tracking_config_manager.get_current_config_name()
    
    def apply_tracking_config_to_server(self) -> bool:
        """Apply current tracking configuration to the image server."""
        try:
            # Use the existing tracking apply functionality
            response = self.image_client.update_tracking_config(self.current_tracking_params)
            if response:
                logging.info("Applied tracking configuration to server")
                return True
            else:
                logging.warning("Failed to apply tracking configuration to server")
                return False
        except Exception as exc:
            logging.error("Failed to apply tracking configuration to server: %s", exc)
            return False
    
    def _update_tracking_params_ui(self) -> None:
        """Update tracking parameter UI with current values."""
        if self.ui is None or self.ui.tracking_inputs is None:
            return
        
        try:
            for param_name, input_id in self.ui.tracking_inputs.items():
                if param_name in self.current_tracking_params:
                    value = self.current_tracking_params[param_name]
                    if dpg.does_item_exist(input_id):
                        dpg.set_value(input_id, value)
        except Exception as exc:
            logging.error("Failed to update tracking parameters UI: %s", exc)
    
    # Experiment Parameters Configuration Management
    
    def save_experiment_params(self, script_name: str, config_name: str, description: str = "") -> bool:
        """Save current experiment parameters for a script."""
        try:
            # Get current parameter values from the active script
            if self.script_manager.current_script:
                if self.script_manager.current_script.name == script_name:
                    params = self.script_manager.current_script.get_all_param_values()
                else:
                    # Script name doesn't match - get from UI widgets
                    params = self._get_current_experiment_params()
            else:
                # No active script - get from UI widgets
                params = self._get_current_experiment_params()
            
            success = self.experiment_params_manager.save_config(
                script_name=script_name,
                config_name=config_name,
                params=params,
                description=description
            )
            
            if success:
                logging.info(f"Saved experiment params '{config_name}' for script '{script_name}'")
            else:
                logging.warning(f"Failed to save experiment params '{config_name}' for script '{script_name}'")
            
            return success
        except Exception as exc:
            logging.error(f"Failed to save experiment params '{config_name}' for script '{script_name}': {exc}")
            return False
    
    def load_experiment_params(self, script_name: str, config_name: str) -> bool:
        """Load experiment parameters for a script."""
        try:
            config = self.experiment_params_manager.load_config(script_name, config_name)
            if not config:
                logging.warning(f"Experiment params config '{config_name}' not found for script '{script_name}'")
                return False
            
            # Apply parameters to UI widgets
            self._apply_experiment_params(config.params)
            
            # If script is currently loaded, also apply to script
            if self.script_manager.current_script:
                if self.script_manager.current_script.name == script_name:
                    self.script_manager.current_script.set_params_from_dict(config.params)
            
            logging.info(f"Loaded experiment params '{config_name}' for script '{script_name}'")
            return True
        except Exception as exc:
            logging.error(f"Failed to load experiment params '{config_name}' for script '{script_name}': {exc}")
            return False
    
    def set_default_experiment_params(self, script_name: str, config_name: str) -> bool:
        """Set default experiment parameters configuration for a script."""
        try:
            success = self.experiment_params_manager.set_default_config(script_name, config_name)
            if success:
                logging.info(f"Set default experiment params to '{config_name}' for script '{script_name}'")
            else:
                logging.warning(f"Failed to set default experiment params for script '{script_name}'")
            return success
        except Exception as exc:
            logging.error(f"Failed to set default experiment params for script '{script_name}': {exc}")
            return False
    
    def delete_experiment_params(self, script_name: str, config_name: str) -> bool:
        """Delete an experiment parameters configuration."""
        try:
            success = self.experiment_params_manager.delete_config(script_name, config_name)
            if success:
                logging.info(f"Deleted experiment params '{config_name}' for script '{script_name}'")
            else:
                logging.warning(f"Failed to delete experiment params '{config_name}' for script '{script_name}'")
            return success
        except Exception as exc:
            logging.error(f"Failed to delete experiment params '{config_name}' for script '{script_name}': {exc}")
            return False
    
    def list_experiment_params(self, script_name: str) -> List[str]:
        """Get list of saved experiment parameter configurations for a script."""
        return self.experiment_params_manager.list_configs(script_name)
    
    def get_default_experiment_params(self, script_name: str) -> Optional[str]:
        """Get name of default experiment parameters configuration for a script."""
        return self.experiment_params_manager.get_default_config(script_name)
    
    def load_default_experiment_params(self, script_name: str) -> bool:
        """Load default experiment parameters for a script."""
        try:
            default_name = self.get_default_experiment_params(script_name)
            if default_name:
                return self.load_experiment_params(script_name, default_name)
            return False
        except Exception as exc:
            logging.error(f"Failed to load default experiment params for script '{script_name}': {exc}")
            return False
    
    # Dashboard UI Configuration Management
    
    def save_ui_config(self, name: str, description: str = "") -> bool:
        """Save current UI configuration."""
        try:
            if not self.ui or not hasattr(self, 'dashboard_config_manager'):
                return False
            
            # Capture current UI state
            viewport_width = dpg.get_viewport_width()
            viewport_height = dpg.get_viewport_height()
            
            # Save DearPyGui's internal docking layout to a temporary ini file
            # This captures the complete docking state including tabs, splits, and parent-child relationships
            layout_ini_path = self.dashboard_config_manager.config_path.parent / f".layout_{name}.ini"
            try:
                dpg.save_init_file(str(layout_ini_path))
                with open(layout_ini_path, 'r', encoding='utf-8') as f:
                    docking_layout = f.read()
            except Exception as exc:
                logging.warning(f"Could not save docking layout: {exc}")
                docking_layout = None
            
            # Capture window positions and sizes
            windows = {}
            window_tags = {
                'connections': getattr(self.ui, 'connection_window', None),
                'monitoring': getattr(self.ui, 'monitoring_window', None),
                'experiment_scripts': getattr(self.ui, 'experiment_script_window', None),
                'image_viewer': getattr(self.ui, 'viewer_window', None),
                'image_display_controls': getattr(self.ui, 'image_display_window', None),
                'image_saving': getattr(self.ui, 'image_saving_window', None),
                'environment_dac': getattr(self.ui, 'env_window', None),
                'hardware_monitoring': getattr(self.ui, 'hardware_window', None),
                'image_metrics': getattr(self.ui, 'image_metrics_window', None),
                'slm_metrics': getattr(self.ui, 'slm_metrics_window', None),
                'tracking_parameters': getattr(self.ui, 'tracking_window', None),
                'slm_control': getattr(self.ui, 'slm_window', None),
                'ui_config': getattr(self.ui, 'ui_config_window', None) if hasattr(self.ui, 'ui_config_window') else None,
            }
            
            for window_name, tag in window_tags.items():
                if tag and dpg.does_item_exist(tag):
                    pos = dpg.get_item_pos(tag)
                    size = dpg.get_item_rect_size(tag)
                    item_config = dpg.get_item_configuration(tag)
                    windows[window_name] = {
                        'x': pos[0] if pos else None,
                        'y': pos[1] if pos else None,
                        'width': size[0] if size else None,
                        'height': size[1] if size else None,
                        'collapsed': item_config.get('collapsed', False),
                        'show': item_config.get('show', True),
                        # Save scroll position if scrollable
                        'scroll_x': dpg.get_x_scroll(tag) if dpg.does_item_exist(tag) else 0.0,
                        'scroll_y': dpg.get_y_scroll(tag) if dpg.does_item_exist(tag) else 0.0,
                    }
            
            # Capture current settings
            config = DashboardConfig(
                name=name,
                description=description,
                created_at=datetime.now(timezone.utc).isoformat(),
                viewport={
                    'width': viewport_width,
                    'height': viewport_height,
                    'title': dpg.get_viewport_title(),
                },
                windows=windows,
                docking_layout=docking_layout,
                image_display={
                    'display_mode': self.image_state.display_mode if hasattr(self.image_state, 'display_mode') else 'overlay',
                    'show_tile_grid': self.image_state.show_tile_grid if hasattr(self.image_state, 'show_tile_grid') else False,
                    'zoom': self.image_state.zoom,
                    'use_mass_colormap': self.image_state.use_mass_colormap if hasattr(self.image_state, 'use_mass_colormap') else True,
                    'mass_cutoff': self.image_state.mass_cutoff if hasattr(self.image_state, 'mass_cutoff') else 600.0,
                    'below_cutoff_color': list(self.image_state.below_cutoff_color) if hasattr(self.image_state, 'below_cutoff_color') else [255, 255, 255],
                    'above_cutoff_color': list(self.image_state.above_cutoff_color) if hasattr(self.image_state, 'above_cutoff_color') else [255, 0, 0],
                    'circle_scale': self.image_state.circle_scale if hasattr(self.image_state, 'circle_scale') else 1.0,
                },
                slm_visualization={
                    'circle_color': list(self.circle_color),
                    'circle_radius': self.circle_radius,
                    'circle_thickness': self.circle_thickness,
                },
                hardware_monitoring={
                    'history_limit': self.metrics_history_limit,
                    'show_temperature': True,
                    'show_humidity': True,
                    'show_analog_channels': True,
                },
                image_metrics={
                    'history_limit': self.metrics_history_limit,
                    'show_latency': True,
                    'show_processing': True,
                    'show_render': True,
                    'show_features': True,
                    'show_save': True,
                    'show_compression': True,
                },
                storage={
                    'auto_save_raw': self.image_state.auto_save_raw if hasattr(self.image_state, 'auto_save_raw') else False,
                    'auto_save_overlay': self.image_state.auto_save_overlay if hasattr(self.image_state, 'auto_save_overlay') else False,
                    'save_hdf5': self.image_state.save_hdf5 if hasattr(self.image_state, 'save_hdf5') else False,
                    'target_fps': self.image_state.storage_target_fps if hasattr(self.image_state, 'storage_target_fps') else 30.0,
                },
                monitoring={
                    'interval_seconds': self.monitoring_interval,
                    'auto_start': False,
                },
                experiment=self._get_current_experiment_params(),
                theme=self._get_current_theme_colors(),
            )
            
            return self.dashboard_config_manager.save_config(name, config)
        except Exception as exc:
            logging.error(f"Failed to save UI config '{name}': {exc}")
            return False
    
    def load_ui_config(self, name: str) -> bool:
        """Load and apply UI configuration."""
        try:
            if not hasattr(self, 'dashboard_config_manager'):
                return False
            
            config = self.dashboard_config_manager.load_config(name)
            if not config:
                return False
            
            # Apply viewport settings
            if config.viewport:
                dpg.configure_viewport(
                    dpg.get_active_window(),
                    width=config.viewport.get('width', 2560),
                    height=config.viewport.get('height', 1400),
                    title=config.viewport.get('title', 'Tweezer Control & Monitoring'),
                )
            
            # Restore DearPyGui docking layout from ini file if available
            if config.docking_layout:
                try:
                    # Write the ini content to a temporary file and load it
                    layout_ini_path = self.dashboard_config_manager.config_path.parent / f".layout_{name}_restore.ini"
                    with open(layout_ini_path, 'w', encoding='utf-8') as f:
                        f.write(config.docking_layout)
                    dpg.configure_app(init_file=str(layout_ini_path))
                    logging.info(f"Restored docking layout from config '{name}'")
                except Exception as exc:
                    logging.warning(f"Could not restore docking layout: {exc}")
            
            # Apply window positions and sizes
            if config.windows and self.ui:
                window_tags = {
                    'connections': getattr(self.ui, 'connection_window', None),
                    'monitoring': getattr(self.ui, 'monitoring_window', None),
                    'experiment_scripts': getattr(self.ui, 'experiment_script_window', None),
                    'image_viewer': getattr(self.ui, 'viewer_window', None),
                    'image_display_controls': getattr(self.ui, 'image_display_window', None),
                    'image_saving': getattr(self.ui, 'image_saving_window', None),
                    'environment_dac': getattr(self.ui, 'env_window', None),
                    'hardware_monitoring': getattr(self.ui, 'hardware_window', None),
                    'image_metrics': getattr(self.ui, 'image_metrics_window', None),
                    'slm_metrics': getattr(self.ui, 'slm_metrics_window', None),
                    'tracking_parameters': getattr(self.ui, 'tracking_window', None),
                    'slm_control': getattr(self.ui, 'slm_window', None),
                    'ui_config': getattr(self.ui, 'ui_config_window', None) if hasattr(self.ui, 'ui_config_window') else None,
                }
                
                for window_name, tag in window_tags.items():
                    if tag and dpg.does_item_exist(tag) and window_name in config.windows:
                        win_cfg = config.windows[window_name]
                        
                        # Restore position and size
                        if win_cfg.get('x') is not None and win_cfg.get('y') is not None:
                            dpg.set_item_pos(tag, [win_cfg['x'], win_cfg['y']])
                        if win_cfg.get('width') is not None and win_cfg.get('height') is not None:
                            dpg.configure_item(tag, width=win_cfg['width'], height=win_cfg['height'])
                        
                        # Restore collapsed state
                        if win_cfg.get('collapsed') is not None:
                            dpg.configure_item(tag, collapsed=win_cfg['collapsed'])
                        
                        # Restore visibility
                        if win_cfg.get('show') is not None:
                            dpg.configure_item(tag, show=win_cfg['show'])
                        
                        # Restore scroll positions
                        if win_cfg.get('scroll_x') is not None:
                            try:
                                dpg.set_x_scroll(tag, win_cfg['scroll_x'])
                            except Exception:
                                pass  # Not all windows are scrollable
                        if win_cfg.get('scroll_y') is not None:
                            try:
                                dpg.set_y_scroll(tag, win_cfg['scroll_y'])
                            except Exception:
                                pass  # Not all windows are scrollable
            
            # Store for later use by resize callback (legacy support)
            self.saved_window_layout = config.windows
            
            # Apply image display settings
            if config.image_display:
                display = config.image_display
                if 'display_mode' in display:
                    self.image_state.set_display_mode(display['display_mode'])
                if 'show_tile_grid' in display:
                    self.image_state.set_show_tile_grid(display['show_tile_grid'])
                if 'zoom' in display:
                    self.image_state.set_zoom(display['zoom'])
                if 'use_mass_colormap' in display:
                    self.image_state.set_use_mass_colormap(display['use_mass_colormap'])
                if 'mass_cutoff' in display:
                    self.image_state.set_mass_cutoff(display['mass_cutoff'])
                if 'circle_scale' in display:
                    self.image_state.set_circle_scale(display['circle_scale'])
                
                # Update UI controls
                if self.ui:
                    if self.ui.display_mode_combo and dpg.does_item_exist(self.ui.display_mode_combo):
                        dpg.set_value(self.ui.display_mode_combo, display.get('display_mode', 'overlay'))
                    if self.ui.tile_grid_checkbox and dpg.does_item_exist(self.ui.tile_grid_checkbox):
                        dpg.set_value(self.ui.tile_grid_checkbox, display.get('show_tile_grid', False))
                    if self.ui.zoom_slider and dpg.does_item_exist(self.ui.zoom_slider):
                        dpg.set_value(self.ui.zoom_slider, display.get('zoom', 1.0))
                    if self.ui.use_colormap_checkbox and dpg.does_item_exist(self.ui.use_colormap_checkbox):
                        dpg.set_value(self.ui.use_colormap_checkbox, display.get('use_mass_colormap', True))
                    if self.ui.mass_cutoff_input and dpg.does_item_exist(self.ui.mass_cutoff_input):
                        dpg.set_value(self.ui.mass_cutoff_input, display.get('mass_cutoff', 600.0))
                    if self.ui.circle_scale_slider and dpg.does_item_exist(self.ui.circle_scale_slider):
                        dpg.set_value(self.ui.circle_scale_slider, display.get('circle_scale', 1.0))
            
            # Apply SLM visualization settings
            if config.slm_visualization:
                slm_viz = config.slm_visualization
                self.circle_color = tuple(slm_viz.get('circle_color', [255, 0, 0, 255]))
                self.circle_radius = slm_viz.get('circle_radius', 15.0)
                self.circle_thickness = slm_viz.get('circle_thickness', 2)
                
                if self.ui:
                    if self.ui.slm_circle_color_picker and dpg.does_item_exist(self.ui.slm_circle_color_picker):
                        dpg.set_value(self.ui.slm_circle_color_picker, self.circle_color)
                    if self.ui.slm_circle_size_slider and dpg.does_item_exist(self.ui.slm_circle_size_slider):
                        dpg.set_value(self.ui.slm_circle_size_slider, self.circle_radius)
                    if self.ui.slm_circle_thickness_slider and dpg.does_item_exist(self.ui.slm_circle_thickness_slider):
                        dpg.set_value(self.ui.slm_circle_thickness_slider, self.circle_thickness)
            
            # Apply hardware monitoring settings
            if config.hardware_monitoring:
                hw_mon = config.hardware_monitoring
                if 'history_limit' in hw_mon:
                    self.set_hardware_history_limit(hw_mon['history_limit'])
            
            # Apply image metrics settings
            if config.image_metrics:
                img_met = config.image_metrics
                if 'history_limit' in img_met:
                    self.set_image_history_limit(img_met['history_limit'])
            
            # Apply storage settings
            if config.storage:
                storage = config.storage
                try:
                    if 'auto_save_raw' in storage:
                        self.image_state.set_auto_save_raw(storage['auto_save_raw'])
                    if 'auto_save_overlay' in storage:
                        self.image_state.set_auto_save_overlay(storage['auto_save_overlay'])
                    if 'save_hdf5' in storage:
                        self.image_state.set_save_hdf5(storage['save_hdf5'])
                    if 'target_fps' in storage:
                        self.image_state.set_storage_target_fps(storage['target_fps'])
                except Exception as exc:
                    logging.warning(f"Some storage settings could not be applied: {exc}")
            
            # Apply monitoring settings
            if config.monitoring:
                mon = config.monitoring
                if 'interval_seconds' in mon:
                    self.set_monitoring_interval(mon['interval_seconds'])
            
            # Apply experiment settings
            if config.experiment and self.ui:
                self._apply_experiment_params(config.experiment)
            
            # Update UI config combo box to show the loaded config
            if self.ui and self.ui.ui_config_combo and dpg.does_item_exist(self.ui.ui_config_combo):
                dpg.set_value(self.ui.ui_config_combo, name)
            
            # Collect garbage after loading large configuration
            gc.collect(generation=0)
            
            logging.info(f"Loaded UI configuration: {name}")
            return True
        except Exception as exc:
            logging.error(f"Failed to load UI config '{name}': {exc}")
            return False
    
    def set_default_ui_config(self, name: str) -> bool:
        """Set a configuration as the default."""
        try:
            if not hasattr(self, 'dashboard_config_manager'):
                return False
            return self.dashboard_config_manager.set_active_config(name)
        except Exception as exc:
            logging.error(f"Failed to set default UI config: {exc}")
            return False
    
    def delete_ui_config(self, name: str) -> bool:
        """Delete a UI configuration."""
        try:
            if not hasattr(self, 'dashboard_config_manager'):
                return False
            return self.dashboard_config_manager.delete_config(name)
        except Exception as exc:
            logging.error(f"Failed to delete UI config '{name}': {exc}")
            return False
    
    def list_ui_configs(self) -> List[str]:
        """List all UI configurations."""
        if not hasattr(self, 'dashboard_config_manager'):
            return []
        return self.dashboard_config_manager.list_configs()
    
    def get_current_ui_config_name(self) -> str:
        """Get current UI configuration name."""
        if not hasattr(self, 'dashboard_config_manager'):
            return "default"
        return self.dashboard_config_manager.active_config_name
    
    def _get_current_experiment_params(self) -> Dict[str, Any]:
        """Get current experiment parameters from UI (auto-config system)."""
        params = {}
        if self.ui and self.ui.experiment_param_widgets:
            # Get all parameter values from dynamically created widgets
            for param_name, widget_id in self.ui.experiment_param_widgets.items():
                if dpg.does_item_exist(widget_id):
                    params[param_name] = dpg.get_value(widget_id)
        return params
    
    def _apply_experiment_params(self, params: Dict[str, Any]) -> None:
        """Apply experiment parameters to UI (auto-config system)."""
        if not self.ui or not self.ui.experiment_param_widgets:
            return
        
        # Apply all parameter values to their respective widgets
        for param_name, value in params.items():
            if param_name in self.ui.experiment_param_widgets:
                widget_id = self.ui.experiment_param_widgets[param_name]
                if dpg.does_item_exist(widget_id):
                    dpg.set_value(widget_id, value)
    
    def _get_current_theme_colors(self) -> Dict[str, Any]:
        """Get current theme colors."""
        # For now, return defaults - can be extended to capture actual theme
        return {
            'hardware_color': [220, 80, 80, 255],
            'image_color': [80, 200, 90, 255],
            'slm_color': [230, 180, 60, 255],
            'status_connected': [80, 220, 90, 255],
            'status_disconnected': [220, 60, 60, 255],
            'text_primary': [230, 230, 230, 255],
            'text_secondary': [180, 180, 180, 255],
        }
    
    def find_point_at_position(self, x: float, y: float, radius: float = POINT_SELECTION_RADIUS_PX) -> Optional[int]:
        """Find the index of a point near the given position, or None."""
        for idx, pt in enumerate(self.slm_points):
            dx = pt.x - x
            dy = pt.y - y
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= radius:
                return idx
        return None

    # UI updates
    
    def handle_image_click(self, button: int, mouse_x: float, mouse_y: float) -> None:
        """Handle mouse click on image."""
        # Convert display coordinates to image coordinates
        img_x, img_y = self._display_to_image_coords(mouse_x, mouse_y)
        
        logging.info(f"Image click: button={button}, display=({mouse_x:.1f}, {mouse_y:.1f}), image=({img_x:.1f}, {img_y:.1f})")
        
        if button == 0:  # Left click
            # Check if clicking on existing point (for removal with right-click)
            point_idx = self.find_point_at_position(img_x, img_y)
            if point_idx is not None:
                # Start dragging
                self.dragging_point_index = point_idx
                logging.debug("Started dragging point %d", point_idx)
            else:
                # Add new point
                if self.slm_client.connected:
                    self.add_point(img_x, img_y)
        elif button == 1:  # Right click
            # Remove point if clicking on one
            point_idx = self.find_point_at_position(img_x, img_y)
            if point_idx is not None:
                self.remove_point(point_idx)

    def handle_image_drag(self, mouse_x: float, mouse_y: float) -> None:
        """Handle mouse drag on image."""
        img_x, img_y = self._display_to_image_coords(mouse_x, mouse_y)
        
        if self.dragging_point_index is not None:
            # Move the dragged point
            self.move_point(self.dragging_point_index, img_x, img_y)
            # Send immediately during drag for real-time feedback
            if time.time() - self.slm_last_send > SLM_SEND_DEBOUNCE_SECONDS:
                self.force_send_slm()

    def handle_image_release(self) -> None:
        """Handle mouse release on image."""
        if self.dragging_point_index is not None:
            logging.debug("Released point %d", self.dragging_point_index)
            self.dragging_point_index = None
            # Final send on release
            self.force_send_slm()
    
    def update_mouse_position(self, mouse_x: float, mouse_y: float) -> None:
        """Update tracked mouse position in image coordinates."""
        img_x, img_y = self._display_to_image_coords(mouse_x, mouse_y)
        self.mouse_pos_image = (img_x, img_y)
    
    def _display_to_image_coords(self, display_x: float, display_y: float) -> Tuple[float, float]:
        """Convert display coordinates to image coordinates, accounting for zoom.
        
        Args:
            display_x: X coordinate in display space (pixels on screen)
            display_y: Y coordinate in display space (pixels on screen)
            
        Returns:
            Tuple of (img_x, img_y) in original image coordinates
        """
        if not self.ui:
            return (0.0, 0.0)
        
        try:
            # Get the current zoom level
            zoom = self.image_state.zoom
            if zoom <= 0:
                zoom = 1.0
            
            # Convert from display coordinates back to original image coordinates
            # If zoom is 0.5, the displayed image is half the size of the original
            # So we need to divide the display position by zoom to get original position
            img_x = display_x / zoom
            img_y = display_y / zoom
            
            logging.debug(f"Coord transform: display=({display_x:.1f}, {display_y:.1f}), zoom={zoom:.2f}, image=({img_x:.1f}, {img_y:.1f})")
            
            return (img_x, img_y)
        except Exception as exc:
            logging.exception("Error converting display to image coords: %s", exc)
            return (0.0, 0.0)

    def set_hardware_history_limit(self, limit: int) -> None:
        """Set the history limit for hardware telemetry plots."""
        try:
            new_limit = max(100, min(50000, limit))
            # Update SHTC3 sensor histories (temp and humidity)
            self.temp_history = (deque(self.temp_history[0], maxlen=new_limit), deque(self.temp_history[1], maxlen=new_limit))
            self.humidity_history = (deque(self.humidity_history[0], maxlen=new_limit), deque(self.humidity_history[1], maxlen=new_limit))
            
            # Update all analog channel histories
            for name in self.analog_history:
                times, values = self.analog_history[name]
                self.analog_history[name] = (deque(times, maxlen=new_limit), deque(values, maxlen=new_limit))
            
            # Update metrics history limit
            self.metrics_history_limit = new_limit
        except Exception as exc:
            logging.exception("Error setting hardware history limit: %s", exc)

    def set_image_history_limit(self, limit: int) -> None:
        """Set the history limit for image server metrics plots."""
        try:
            new_limit = max(100, min(50000, limit))
            # Update image state metrics history
            self.image_state.set_metrics_history_limit(new_limit)
        except Exception as exc:
            logging.exception("Error setting image history limit: %s", exc)

    def update(self) -> None:
        """Main update loop."""
        try:
            # Update image
            if self.image_state.connected:
                # Get latest frame from state
                if self.image_state.latest_overlay_array is not None:
                    self._update_image_view_from_state()
                    # Process experiment script on new frame
                    self._process_experiment_script()
                # Update image server metrics
                self._update_image_metrics()
            
            # Update Due telemetry
            if self.due_manager.connected:
                telemetry = self.due_manager.get_telemetry()
                self._update_due_display(telemetry)
            
            # Update SLM metrics
            self._update_slm_metrics()
            
            # Update cursor
            self._update_cursor_label()
            
            # Update experiment script status display
            self._update_experiment_script_status()
            
            # Update fine-tuning UI
            self._update_finetuning_display()
            
            # Periodic garbage collection to prevent gradual memory buildup
            self._frame_counter += 1
            if self._frame_counter >= self._gc_interval:
                gc.collect(generation=0)  # Quick collection of young objects only
                self._frame_counter = 0
            
        except Exception as exc:
            logging.exception("Error in update loop: %s", exc)

    def _update_image_view_from_state(self) -> None:
        """Update image texture from image state."""
        if not self.ui:
            return
        
        # Select frame based on display mode
        if self.image_state.display_mode == "raw":
            # Show raw image without tracking overlay
            frame = self.image_state.latest_image_uint8
        else:
            # Show overlay if available, otherwise use latest image
            frame = self.image_state.latest_overlay_array
            if frame is None:
                frame = self.image_state.latest_image_uint8
        
        if frame is None:
            return
        
        try:
            h, w = frame.shape[:2]
            
            # Use zoom from image state
            scale = self.image_state.zoom
            resampled = _resample_for_display(frame, scale)
            
            display_h, display_w = resampled.shape[:2]
            
            if resampled.ndim == 2:
                rgb = np.stack([resampled] * 3, axis=-1)
            else:
                rgb = resampled[:, :, :3]
            
            # Draw circles on SLM points before converting to texture
            rgb = self._draw_slm_circles(rgb, scale)
            
            rgba = np.concatenate([rgb, np.full((display_h, display_w, 1), 255, dtype=np.uint8)], axis=-1)
            flat = (rgba.astype(np.float32) / 255.0).flatten()
            
            # Check if we need to create a new texture due to size change
            current_size = self.ui.texture_size
            if (display_w, display_h) != current_size:
                # Delete old texture and create new one with correct size
                if dpg.does_item_exist(self.ui.texture_id):
                    dpg.delete_item(self.ui.texture_id)
                
                new_texture = dpg.add_raw_texture(
                    width=display_w,
                    height=display_h,
                    default_value=flat,
                    format=dpg.mvFormat_Float_rgba,
                    parent=self.ui.texture_registry,
                )
                
                # Update the image widget to use the new texture
                dpg.configure_item(self.ui.image_item, texture_tag=new_texture)
                dpg.configure_item(self.ui.image_item, width=display_w, height=display_h)
                
                # Update our references
                self.ui.texture_id = new_texture
                self.ui.texture_size = (display_w, display_h)
                
                # Collect garbage after texture recreation to prevent memory buildup
                gc.collect()
            else:
                # Same size, just update the data
                dpg.set_value(self.ui.texture_id, flat)
                dpg.configure_item(self.ui.image_item, width=display_w, height=display_h)
        except Exception as exc:
            logging.exception("Error updating image view: %s", exc)

    def _draw_slm_circles(self, image: np.ndarray, scale: float) -> np.ndarray:
        """Draw circles on the image at SLM point locations and fine-tuning visualizations.
        
        Args:
            image: RGB image array (H, W, 3)
            scale: Current zoom scale factor
            
        Returns:
            Image with circles and visualizations drawn
        """
        import cv2
        
        # Make a copy so we don't modify the original
        img_with_circles = image.copy()
        
        # Draw fine-tuning visualization if active
        if (
            self.finetuning_state.show_visualization
            and (
                self.finetuning_state.active
                or bool(self.finetuning_state.manual_sample_points)
            )
        ):
            img_with_circles = self._draw_finetuning_overlay(img_with_circles, scale)
        
        # Draw regular SLM circles
        if self.slm_points and self.slm_client.connected:
            # Convert color from 0-255 to OpenCV format
            color_bgr = (self.circle_color[2], self.circle_color[1], self.circle_color[0])  # RGB to BGR
            
            # Scale radius and thickness
            scaled_radius = int(self.circle_radius * scale)
            scaled_thickness = max(1, int(self.circle_thickness * scale))
            
            logging.debug(f"Drawing {len(self.slm_points)} circles with scale={scale:.2f}, radius={scaled_radius}, thickness={scaled_thickness}")
            
            for idx, point in enumerate(self.slm_points):
                # Convert point coordinates (in original image space) to display coordinates
                display_x = int(point.x * scale)
                display_y = int(point.y * scale)
                
                logging.debug(f"  Point {idx}: orig=({point.x:.1f}, {point.y:.1f}) -> display=({display_x}, {display_y})")
                
                # Draw circle
                cv2.circle(  # type: ignore
                    img_with_circles,
                    (display_x, display_y),
                    scaled_radius,
                    color_bgr,
                    scaled_thickness
                )
        
        return img_with_circles
    
    def _draw_finetuning_overlay(self, image: np.ndarray, scale: float) -> np.ndarray:
        """Draw fine-tuning visualization overlay with animations.
        
        Args:
            image: RGB image array (H, W, 3)
            scale: Current zoom scale factor
            
        Returns:
            Image with fine-tuning overlay drawn
        """
        import cv2
        
        # Update animation phase (pulsing effect)
        self.finetuning_state.animation_phase = (self.finetuning_state.animation_phase + 0.05) % 1.0
        pulse = 0.7 + 0.3 * np.sin(self.finetuning_state.animation_phase * 2 * np.pi)
        
        # Draw sampling area boundary
        if self.finetuning_state.sample_area_max_x > 0:
            margin_color = (100, 100, 255)  # Light blue
            margin_thickness = max(1, int(2 * scale))
            
            min_x = int(self.finetuning_state.sample_area_min_x * scale)
            min_y = int(self.finetuning_state.sample_area_min_y * scale)
            max_x = int(self.finetuning_state.sample_area_max_x * scale)
            max_y = int(self.finetuning_state.sample_area_max_y * scale)
            
            cv2.rectangle(image, (min_x, min_y), (max_x, max_y), margin_color, margin_thickness)  # type: ignore
        
        # Draw completed sample points and their matches
        for i, (sample_pt, particle_pt) in enumerate(zip(
            self.finetuning_state.sample_points,
            self.finetuning_state.matched_particles
        )):
            sample_x = int(sample_pt[0] * scale)
            sample_y = int(sample_pt[1] * scale)
            particle_x = int(particle_pt[0] * scale)
            particle_y = int(particle_pt[1] * scale)
            
            error = self.finetuning_state.errors[i] if i < len(self.finetuning_state.errors) else 0
            
            # Color based on error (green = good, yellow = medium, red = bad)
            if error < 5.0:
                color = (0, 255, 0)  # Green
            elif error < 15.0:
                color = (0, 255, 255)  # Yellow
            else:
                color = (0, 0, 255)  # Red
            
            # Draw target point (small cross)
            cross_size = int(8 * scale)
            cv2.line(image, (sample_x - cross_size, sample_y), (sample_x + cross_size, sample_y), color, 2)  # type: ignore
            cv2.line(image, (sample_x, sample_y - cross_size), (sample_x, sample_y + cross_size), color, 2)  # type: ignore
            
            # Draw matched particle (small circle)
            cv2.circle(image, (particle_x, particle_y), int(5 * scale), color, 2)  # type: ignore
            
            # Draw error vector (line from target to particle)
            cv2.line(image, (sample_x, sample_y), (particle_x, particle_y), color, 1)  # type: ignore

        # Draw manual sample overlays in magenta
        for manual_pt, manual_particle, error in zip(
            self.finetuning_state.manual_sample_points,
            self.finetuning_state.manual_matched_particles,
            self.finetuning_state.manual_errors,
        ):
            sample_x = int(manual_pt[0] * scale)
            sample_y = int(manual_pt[1] * scale)
            particle_x = int(manual_particle[0] * scale)
            particle_y = int(manual_particle[1] * scale)
            color = (255, 0, 255)
            size = int(10 * scale)
            cv2.rectangle(  # type: ignore
                image,
                (sample_x - size, sample_y - size),
                (sample_x + size, sample_y + size),
                color,
                2,
            )
            cv2.circle(image, (particle_x, particle_y), int(5 * scale), color, 2)  # type: ignore
            cv2.line(image, (sample_x, sample_y), (particle_x, particle_y), color, 1)  # type: ignore
        
        # Draw current target with pulsing animation
        if self.finetuning_state.current_target:
            target_x = int(self.finetuning_state.current_target[0] * scale)
            target_y = int(self.finetuning_state.current_target[1] * scale)
            
            # Pulsing cyan circle for current target
            pulse_color = (int(255 * pulse), 255, 255)  # Cyan with pulsing brightness
            pulse_radius = int((15 + 5 * pulse) * scale)
            cv2.circle(image, (target_x, target_y), pulse_radius, pulse_color, 3)  # type: ignore
            
            # Add cross hair
            cross_size = int(20 * scale)
            cv2.line(image, (target_x - cross_size, target_y), (target_x + cross_size, target_y), pulse_color, 2)  # type: ignore
            cv2.line(image, (target_x, target_y - cross_size), (target_x, target_y + cross_size), pulse_color, 2)  # type: ignore
            
            # Draw text label
            label = f"Sample {self.finetuning_state.current_sample}/{self.finetuning_state.total_samples}"
            font_scale = 0.5 * scale
            thickness = max(1, int(1 * scale))
            text_offset_y = int(30 * scale)
            
            cv2.putText(  # type: ignore
                image, label,
                (target_x + int(20 * scale), target_y - text_offset_y),
                cv2.FONT_HERSHEY_SIMPLEX,  # type: ignore
                font_scale, pulse_color, thickness
            )
        
        # Draw current matched particle with pulsing animation
        if self.finetuning_state.current_match:
            match_x = int(self.finetuning_state.current_match[0] * scale)
            match_y = int(self.finetuning_state.current_match[1] * scale)
            
            # Pulsing green circle for matched particle
            match_color = (0, int(255 * pulse), 0)  # Green with pulsing brightness
            match_radius = int((12 + 4 * pulse) * scale)
            cv2.circle(image, (match_x, match_y), match_radius, match_color, 3)  # type: ignore
            
            # Draw line connecting current target to match
            if self.finetuning_state.current_target:
                target_x = int(self.finetuning_state.current_target[0] * scale)
                target_y = int(self.finetuning_state.current_target[1] * scale)
                cv2.line(image, (target_x, target_y), (match_x, match_y), (0, 255, 255), 2)  # type: ignore

        # Draw trace of tracked particle history
        if self.finetuning_state.current_track_history:
            track_points = [
                (int(px * scale), int(py * scale))
                for px, py in self.finetuning_state.current_track_history
            ]
            if len(track_points) >= 2:
                cv2.polylines(  # type: ignore
                    image,
                    [np.array(track_points, dtype=np.int32)],
                    False,
                    (255, 165, 0),
                    max(1, int(2 * scale)),
                )
            for px, py in track_points[-5:]:
                cv2.circle(image, (px, py), max(1, int(3 * scale)), (255, 200, 0), -1)  # type: ignore
        
        return image
    
    def _update_cursor_label(self) -> None:
        """Update cursor position label with scaled image coordinates."""
        if not self.ui:
            return
        try:
            img_x, img_y = self.mouse_pos_image
            cursor_text = f"Cursor: ({img_x:.1f}, {img_y:.1f})"
            dpg.set_value(self.ui.cursor_label, cursor_text)
        except Exception:
            pass

    def _update_due_display(self, telemetry: Dict[str, float]) -> None:
        """Update Due telemetry display."""
        if not self.ui:
            return
        
        try:
            # Update status with color
            if self.due_manager.connected:
                status_text = f"Due: Connected to {self.due_endpoint.display()} | {len(telemetry)} channels"
                dpg.set_value(self.ui.due_status_label, status_text)
                dpg.configure_item(self.ui.due_status_label, color=STATUS_CONNECTED)
            else:
                dpg.set_value(self.ui.due_status_label, "Due: Disconnected")
                dpg.configure_item(self.ui.due_status_label, color=STATUS_DISCONNECTED)
            
            # Update analog readings
            for name, value in telemetry.items():
                if name in self.analog_specs:
                    label_id = self.ui.analog_labels.get(name)
                    if label_id:
                        spec = self.analog_specs[name]
                        # Value is already converted by the server, use it directly
                        dpg.set_value(label_id, f"{spec.label}: {value:.3f} {spec.unit}")
                    
                    # Update history
                    if name in self.analog_history:
                        times, values = self.analog_history[name]
                        times.append(time.time())
                        values.append(value)
            
            # Update SHTC3 sensors (temperature and humidity)
            current_time = time.time()
            
            if "SHTC3_TEMPERATURE" in telemetry:
                temp_value = telemetry["SHTC3_TEMPERATURE"]
                temp_label = self.ui.shtc3_labels.get("temp")
                temp_display = self.ui.shtc3_display_labels.get("temp", "Temperature")
                if temp_label:
                    dpg.set_value(temp_label, f"{temp_display}: {temp_value:.2f} °C")
                
                # Update temperature history and plot
                temp_times, temp_values = self.temp_history
                temp_times.append(current_time)
                temp_values.append(temp_value)
                
                if self.ui.temp_series and len(temp_times) > 1:
                    times_array = list(temp_times)
                    values_array = list(temp_values)
                    
                    # Apply moving average to smooth the plot data
                    if len(values_array) >= MOVING_AVERAGE_WINDOW:
                        values_array = _compute_moving_average(values_array)
                    
                    dpg.set_value(self.ui.temp_series, [times_array, values_array])
                    if self.ui.temp_x_axis:
                        dpg.fit_axis_data(self.ui.temp_x_axis)
                    if self.ui.temp_y_axis:
                        dpg.fit_axis_data(self.ui.temp_y_axis)
            
            if "SHTC3_HUMIDITY" in telemetry:
                humidity_value = telemetry["SHTC3_HUMIDITY"]
                humidity_label = self.ui.shtc3_labels.get("humidity")
                humidity_display = self.ui.shtc3_display_labels.get("humidity", "Humidity")
                if humidity_label:
                    dpg.set_value(humidity_label, f"{humidity_display}: {humidity_value:.1f} %RH")
                
                # Update humidity history and plot
                humidity_times, humidity_values = self.humidity_history
                humidity_times.append(current_time)
                humidity_values.append(humidity_value)
                
                if self.ui.humidity_series and len(humidity_times) > 1:
                    times_array = list(humidity_times)
                    values_array = list(humidity_values)
                    
                    # Apply moving average to smooth the plot data
                    if len(values_array) >= MOVING_AVERAGE_WINDOW:
                        values_array = _compute_moving_average(values_array)
                    
                    dpg.set_value(self.ui.humidity_series, [times_array, values_array])
                    if self.ui.humidity_x_axis:
                        dpg.fit_axis_data(self.ui.humidity_x_axis)
                    if self.ui.humidity_y_axis:
                        dpg.fit_axis_data(self.ui.humidity_y_axis)
            
            # Dynamically update all analog channel plots
            for name in self.analog_specs:
                if name in telemetry and name in self.analog_history:
                    value = telemetry[name]
                    times, values = self.analog_history[name]
                    times.append(current_time)
                    values.append(value)
                    
                    # Update plot series if exists
                    if self.ui.analog_plot_series and name in self.ui.analog_plot_series:
                        series_id = self.ui.analog_plot_series[name]
                        if series_id and len(times) > 1:
                            times_array = list(times)
                            values_array = list(values)
                            
                            # Apply moving average to smooth the plot data
                            if len(values_array) >= MOVING_AVERAGE_WINDOW:
                                values_array = _compute_moving_average(values_array)
                            
                            dpg.set_value(series_id, [times_array, values_array])
                            
                            # Fit axes - find the plot group key for this channel
                            spec = self.analog_specs[name]
                            plot_key = f"pair_{spec.pair_graph_code}" if spec.pair_graph_code is not None else name
                            
                            if self.ui.analog_plot_axes and plot_key in self.ui.analog_plot_axes:
                                x_axis, y_axis = self.ui.analog_plot_axes[plot_key]
                                if x_axis:
                                    dpg.fit_axis_data(x_axis)
                                if y_axis:
                                    dpg.fit_axis_data(y_axis)
        
        except Exception as exc:
            logging.exception("Error updating Due display: %s", exc)

    def _update_image_metrics(self) -> None:
        """Update image server metrics display."""
        if not self.ui:
            return
        
        try:
            # Update sequence and detection info from image state
            if self.ui.image_sequence_text:
                seq = self.image_state.latest_sequence
                filename = self.image_state.latest_filename or "N/A"
                dpg.set_value(self.ui.image_sequence_text, f"Sequence {seq} | {filename}")
            
            if self.ui.image_detection_text:
                count = self.image_state.detection_count
                dpg.set_value(self.ui.image_detection_text, f"Detections: {count}")
            
            if self.ui.image_latency_text:
                latency = self.image_state.latest_latency_ms
                dpg.set_value(self.ui.image_latency_text, f"Frame latency: {latency} ms")
            
            if self.ui.image_processing_text:
                processing = self.image_state.latest_processing_ms
                dpg.set_value(self.ui.image_processing_text, f"Processing: {processing} ms")
            
            if self.ui.image_request_latency_text:
                req_latency = self.image_state.latest_request_latency_ms
                dpg.set_value(self.ui.image_request_latency_text, f"Request latency: {req_latency:.1f} ms")
            
            if self.ui.image_render_latency_text:
                render_latency = self.image_state.latest_render_latency_ms
                dpg.set_value(self.ui.image_render_latency_text, f"Render prep: {render_latency:.1f} ms")
            
            # Update metrics window text displays
            if self.ui.image_metrics_sequence_text:
                seq = self.image_state.latest_sequence
                dpg.set_value(self.ui.image_metrics_sequence_text, f"Sequence: {seq}")
            
            if self.ui.image_metrics_latency_text:
                latency = self.image_state.latest_latency_ms
                dpg.set_value(self.ui.image_metrics_latency_text, f"Latency: {latency} ms")
            
            if self.ui.image_metrics_processing_text:
                processing = self.image_state.latest_processing_ms
                dpg.set_value(self.ui.image_metrics_processing_text, f"Processing: {processing} ms")
            
            if self.ui.image_metrics_render_text:
                render = self.image_state.latest_render_latency_ms
                dpg.set_value(self.ui.image_metrics_render_text, f"Render: {render:.1f} ms")
            
            if self.ui.image_metrics_features_text:
                features = self.image_state.detection_count
                dpg.set_value(self.ui.image_metrics_features_text, f"Detections: {features}")
            
            # Update storage/save metrics
            if self.ui.save_text:
                duration = self.image_state.latest_save_duration_ms
                kind = self.image_state.latest_save_kind or "idle"
                dpg.set_value(self.ui.save_text, f"Last save ({kind}): {duration:.1f} ms")
            
            if self.ui.storage_ratio_text:
                ratio = self.image_state.latest_storage_ratio
                dpg.set_value(self.ui.storage_ratio_text, f"Compression: {ratio:.1f}%")
            
            if self.ui.storage_codec_text:
                codec = self.image_state.latest_storage_codec or "n/a"
                dpg.set_value(self.ui.storage_codec_text, f"Codec: {codec}")
            
            if self.ui.storage_bytes_text:
                bytes_in = self.image_state.latest_storage_bytes_in
                bytes_out = self.image_state.latest_storage_bytes_out
                dpg.set_value(self.ui.storage_bytes_text, f"Bytes: {bytes_out:,} / {bytes_in:,}")
            
            if self.ui.storage_throttle_text:
                throttle = self.image_state.latest_throttle_ms
                dpg.set_value(self.ui.storage_throttle_text, f"Throttle: {throttle:.1f} ms")
            
            if self.ui.storage_message_text:
                message = self.image_state.latest_storage_message or "(none)"
                dpg.set_value(self.ui.storage_message_text, f"Save Message: {message}")
            
            # Update plots - Latency
            if self.ui.img_latency_series and self.image_state.metric_timestamps:
                timestamps = list(self.image_state.metric_timestamps)
                latencies = list(self.image_state.latency_history)
                if timestamps and latencies and len(timestamps) == len(latencies):
                    dpg.set_value(self.ui.img_latency_series, [timestamps, latencies])
                    if self.ui.img_latency_x_axis:
                        dpg.fit_axis_data(self.ui.img_latency_x_axis)
                    if self.ui.img_latency_y_axis:
                        dpg.fit_axis_data(self.ui.img_latency_y_axis)
            
            # Processing plot
            if self.ui.img_processing_series and self.image_state.metric_timestamps:
                timestamps = list(self.image_state.metric_timestamps)
                processing = list(self.image_state.processing_history)
                if timestamps and processing and len(timestamps) == len(processing):
                    dpg.set_value(self.ui.img_processing_series, [timestamps, processing])
                    if self.ui.img_processing_x_axis:
                        dpg.fit_axis_data(self.ui.img_processing_x_axis)
                    if self.ui.img_processing_y_axis:
                        dpg.fit_axis_data(self.ui.img_processing_y_axis)
            
            # Render plot
            if self.ui.img_render_series and self.image_state.metric_timestamps:
                timestamps = list(self.image_state.metric_timestamps)
                render = list(self.image_state.render_history)
                if timestamps and render and len(timestamps) == len(render):
                    dpg.set_value(self.ui.img_render_series, [timestamps, render])
                    if self.ui.img_render_x_axis:
                        dpg.fit_axis_data(self.ui.img_render_x_axis)
                    if self.ui.img_render_y_axis:
                        dpg.fit_axis_data(self.ui.img_render_y_axis)
            
            # Save duration plot
            if self.ui.img_save_series and self.image_state.save_timestamps:
                timestamps = list(self.image_state.save_timestamps)
                durations = list(self.image_state.save_history)
                if timestamps and durations and len(timestamps) == len(durations):
                    dpg.set_value(self.ui.img_save_series, [timestamps, durations])
                    if self.ui.img_save_x_axis:
                        dpg.fit_axis_data(self.ui.img_save_x_axis)
                    if self.ui.img_save_y_axis:
                        dpg.fit_axis_data(self.ui.img_save_y_axis)
            
            # Compression ratio plot
            if self.ui.img_compression_series and self.image_state.compression_timestamps:
                timestamps = list(self.image_state.compression_timestamps)
                ratios = list(self.image_state.compression_history)
                if timestamps and ratios and len(timestamps) == len(ratios):
                    dpg.set_value(self.ui.img_compression_series, [timestamps, ratios])
                    if self.ui.img_compression_x_axis:
                        dpg.fit_axis_data(self.ui.img_compression_x_axis)
                    if self.ui.img_compression_y_axis:
                        dpg.fit_axis_data(self.ui.img_compression_y_axis)
            
            # Feature/detection count plot
            if self.ui.img_features_series and self.image_state.metric_timestamps:
                timestamps = list(self.image_state.metric_timestamps)
                features = list(self.image_state.feature_history)
                if timestamps and features and len(timestamps) == len(features):
                    dpg.set_value(self.ui.img_features_series, [timestamps, features])
                    if self.ui.img_features_x_axis:
                        dpg.fit_axis_data(self.ui.img_features_x_axis)
                    if self.ui.img_features_y_axis:
                        dpg.fit_axis_data(self.ui.img_features_y_axis)
        
        except Exception as exc:
            logging.exception("Error updating image metrics: %s", exc)

    def _update_point_list(self) -> None:
        """Update the visual list of SLM points."""
        if not self.ui or not self.ui.slm_points_list_group:
            return
        
        try:
            # Clear existing list
            children = dpg.get_item_children(self.ui.slm_points_list_group, slot=1)
            if children:
                for child in children:
                    dpg.delete_item(child)
            
            # Add each point as a list item
            for idx, point in enumerate(self.slm_points):
                with dpg.group(parent=self.ui.slm_points_list_group, horizontal=True):
                    dpg.add_text(
                        f"{idx}: ({point.x:.1f}, {point.y:.1f}) z={point.z:.2f} I={point.intensity:.2f}",
                        color=TEXT_PRIMARY,
                        tag=f"slm_point_{idx}"
                    )
                    dpg.add_button(
                        label="X",
                        width=25,
                        height=20,
                        callback=lambda s, a, u: u[0].remove_point(u[1]),
                        user_data=(self, idx),
                        tag=f"slm_point_remove_{idx}"
                    )
        except Exception as exc:
            logging.exception("Error updating point list: %s", exc)
    
    def _update_slm_metrics(self) -> None:
        """Update SLM server metrics display."""
        if not self.ui:
            return
        
        try:
            # Update point count
            if self.ui.slm_points_label:
                dpg.set_value(self.ui.slm_points_label, f"Active Points: {len(self.slm_points)}")
            
            # Update point list
            self._update_point_list()
            
            # Update status/ack message
            if self.ui.slm_ack_label:
                if self.slm_client.connected:
                    if self.slm_client.last_ack:
                        ack = self.slm_client.last_ack
                        status_text = f"Status: {ack.stage} | {ack.detail or 'OK'}"
                        dpg.set_value(self.ui.slm_ack_label, status_text)
                    else:
                        dpg.set_value(self.ui.slm_ack_label, "Status: Connected, ready")
                else:
                    dpg.set_value(self.ui.slm_ack_label, "Status: Not connected")
            
            # Auto-send if dirty and enough time has passed
            if self.slm_dirty and self.slm_client.connected:
                now = time.time()
                if now - self.slm_last_send > SLM_SEND_DEBOUNCE_SECONDS:
                    self.force_send_slm()
            
            # Update SLM generation metric if available
            if self.ui.slm_generation_text:
                if self.slm_client.last_ack and hasattr(self.slm_client.last_ack, 'metrics'):
                    metrics = self.slm_client.last_ack.metrics
                    if hasattr(metrics, 'generation_ms'):
                        dpg.set_value(self.ui.slm_generation_text, f"Generation: {metrics.generation_ms:.2f} ms")
                    else:
                        dpg.set_value(self.ui.slm_generation_text, "Generation: --")
                else:
                    dpg.set_value(self.ui.slm_generation_text, "Generation: --")
            
        except Exception as exc:
            logging.exception("Error updating SLM metrics: %s", exc)
    
    def _update_finetuning_display(self) -> None:
        """Update fine-tuning UI elements."""
        if not self.ui:
            return
        
        try:
            # Update progress bar
            if self.ui.finetuning_progress_bar:
                dpg.set_value(self.ui.finetuning_progress_bar, self.finetuning_state.progress)
            
            # Update status text
            if self.ui.finetuning_status_text:
                if self.finetuning_state.active:
                    if self.finetuning_state.paused:
                        status = f"Paused at {self.finetuning_state.current_sample}/{self.finetuning_state.total_samples}"
                    else:
                        status = f"Running: {self.finetuning_state.current_sample}/{self.finetuning_state.total_samples}"
                    dpg.set_value(self.ui.finetuning_status_text, status)
                    dpg.configure_item(self.ui.finetuning_status_text, color=SLM_COLOR)
                elif self.finetuning_state.progress >= 1.0:
                    status = "Completed! Check configs above."
                    dpg.set_value(self.ui.finetuning_status_text, status)
                    dpg.configure_item(self.ui.finetuning_status_text, color=STATUS_CONNECTED)
                    
                    # Re-enable start button
                    if self.ui.finetuning_start_button:
                        dpg.configure_item(self.ui.finetuning_start_button, enabled=True)
                    if self.ui.finetuning_pause_button:
                        dpg.configure_item(self.ui.finetuning_pause_button, enabled=False)
                    if self.ui.finetuning_stop_button:
                        dpg.configure_item(self.ui.finetuning_stop_button, enabled=False)
                    
                    # Refresh config list and update metadata
                    if self.ui.finetuning_config_combo:
                        configs = self.list_finetuning_configs()
                        if configs:
                            dpg.configure_item(
                                self.ui.finetuning_config_combo,
                                items=configs,
                                default_value=configs[0]  # Select newest
                            )
                            # Force metadata update for the new config
                            _update_finetuning_metadata_display(self)
                        else:
                            dpg.configure_item(
                                self.ui.finetuning_config_combo,
                                items=["No fine-tuned configs"],
                                default_value="No fine-tuned configs"
                            )
                    
                    # Reset progress to allow detection of next completion
                    # (use a flag to avoid repeated updates)
                    if not hasattr(self, '_last_completion_notified'):
                        self._last_completion_notified = False
                    if not self._last_completion_notified:
                        self._last_completion_notified = True
                        logging.info("Fine-tuning completion detected, UI updated")
                else:
                    status = "Ready to start"
                    dpg.set_value(self.ui.finetuning_status_text, status)
                    dpg.configure_item(self.ui.finetuning_status_text, color=SLM_COLOR)
            
            # Update config metadata when selection changes
            if self.ui.finetuning_config_combo and self.ui.finetuning_metadata_text:
                config_name = dpg.get_value(self.ui.finetuning_config_combo)
                if config_name and config_name != "No fine-tuned configs":
                    current_text = dpg.get_value(self.ui.finetuning_metadata_text)
                    if not current_text:  # Only update if empty
                        _update_finetuning_metadata_display(self)
        
        except Exception as exc:
            logging.exception("Error updating fine-tuning display: %s", exc)

    # Monitoring functionality
    
    def start_monitoring(self) -> None:
        """Start metrics monitoring to files."""
        if self.monitoring_active:
            logging.warning("Monitoring already active")
            return
        
        # Create monitoring folder with timestamp
        base_folder = _REPO_ROOT / "logs" / "monitoring"
        base_folder.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.monitoring_folder = base_folder / timestamp
        self.monitoring_folder.mkdir(parents=True, exist_ok=True)
        
        # Create CSV files with headers
        self._create_monitoring_files()
        
        # Update UI to show folder path
        if self.ui and self.ui.monitoring_path_text:
            folder_name = self.monitoring_folder.name
            dpg.configure_item(self.ui.monitoring_path_text, label=f"📁 {folder_name}")
        
        # Start monitoring thread
        self.monitoring_active = True
        self.monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitoring_thread.start()
        
        # Update button
        if self.ui and self.ui.monitoring_start_button:
            dpg.configure_item(self.ui.monitoring_start_button, label="Stop Monitor", 
                             callback=_on_monitoring_stop, user_data=self)
        
        logging.info(f"Monitoring started: {self.monitoring_folder}")
    
    def stop_monitoring(self) -> None:
        """Stop metrics monitoring."""
        if not self.monitoring_active:
            return
        
        self.monitoring_active = False
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=self.monitoring_interval + 2.0)
        
        # Collect garbage after stopping monitoring thread
        gc.collect(generation=0)
        
        # Update button
        if self.ui and self.ui.monitoring_start_button:
            dpg.configure_item(self.ui.monitoring_start_button, label="Start Monitor",
                             callback=_on_monitoring_start, user_data=self)
        
        logging.info("Monitoring stopped")
    
    def set_monitoring_interval(self, interval: float) -> None:
        """Set monitoring interval (minimum 5 seconds)."""
        self.monitoring_interval = max(5.0, float(interval))
    
    def _create_monitoring_files(self) -> None:
        """Create CSV files with headers for each metric group."""
        if not self.monitoring_folder:
            return
        
        # Hardware metrics file (Arduino Due)
        hardware_file = self.monitoring_folder / "hardware_metrics.csv"
        hardware_headers = ["timestamp", "datetime"]
        
        # Add analog channel headers
        for name in sorted(self.analog_specs.keys()):
            hardware_headers.append(name)
        
        # Add SHTC3 headers if present
        if self.shtc3_spec:
            hardware_headers.extend(["temperature_c", "humidity_rh"])
        
        with open(hardware_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(hardware_headers)
        self.monitoring_files["hardware"] = hardware_file
        
        # Image metrics file
        image_file = self.monitoring_folder / "image_metrics.csv"
        image_headers = [
            "timestamp", "datetime", "sequence", "latency_ms", "processing_ms",
            "render_ms", "request_latency_ms", "detections",
            "save_duration_ms", "save_kind", "compression_ratio",
            "storage_codec", "bytes_in", "bytes_out", "throttle_ms"
        ]
        with open(image_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(image_headers)
        self.monitoring_files["image"] = image_file
        
        # SLM metrics file
        slm_file = self.monitoring_folder / "slm_metrics.csv"
        slm_headers = [
            "timestamp", "datetime", "num_points", "generation_ms",
            "last_ack_stage", "last_ack_detail"
        ]
        with open(slm_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(slm_headers)
        self.monitoring_files["slm"] = slm_file
    
    def _monitoring_loop(self) -> None:
        """Background thread loop to periodically write metrics."""
        while self.monitoring_active:
            try:
                self._write_monitoring_snapshot()
            except Exception as exc:
                logging.exception("Error in monitoring loop: %s", exc)
            
            # Sleep in small increments to allow quick shutdown
            sleep_remaining = self.monitoring_interval
            while sleep_remaining > 0 and self.monitoring_active:
                time.sleep(min(0.5, sleep_remaining))
                sleep_remaining -= 0.5
    
    def _write_monitoring_snapshot(self) -> None:
        """Write current metrics snapshot to CSV files."""
        if not self.monitoring_folder or not self.monitoring_files:
            return
        
        current_time = time.time()
        dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        with self.monitoring_lock:
            # Write hardware metrics
            if "hardware" in self.monitoring_files and self.due_manager.connected:
                try:
                    telemetry = self.due_manager.get_telemetry()
                    hardware_row = [current_time, dt_str]
                    
                    # Add analog values
                    for name in sorted(self.analog_specs.keys()):
                        hardware_row.append(telemetry.get(name, ""))
                    
                    # Add SHTC3 values
                    if self.shtc3_spec:
                        hardware_row.append(telemetry.get("SHTC3_TEMPERATURE", ""))
                        hardware_row.append(telemetry.get("SHTC3_HUMIDITY", ""))
                    
                    with open(self.monitoring_files["hardware"], 'a', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(hardware_row)
                except Exception as exc:
                    logging.error(f"Error writing hardware metrics: {exc}")
            
            # Write image metrics
            if "image" in self.monitoring_files and self.image_state.connected:
                try:
                    image_row = [
                        current_time, dt_str,
                        self.image_state.latest_sequence,
                        self.image_state.latest_latency_ms,
                        self.image_state.latest_processing_ms,
                        self.image_state.latest_render_latency_ms,
                        self.image_state.latest_request_latency_ms,
                        self.image_state.detection_count,
                        self.image_state.latest_save_duration_ms,
                        self.image_state.latest_save_kind or "",
                        self.image_state.latest_storage_ratio,
                        self.image_state.latest_storage_codec or "",
                        self.image_state.latest_storage_bytes_in,
                        self.image_state.latest_storage_bytes_out,
                        self.image_state.latest_throttle_ms,
                    ]
                    with open(self.monitoring_files["image"], 'a', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(image_row)
                except Exception as exc:
                    logging.error(f"Error writing image metrics: {exc}")
            
            # Write SLM metrics
            if "slm" in self.monitoring_files and self.slm_client.connected:
                try:
                    gen_ms = ""
                    stage = ""
                    detail = ""
                    
                    if self.slm_client.last_ack:
                        ack = self.slm_client.last_ack
                        stage = getattr(ack, 'stage', "")
                        detail = getattr(ack, 'detail', "")
                        if hasattr(ack, 'metrics') and hasattr(ack.metrics, 'generation_ms'):
                            gen_ms = ack.metrics.generation_ms
                    
                    slm_row = [
                        current_time, dt_str,
                        len(self.slm_points),
                        gen_ms,
                        stage,
                        detail,
                    ]
                    with open(self.monitoring_files["slm"], 'a', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(slm_row)
                except Exception as exc:
                    logging.error(f"Error writing SLM metrics: {exc}")
        
        # Collect garbage after monitoring writes to prevent buildup from CSV operations
        gc.collect(generation=0)

    # Experiment script management
    
    def _build_experiment_context(self) -> ExperimentContext:
        """Build experiment context with current system state."""
        # Get tracked positions from image state
        tracked_positions = []
        if hasattr(self.image_state, 'tracks') and self.image_state.tracks:
            for track in self.image_state.tracks:
                # Track format: {'x': float, 'y': float, 'mass': float, ...}
                if isinstance(track, dict):
                    x = float(track.get('x', 0.0))
                    y = float(track.get('y', 0.0))
                    mass = float(track.get('mass', 0.0))
                    tracked_positions.append((x, y, mass))
        
        # Build context
        ctx = ExperimentContext(
            current_image=self.image_state.latest_image_uint8 if hasattr(self.image_state, 'latest_image_uint8') else None,
            tracked_positions=tracked_positions,
            tracking_metadata={
                'frame_number': self.image_state.latest_sequence if hasattr(self.image_state, 'latest_sequence') else 0,
            },
            frame_number=self.image_state.latest_sequence if hasattr(self.image_state, 'latest_sequence') else 0,
            timestamp=time.time(),
            slm_client=self.slm_client,
            due_manager=self.due_manager,
            slm_points=[(p.x, p.y, p.z, p.intensity) for p in self.slm_points],
            dac_states=self.dac_values.copy(),
            analog_states=self.analog_values.copy(),
            slm_config=self.slm_config_manager.get_current_config(),
            tracking_config=self.tracking_config_manager.get_current_config(),
            feature_config=self.slm_feature_config_manager.get_current_config(),
            metrics_history={},  # Could populate if needed
        )
        
        # Set callback functions
        ctx._set_slm_points_callback = self._experiment_set_slm_points
        ctx._set_dac_value_callback = self._experiment_set_dac_value
        ctx._log_callback = self._experiment_log
        ctx._get_particle_at_callback = None  # Use default implementation
        
        return ctx
    
    def _experiment_set_slm_points(self, points: List[Tuple[float, float, float, float]]) -> None:
        """Callback for experiment scripts to set SLM points."""
        try:
            # Convert to SlmPoint objects
            self.slm_points.clear()
            for x, y, z, intensity in points:
                self.slm_points.append(SlmPoint(x=x, y=y, z=z, intensity=intensity))
            
            # Mark as dirty to trigger send
            self.slm_dirty = True
            
            # Immediately send if connected
            if self.slm_client.connected:
                self.force_send_slm()
                
        except Exception as e:
            logging.error(f"Error setting SLM points from experiment: {e}")
    
    def _experiment_set_dac_value(self, channel: str, value: float) -> None:
        """Callback for experiment scripts to set DAC values."""
        try:
            if channel in self.dac_specs:
                # Use the existing set_dac_value method which handles conversion
                self.set_dac_value(channel, value)
        except Exception as e:
            logging.error(f"Error setting DAC value from experiment: {e}")
    
    def _experiment_log(self, message: str, level: str = "INFO") -> None:
        """Callback for experiment scripts to log messages."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] [{level}] {message}"
        self.experiment_log_messages.append(log_message)
        
        # Also log to standard logger
        if level == "ERROR":
            logging.error(f"Experiment: {message}")
        elif level == "WARNING":
            logging.warning(f"Experiment: {message}")
        elif level == "DEBUG":
            logging.debug(f"Experiment: {message}")
        else:
            logging.info(f"Experiment: {message}")
    
    def _process_experiment_script(self) -> None:
        """Process current frame with experiment script if running."""
        if self.script_manager.current_script:
            ctx = self._build_experiment_context()
            self.script_manager.process_frame(ctx)
    
    def _update_experiment_script_status(self) -> None:
        """Update experiment script status display in UI."""
        if not self.ui or not self.ui.experiment_script_status_text:
            return
        
        status = self.script_manager.get_current_status()
        
        if status['running']:
            script_name = status.get('script_name', 'Unknown')
            frames = status.get('frame_count', 0)
            errors = status.get('error_count', 0)
            paused = status.get('paused', False)
            
            if paused:
                status_text = f"PAUSED: {script_name} (Frames: {frames}, Errors: {errors})"
            else:
                status_text = f"RUNNING: {script_name} (Frames: {frames}, Errors: {errors})"
        else:
            status_text = "No script running"
        
        dpg.set_value(self.ui.experiment_script_status_text, status_text)
        
        # Update log display if we have one
        if self.ui.experiment_script_info_text and self.experiment_log_messages:
            # Show last few log messages
            recent_logs = list(self.experiment_log_messages)[-10:]
            log_text = "\n".join(recent_logs)
            dpg.set_value(self.ui.experiment_script_info_text, log_text)
    
    def _populate_experiment_params_ui(self, script: ExperimentScript) -> None:
        """
        Dynamically generate UI controls for experiment parameters based on ParamSpec.
        
        This method introspects the script's registered parameters and creates
        appropriate UI widgets automatically.
        """
        if not self.ui or not self.ui.experiment_params_container:
            return
        
        # Clear existing parameter widgets
        if dpg.does_item_exist(self.ui.experiment_params_container):
            # Delete all children
            children = dpg.get_item_children(self.ui.experiment_params_container, slot=1)
            if children:
                for child in children:
                    dpg.delete_item(child)
        
        # Clear the widget mapping
        self.ui.experiment_param_widgets = {}
        
        # Get parameter specifications from script
        try:
            param_specs = script.get_param_specs()
        except AttributeError:
            # Script doesn't support auto-config - show message
            dpg.add_text("This script uses manual parameter configuration", 
                        color=(180, 180, 180, 255), 
                        parent=self.ui.experiment_params_container)
            return
        
        if not param_specs:
            # No auto-config parameters - show message
            dpg.add_text("This script has no configurable parameters", 
                        color=(180, 180, 180, 255), 
                        parent=self.ui.experiment_params_container)
            return
        
        # Group parameters by category
        categories: Dict[str, List[Tuple[str, Any]]] = {}
        for name, spec in param_specs.items():
            category = spec.category or "General"
            if category not in categories:
                categories[category] = []
            categories[category].append((name, spec))
        
        # Create UI controls grouped by category
        for category, params in sorted(categories.items()):
            # Category header
            if len(categories) > 1:
                dpg.add_text(category, color=(150, 150, 255, 255), parent=self.ui.experiment_params_container)
                dpg.add_spacing(count=1, parent=self.ui.experiment_params_container)
            
            for param_name, spec in sorted(params, key=lambda x: x[1].label):
                current_value = script.get_param_value(param_name)
                
                # Generate appropriate widget based on type
                if spec.param_type == float:
                    widget_id = dpg.add_input_float(
                        label=f"{spec.label} ({spec.unit})" if spec.unit else spec.label,
                        default_value=current_value if current_value is not None else spec.default,
                        width=150,
                        min_value=spec.min_value if spec.min_value is not None else 0.0,
                        max_value=spec.max_value if spec.max_value is not None else 100.0,
                        min_clamped=spec.min_value is not None,
                        max_clamped=spec.max_value is not None,
                        step=spec.step if spec.step else 0.1,
                        format=spec.format_str,
                        parent=self.ui.experiment_params_container
                    )
                
                elif spec.param_type == int:
                    widget_id = dpg.add_input_int(
                        label=f"{spec.label} ({spec.unit})" if spec.unit else spec.label,
                        default_value=current_value if current_value is not None else spec.default,
                        width=150,
                        min_value=int(spec.min_value) if spec.min_value is not None else 0,
                        max_value=int(spec.max_value) if spec.max_value is not None else 1000,
                        min_clamped=spec.min_value is not None,
                        max_clamped=spec.max_value is not None,
                        step=int(spec.step) if spec.step else 1,
                        parent=self.ui.experiment_params_container
                    )
                
                elif spec.param_type == bool:
                    widget_id = dpg.add_checkbox(
                        label=spec.label,
                        default_value=current_value if current_value is not None else spec.default,
                        parent=self.ui.experiment_params_container
                    )
                
                elif spec.param_type == str:
                    widget_id = dpg.add_input_text(
                        label=spec.label,
                        default_value=current_value if current_value is not None else spec.default,
                        width=150,
                        parent=self.ui.experiment_params_container
                    )
                
                else:
                    # Unsupported type - skip
                    logging.warning(f"Unsupported parameter type for {param_name}: {spec.param_type}")
                    continue
                
                # Store widget ID for later access
                self.ui.experiment_param_widgets[param_name] = widget_id
                
                # Add tooltip if description available
                if spec.description and dpg.does_item_exist(widget_id):
                    with dpg.tooltip(widget_id):
                        dpg.add_text(spec.description, wrap=200)
            
            dpg.add_spacing(count=1, parent=self.ui.experiment_params_container)
        
        # Add apply button
        dpg.add_spacing(count=1, parent=self.ui.experiment_params_container)
        dpg.add_button(
            label="Apply Parameters",
            callback=_on_experiment_params_apply,
            user_data=self,
            width=-1,
            parent=self.ui.experiment_params_container
        )

    # Fine-tuning methods
    
    def start_finetuning(self, sample_count: int, margin_pixels: float) -> None:
        """Start the auto fine-tuning process."""
        logging.info(f"start_finetuning called with samples={sample_count}, margin={margin_pixels}")
        
        # Immediate UI feedback
        if self.ui and self.ui.finetuning_status_text:
            dpg.set_value(self.ui.finetuning_status_text, "Initializing...")
            dpg.configure_item(self.ui.finetuning_status_text, color=SLM_COLOR)
        
        if self.finetuning_state.active:
            logging.warning("Fine-tuning already active")
            if self.ui and self.ui.finetuning_status_text:
                dpg.set_value(self.ui.finetuning_status_text, "ERROR: Already running")
                dpg.configure_item(self.ui.finetuning_status_text, color=STATUS_DISCONNECTED)
            return
        
        if not self.slm_client.connected:
            logging.error("Cannot start fine-tuning: SLM not connected")
            if self.ui and self.ui.finetuning_status_text:
                dpg.set_value(self.ui.finetuning_status_text, "ERROR: SLM not connected")
                dpg.configure_item(self.ui.finetuning_status_text, color=STATUS_DISCONNECTED)
            return
        
        if not self.image_state.connected:
            logging.error("Cannot start fine-tuning: Image server not connected")
            if self.ui and self.ui.finetuning_status_text:
                dpg.set_value(self.ui.finetuning_status_text, "ERROR: Image server not connected")
                dpg.configure_item(self.ui.finetuning_status_text, color=STATUS_DISCONNECTED)
            return
        
        # Reset state
        self.finetuning_state = FineTuningState(
            active=True,
            progress=0.0,
            current_sample=0,
            total_samples=sample_count,
            margin_pixels=margin_pixels,
            base_config_name=self.slm_config_manager.get_current_config().name,
            show_visualization=self.finetuning_state.show_visualization,
            base_affine_params=self.slm_affine_params.copy(),
        )
        self._sync_manual_samples_to_state()
        
        # Reset completion notification flag
        self._last_completion_notified = False
        
        # Start fine-tuning in background thread
        self.finetuning_thread = threading.Thread(target=self._finetuning_worker, daemon=True)
        self.finetuning_thread.start()
        
        logging.info(f"Started fine-tuning with {sample_count} samples, {margin_pixels}px margin")
    
    def stop_finetuning(self) -> None:
        """Stop the fine-tuning process."""
        if not self.finetuning_state.active:
            return
        
        self.finetuning_state.active = False
        logging.info("Stopping fine-tuning...")
    
    def pause_finetuning(self) -> None:
        """Pause/resume the fine-tuning process."""
        if not self.finetuning_state.active:
            return
        
        self.finetuning_state.paused = not self.finetuning_state.paused
        status = "paused" if self.finetuning_state.paused else "resumed"
        logging.info(f"Fine-tuning {status}")
    
    def _finetuning_worker(self) -> None:
        """Worker thread for fine-tuning process."""
        try:
            # Get image dimensions for sampling area
            if self.image_state.latest_image_array is None:
                logging.error("No image available for fine-tuning")
                self.finetuning_state.active = False
                return
            
            img = self.image_state.latest_image_array
            img_height, img_width = img.shape[:2]
            
            # Calculate sampling area with margins
            margin = self.finetuning_state.margin_pixels
            min_x = margin
            min_y = margin
            max_x = img_width - margin
            max_y = img_height - margin
            
            self.finetuning_state.sample_area_min_x = min_x
            self.finetuning_state.sample_area_min_y = min_y
            self.finetuning_state.sample_area_max_x = max_x
            self.finetuning_state.sample_area_max_y = max_y
            
            # Collect samples
            samples: List[FineTuningSample] = []
            
            for i in range(self.finetuning_state.total_samples):
                if not self.finetuning_state.active:
                    break
                
                # Wait if paused
                while self.finetuning_state.paused and self.finetuning_state.active:
                    time.sleep(0.1)
                
                if not self.finetuning_state.active:
                    break
                
                # Generate random point in sampling area
                target_x = np.random.uniform(min_x, max_x)
                target_y = np.random.uniform(min_y, max_y)
                
                # Update UI with current target
                self.finetuning_state.current_target = (target_x, target_y)
                self.finetuning_state.current_sample = i + 1
                self.finetuning_state.progress = (i + 1) / self.finetuning_state.total_samples
                self.finetuning_state.current_track_history = []
                
                # Pin the point
                self.clear_points()
                self.add_point(target_x, target_y)
                self.force_send_slm()
                
                capture_particle, capture_history = self._wait_for_particle_near(
                    target_x,
                    target_y,
                    timeout=3.0,
                    radius=30.0,
                    hold_frames=6,
                    require_active=True,
                )
                self.finetuning_state.current_track_history = list(capture_history)

                if capture_particle is None:
                    logging.warning(
                        "Sample %d: no particle locked near (%.1f, %.1f)",
                        i + 1,
                        target_x,
                        target_y,
                    )
                    self.finetuning_state.current_match = None
                    time.sleep(0.3)
                    continue

                validated, final_particle, validation_history = self._validate_particle_follow(
                    capture_particle,
                    target_x,
                    target_y,
                    radius=35.0,
                    step_size=max(6.0, self.finetuning_state.margin_pixels * 0.1),
                )
                self.finetuning_state.current_track_history.extend(validation_history)

                if not validated or final_particle is None:
                    logging.warning(
                        "Sample %d: trapped particle did not follow the validation sweep",
                        i + 1,
                    )
                    self.finetuning_state.current_match = None
                    time.sleep(0.3)
                    continue

                particle_x, particle_y = final_particle
                error = math.hypot(target_x - particle_x, target_y - particle_y)

                sample = FineTuningSample(
                    slm_x=target_x,
                    slm_y=target_y,
                    particle_x=particle_x,
                    particle_y=particle_y,
                    error=error,
                )
                samples.append(sample)

                # Update visualization
                self.finetuning_state.sample_points.append((target_x, target_y))
                self.finetuning_state.matched_particles.append((particle_x, particle_y))
                self.finetuning_state.errors.append(error)
                self.finetuning_state.current_match = final_particle

                logging.info(
                    "Sample %d/%d captured (error %.2f px)",
                    i + 1,
                    self.finetuning_state.total_samples,
                    error,
                )
                
                # Small delay between samples
                time.sleep(0.5)
            
            # Compute refined calibration
            manual_samples: List[FineTuningSample] = []
            with self.manual_calibration_lock:
                if self.manual_calibration_samples:
                    manual_samples = list(self.manual_calibration_samples)

            combined_samples = samples + manual_samples if manual_samples else samples

            if len(combined_samples) >= 3 and self.finetuning_state.active:
                if manual_samples:
                    logging.info(
                        "Including %d manual calibration samples in refinement (auto=%d)",
                        len(manual_samples),
                        len(samples),
                    )
                self._compute_refined_calibration(combined_samples)
            else:
                logging.error(
                    "Insufficient samples for calibration: %d < 3",
                    len(combined_samples),
                )
                self.finetuning_state.active = False
        
        except Exception as exc:
            logging.exception(f"Fine-tuning error: {exc}")
        finally:
            if self.finetuning_state.active:
                self.finetuning_state.active = False
                self.finetuning_state.progress = 1.0
            
            # Clear the trap
            self.clear_points()
            self.force_send_slm()
            
            # Collect garbage after fine-tuning completes (major operation with many temp objects)
            gc.collect()
    
    def _find_nearest_particle(self, x: float, y: float, max_distance: float = 100.0) -> Optional[Tuple[float, float]]:
        """Find the nearest tracked particle to a given position.
        
        Args:
            x: Target x coordinate
            y: Target y coordinate
            max_distance: Maximum search radius in pixels
            
        Returns:
            Tuple of (particle_x, particle_y) or None if no particle found
        """
        if not hasattr(self.image_state, 'tracks') or not self.image_state.tracks:
            return None
        
        nearest_dist = max_distance
        nearest_particle = None
        
        for track in self.image_state.tracks:
            if isinstance(track, dict):
                px = float(track.get('x', 0.0))
                py = float(track.get('y', 0.0))
                
                dist = np.sqrt((x - px)**2 + (y - py)**2)
                if dist < nearest_dist:
                    nearest_dist = dist
                    nearest_particle = (px, py)
        
        return nearest_particle
    
    def _snapshot_tracks(self) -> List[Dict[str, Any]]:
        """Return a snapshot of the current detection tracks."""
        tracks: List[Dict[str, Any]] = []
        lock = getattr(self.image_state, "lock", None)
        if lock is not None:
            with lock:
                tracks = [dict(track) for track in getattr(self.image_state, "tracks", [])]
        else:
            tracks = [dict(track) for track in getattr(self.image_state, "tracks", [])]
        return tracks

    @staticmethod
    def _nearest_track_position(tracks: Sequence[Dict[str, Any]], x: float, y: float) -> Tuple[Optional[Tuple[float, float]], float]:
        """Compute nearest track position to desired location."""
        closest: Optional[Tuple[float, float]] = None
        best_distance = float("inf")
        for track in tracks:
            try:
                px = float(track.get("x", 0.0))
                py = float(track.get("y", 0.0))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(px) and math.isfinite(py)):
                continue
            distance = math.hypot(px - x, py - y)
            if distance < best_distance:
                best_distance = distance
                closest = (px, py)
        return closest, best_distance

    def _wait_for_particle_near(
        self,
        x: float,
        y: float,
        *,
        timeout: float = 2.5,
        radius: float = 30.0,
        hold_frames: int = 5,
        poll_interval: float = 0.05,
        require_active: bool = False,
    ) -> Tuple[Optional[Tuple[float, float]], List[Tuple[float, float]]]:
        """Track until a particle remains near the target for several frames."""
        start = time.time()
        consecutive = 0
        history: List[Tuple[float, float]] = []
        last_point: Optional[Tuple[float, float]] = None
        while time.time() - start < timeout:
            if require_active and not self.finetuning_state.active:
                break
            tracks = self._snapshot_tracks()
            if not tracks:
                time.sleep(poll_interval)
                continue
            candidate, distance = self._nearest_track_position(tracks, x, y)
            if candidate is None:
                time.sleep(poll_interval)
                continue
            history.append(candidate)
            if distance <= radius:
                if last_point is not None:
                    drift = math.hypot(candidate[0] - last_point[0], candidate[1] - last_point[1])
                    if drift <= max(5.0, radius * 0.2):
                        consecutive += 1
                    else:
                        consecutive = 1
                else:
                    consecutive = 1
                last_point = candidate
            else:
                consecutive = 0
                last_point = None
            if consecutive >= hold_frames:
                return candidate, history
            time.sleep(poll_interval)
        return None, history

    def _validate_particle_follow(
        self,
        captured_particle: Tuple[float, float],
        target_x: float,
        target_y: float,
        *,
        radius: float = 30.0,
        step_size: float = 10.0,
        steps: int = 3,
        timeout: float = 1.5,
    ) -> Tuple[bool, Optional[Tuple[float, float]], List[Tuple[float, float]]]:
        """Move the trap and confirm the same particle follows the commanded motion."""
        if not self.slm_points:
            return False, None, []

        history: List[Tuple[float, float]] = []
        previous_particle = captured_particle
        current_x, current_y = target_x, target_y

        # Determine safe bounds for horizontal movement
        min_x = self.finetuning_state.sample_area_min_x or 0.0
        max_x = self.finetuning_state.sample_area_max_x or (float(self.image_state.latest_image_array.shape[1]) if self.image_state.latest_image_array is not None else target_x + step_size * steps)
        step_direction = 1.0
        if current_x + step_size * steps > max_x:
            step_direction = -1.0
        if current_x + step_direction * step_size * steps < min_x:
            step_direction = 1.0  # fallback to positive direction if clamped region is too tight

        success = True
        for _ in range(steps):
            next_x = current_x + step_direction * step_size
            next_y = current_y
            next_x = max(min_x, min(max_x, next_x))

            self.move_point(0, next_x, next_y)
            self.force_send_slm()
            time.sleep(0.15)

            particle, step_history = self._wait_for_particle_near(
                next_x,
                next_y,
                timeout=timeout,
                radius=radius,
                hold_frames=4,
                require_active=True,
            )
            history.extend(step_history)
            if particle is None:
                success = False
                break

            commanded_delta = math.hypot(next_x - current_x, next_y - current_y)
            actual_delta = math.hypot(particle[0] - previous_particle[0], particle[1] - previous_particle[1])
            if commanded_delta >= 1.0:
                # Allow generous slack but ensure motion is correlated with the trap move
                if abs(actual_delta - commanded_delta) > max(3.0, commanded_delta * 0.75):
                    success = False
                    break

            previous_particle = particle
            current_x, current_y = next_x, next_y

        # Return to the original location
        self.move_point(0, target_x, target_y)
        self.force_send_slm()
        time.sleep(0.15)
        final_particle, final_history = self._wait_for_particle_near(
            target_x,
            target_y,
            timeout=timeout,
            radius=radius,
            hold_frames=4,
            require_active=True,
        )
        history.extend(final_history)

        if not success or final_particle is None:
            return False, final_particle if success else None, history
        return True, final_particle, history

    def _compute_refined_calibration(self, samples: List[FineTuningSample]) -> None:
        """Compute refined calibration from fine-tuning samples."""
        from slm_config.slm_finetuning_manager import (
            compute_affine_transform,
            transform_points,
        )

        if not samples:
            logging.error("Fine-tuning did not collect any samples")
            return

        base_params = dict(self.finetuning_state.base_affine_params or self.slm_affine_params)
        required_keys = {
            "cam_x0", "cam_y0", "slm_x0", "slm_y0",
            "cam_x1", "cam_y1", "slm_x1", "slm_y1",
            "cam_x2", "cam_y2", "slm_x2", "slm_y2",
        }
        if not required_keys.issubset(base_params):
            missing = sorted(required_keys.difference(base_params))
            logging.error("Base affine parameters missing keys: %s", ", ".join(missing))
            return

        # Build baseline camera→SLM mapping from the active configuration.
        base_camera_points = np.array([
            [base_params["cam_x0"], base_params["cam_y0"]],
            [base_params["cam_x1"], base_params["cam_y1"]],
            [base_params["cam_x2"], base_params["cam_y2"]],
        ], dtype=np.float64)
        base_slm_points = np.array([
            [base_params["slm_x0"], base_params["slm_y0"]],
            [base_params["slm_x1"], base_params["slm_y1"]],
            [base_params["slm_x2"], base_params["slm_y2"]],
        ], dtype=np.float64)

        try:
            base_affine_matrix, _ = compute_affine_transform(base_camera_points, base_slm_points)
        except ValueError as exc:
            logging.exception("Failed to compute baseline affine matrix: %s", exc)
            return

        # Measurements: desired image coordinates (targets), detected particle coordinates, and
        # the SLM coordinates actually issued by the current calibration.
        desired_points = np.array([[s.slm_x, s.slm_y] for s in samples], dtype=np.float64)
        particle_points = np.array([[s.particle_x, s.particle_y] for s in samples], dtype=np.float64)
        commanded_slm_points = transform_points(desired_points, base_affine_matrix)

        # Error metrics before refinement (camera space).
        errors_before = np.linalg.norm(desired_points - particle_points, axis=1)
        rms_before = float(np.sqrt(np.mean(errors_before ** 2))) if len(errors_before) else 0.0
        max_before = float(np.max(errors_before)) if len(errors_before) else 0.0
        mean_before = float(np.mean(errors_before)) if len(errors_before) else 0.0

        try:
            slm_to_camera_matrix, rms_slm_to_camera = compute_affine_transform(
                commanded_slm_points, particle_points
            )
            camera_to_slm_matrix, _ = compute_affine_transform(
                particle_points, commanded_slm_points
            )
        except ValueError as exc:
            logging.exception("Fine-tuning failed to fit affine mappings: %s", exc)
            return

        if not (np.isfinite(camera_to_slm_matrix).all() and np.isfinite(slm_to_camera_matrix).all()):
            logging.error("Computed affine matrices contain non-finite values; aborting fine-tuning")
            return

        # Predict post-correction camera error by composing the new mapping with the
        # observed SLM→camera transform.
        predicted_slm_for_targets = transform_points(desired_points, camera_to_slm_matrix)
        predicted_camera_after = transform_points(predicted_slm_for_targets, slm_to_camera_matrix)
        errors_after_camera = np.linalg.norm(predicted_camera_after - desired_points, axis=1)
        rms_after = float(np.sqrt(np.mean(errors_after_camera ** 2))) if len(errors_after_camera) else 0.0
        max_after = float(np.max(errors_after_camera)) if len(errors_after_camera) else 0.0
        mean_after = float(np.mean(errors_after_camera)) if len(errors_after_camera) else 0.0

        # Derive refined legacy parameters by adjusting the existing reference camera points.
        refined_slm_points = transform_points(base_camera_points, camera_to_slm_matrix)
        refined_params = {
            "refined_cam_x0": float(base_camera_points[0, 0]),
            "refined_cam_y0": float(base_camera_points[0, 1]),
            "refined_slm_x0": float(refined_slm_points[0, 0]),
            "refined_slm_y0": float(refined_slm_points[0, 1]),
            "refined_cam_x1": float(base_camera_points[1, 0]),
            "refined_cam_y1": float(base_camera_points[1, 1]),
            "refined_slm_x1": float(refined_slm_points[1, 0]),
            "refined_slm_y1": float(refined_slm_points[1, 1]),
            "refined_cam_x2": float(base_camera_points[2, 0]),
            "refined_cam_y2": float(base_camera_points[2, 1]),
            "refined_slm_x2": float(refined_slm_points[2, 0]),
            "refined_slm_y2": float(refined_slm_points[2, 1]),
        }

        slm_residuals = np.linalg.norm(
            transform_points(particle_points, camera_to_slm_matrix) - commanded_slm_points,
            axis=1,
        )
        logging.info(
            "Fine-tuning fit summary: rms_before=%.2fpx, rms_after=%.2fpx, "
            "slm_to_camera_rms=%.2fpx, residual_slm=%.2fpx",
            rms_before,
            rms_after,
            float(rms_slm_to_camera),
            float(np.sqrt(np.mean(slm_residuals ** 2))) if len(slm_residuals) else 0.0,
        )

        # Create fine-tuning result artifact.
        result = FineTuningResult(
            name=self.finetuning_manager.generate_name(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            base_config_name=self.finetuning_state.base_config_name,
            sample_count=len(samples),
            margin_pixels=self.finetuning_state.margin_pixels,
            sample_area_min_x=self.finetuning_state.sample_area_min_x,
            sample_area_min_y=self.finetuning_state.sample_area_min_y,
            sample_area_max_x=self.finetuning_state.sample_area_max_x,
            sample_area_max_y=self.finetuning_state.sample_area_max_y,
            samples=[{
                "slm_x": s.slm_x,
                "slm_y": s.slm_y,
                "particle_x": s.particle_x,
                "particle_y": s.particle_y,
                "error": s.error,
            } for s in samples],
            rms_error_before=rms_before,
            rms_error_after=rms_after,
            max_error_before=max_before,
            max_error_after=max_after,
            mean_error_before=mean_before,
            mean_error_after=mean_after,
            **refined_params,
            description=f"Auto-generated fine-tuning based on {self.finetuning_state.base_config_name}",
        )

        if self.finetuning_manager.save_result(result):
            improvement = result.get_improvement_percentage()
            logging.info(
                "Fine-tuning complete! RMS error: %.2fpx → %.2fpx (%.1f%% improvement)",
                rms_before,
                rms_after,
                improvement,
            )
            logging.info("Saved as: %s", result.name)

            if self.ui and self.ui.finetuning_config_combo:
                configs = self.list_finetuning_configs()
                if configs:
                    dpg.configure_item(
                        self.ui.finetuning_config_combo,
                        items=configs,
                        default_value=configs[0],
                    )
                    _update_finetuning_metadata_display(self)
                else:
                    dpg.configure_item(self.ui.finetuning_config_combo, items=["No fine-tuned configs"])
                logging.info("Updated combo box with %d configs", len(configs))
        else:
            logging.error("Failed to save fine-tuning result")
    
    def load_finetuning_config(self, name: str) -> bool:
        """Load a fine-tuning configuration as the active SLM config."""
        result = self.finetuning_manager.load_result(name)
        if not result:
            logging.error(f"Fine-tuning config '{name}' not found")
            return False
        
        # Update affine parameters
        self.slm_affine_params.update({
            'cam_x0': result.refined_cam_x0,
            'cam_y0': result.refined_cam_y0,
            'slm_x0': result.refined_slm_x0,
            'slm_y0': result.refined_slm_y0,
            'cam_x1': result.refined_cam_x1,
            'cam_y1': result.refined_cam_y1,
            'slm_x1': result.refined_slm_x1,
            'slm_y1': result.refined_slm_y1,
            'cam_x2': result.refined_cam_x2,
            'cam_y2': result.refined_cam_y2,
            'slm_x2': result.refined_slm_x2,
            'slm_y2': result.refined_slm_y2,
        })
        
        # Update UI
        self._update_slm_affine_ui()
        
        # Mark SLM as dirty to trigger update
        self._mark_slm_dirty()
        
        logging.info(f"Loaded fine-tuning config: {name}")
        return True

    # Manual calibration helpers

    def _set_manual_status(self, message: str, color: Tuple[int, int, int, int] = TEXT_PRIMARY) -> None:
        if self.ui and self.ui.manual_status_text:
            dpg.set_value(self.ui.manual_status_text, message)
            dpg.configure_item(self.ui.manual_status_text, color=color)

    def _update_manual_calibration_ui(self) -> None:
        if not self.ui:
            return
        count = 0
        with self.manual_calibration_lock:
            count = len(self.manual_calibration_samples)
        if self.ui.manual_sample_count_text:
            dpg.set_value(self.ui.manual_sample_count_text, f"Manual samples: {count}")
        if self.ui.manual_apply_button:
            dpg.configure_item(self.ui.manual_apply_button, enabled=count >= 3)
        if self.ui.manual_clear_button:
            dpg.configure_item(self.ui.manual_clear_button, enabled=count > 0)

    def _sync_manual_samples_to_state(self) -> None:
        """Replicate stored manual samples into the active fine-tuning state for visualization."""
        with self.manual_calibration_lock:
            samples_copy = list(self.manual_calibration_samples)
        self.finetuning_state.manual_sample_points = [(s.slm_x, s.slm_y) for s in samples_copy]
        self.finetuning_state.manual_matched_particles = [(s.particle_x, s.particle_y) for s in samples_copy]
        self.finetuning_state.manual_errors = [s.error for s in samples_copy]

    def capture_manual_calibration_sample(self, point_index: int = 0) -> bool:
        """Capture a manual camera↔SLM mapping sample based on the active trap."""
        if not self.slm_points:
            logging.warning("Manual calibration requires at least one SLM point")
            self._set_manual_status("ERROR: Add an SLM point first", STATUS_DISCONNECTED)
            return False

        if not self.slm_client.connected:
            logging.warning("Manual calibration requires an active SLM connection")
            self._set_manual_status("ERROR: SLM not connected", STATUS_DISCONNECTED)
            return False

        if not self.image_state.connected:
            logging.warning("Manual calibration requires the image server connection")
            self._set_manual_status("ERROR: Image server not connected", STATUS_DISCONNECTED)
            return False

        idx = max(0, min(point_index, len(self.slm_points) - 1))
        point = self.slm_points[idx]

        particle, history = self._wait_for_particle_near(
            point.x,
            point.y,
            timeout=3.0,
            radius=28.0,
            hold_frames=6,
            require_active=False,
        )
        self.finetuning_state.current_track_history = list(history)
        if particle is None:
            logging.warning("Manual calibration: no particle detected near (%.1f, %.1f)", point.x, point.y)
            self._set_manual_status("No tracked particle at trap", STATUS_DISCONNECTED)
            self.finetuning_state.current_match = None
            return False

        self.finetuning_state.current_match = particle
        error = math.hypot(point.x - particle[0], point.y - particle[1])
        sample = FineTuningSample(
            slm_x=point.x,
            slm_y=point.y,
            particle_x=particle[0],
            particle_y=particle[1],
            error=error,
        )
        with self.manual_calibration_lock:
            self.manual_calibration_samples.append(sample)
            count = len(self.manual_calibration_samples)
        self._sync_manual_samples_to_state()

        logging.info("Manual calibration sample #%d stored (error %.2f px)", count, error)
        self._set_manual_status(f"Captured manual sample #{count}", STATUS_CONNECTED)
        self._update_manual_calibration_ui()
        return True

    def clear_manual_calibration_samples(self) -> None:
        """Reset stored manual calibration samples."""
        with self.manual_calibration_lock:
            self.manual_calibration_samples.clear()
        self.finetuning_state.manual_sample_points.clear()
        self.finetuning_state.manual_matched_particles.clear()
        self.finetuning_state.manual_errors.clear()
        self._sync_manual_samples_to_state()
        self._set_manual_status("Manual samples cleared", TEXT_SECONDARY)
        self._update_manual_calibration_ui()

    def apply_manual_calibration(self) -> bool:
        """Apply calibration refinement using only manual samples."""
        with self.manual_calibration_lock:
            samples = list(self.manual_calibration_samples)

        if len(samples) < 3:
            logging.warning("Need at least three manual samples to compute calibration (have %d)", len(samples))
            self._set_manual_status("Need ≥3 manual samples", STATUS_DISCONNECTED)
            return False

        current_config = self.slm_config_manager.get_current_config()
        self.finetuning_state.base_config_name = current_config.name
        self.finetuning_state.base_affine_params = self.slm_affine_params.copy()
        self.finetuning_state.margin_pixels = 0.0
        self.finetuning_state.total_samples = len(samples)
        if self.image_state.latest_image_array is not None:
            height, width = self.image_state.latest_image_array.shape[:2]
            self.finetuning_state.sample_area_min_x = 0.0
            self.finetuning_state.sample_area_min_y = 0.0
            self.finetuning_state.sample_area_max_x = float(width)
            self.finetuning_state.sample_area_max_y = float(height)

        self._sync_manual_samples_to_state()
        self._compute_refined_calibration(samples)
        self._set_manual_status("Manual calibration saved", STATUS_CONNECTED)
        return True
    
    def delete_finetuning_config(self, name: str) -> bool:
        """Delete a fine-tuning configuration."""
        return self.finetuning_manager.delete_result(name)
    
    def list_finetuning_configs(self) -> List[str]:
        """List all fine-tuning configurations."""
        configs = self.finetuning_manager.list_results()
        logging.debug(f"list_finetuning_configs returned {len(configs)} configs: {configs}")
        return configs
    
    def get_finetuning_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a fine-tuning configuration."""
        metadata = self.finetuning_manager.get_result_metadata(name)
        logging.debug(f"get_finetuning_metadata for '{name}': {metadata}")
        return metadata

    def shutdown(self) -> None:
        """Shutdown all connections."""
        # Stop experiment script if running
        if self.script_manager.current_script:
            ctx = self._build_experiment_context()
            self.script_manager.stop_script(ctx)
        
        self.stop_monitoring()
        
        # Safety: Set objective heater and laser power to zero before shutdown
        if self.due_manager.connected:
            try:
                # Set laser power to zero (DAC0)
                laser_spec = self._get_dac_spec("LASER_POWER_CONTROL_DAC_PIN")
                self.due_manager.write_dac(laser_spec, 0.0)
                logging.info("Set laser power to 0 before shutdown")
            except Exception as e:
                logging.error(f"Failed to set laser power to zero on shutdown: {e}")
            
            try:
                # Set objective heater to zero (DAC1)
                heater_spec = self._get_dac_spec("OBJECTIVE_HEATER_CONTROL_DAC_PIN")
                self.due_manager.write_dac(heater_spec, 0.0)
                logging.info("Set objective heater to 0 before shutdown")
            except Exception as e:
                logging.error(f"Failed to set objective heater to zero on shutdown: {e}")
        
        # Perform garbage collection before shutdown for clean exit
        gc.collect()
        
        self.due_manager.shutdown()
        self.slm_client.shutdown()
        self.image_client.disconnect()


# DearPyGui UI construction


def _on_image_clicked(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle mouse click on image for SLM point placement."""
    controller = user_data
    if not controller.slm_client.connected:
        return
    
    # Get mouse position relative to the image widget
    mouse_pos = dpg.get_mouse_pos(local=False)
    
    # Get image widget position using rect for accurate positioning
    if controller.ui and dpg.does_item_exist(controller.ui.image_item):
        item_rect_min = dpg.get_item_rect_min(controller.ui.image_item)
        
        # Calculate position relative to the image item
        mouse_x = mouse_pos[0] - item_rect_min[0]
        mouse_y = mouse_pos[1] - item_rect_min[1]
        
        # Log for debugging
        logging.debug(f"Click: mouse_pos={mouse_pos}, item_min={item_rect_min}, relative=({mouse_x:.1f}, {mouse_y:.1f})")
        
        # Determine button: left = 0, right = 1
        button = 0 if dpg.is_mouse_button_down(dpg.mvMouseButton_Left) else 1
        controller.handle_image_click(button, mouse_x, mouse_y)


def _on_image_dragged(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle mouse drag on image for moving SLM points."""
    controller = user_data
    if not controller.slm_client.connected:
        return
    
    # Get mouse position relative to the image widget
    mouse_pos = dpg.get_mouse_pos(local=False)
    
    if controller.ui and dpg.does_item_exist(controller.ui.image_item):
        item_rect_min = dpg.get_item_rect_min(controller.ui.image_item)
        mouse_x = mouse_pos[0] - item_rect_min[0]
        mouse_y = mouse_pos[1] - item_rect_min[1]
        
        controller.handle_image_drag(mouse_x, mouse_y)
        # Update cursor position while dragging
        controller.update_mouse_position(mouse_x, mouse_y)


def _on_image_released(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle mouse release after dragging."""
    controller = user_data
    controller.handle_image_release()


def _on_image_hover(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle mouse hover over image to update cursor position."""
    controller = user_data
    
    # Get mouse position relative to the image widget
    mouse_pos = dpg.get_mouse_pos(local=False)
    
    if controller.ui and dpg.does_item_exist(controller.ui.image_item):
        item_rect_min = dpg.get_item_rect_min(controller.ui.image_item)
        mouse_x = mouse_pos[0] - item_rect_min[0]
        mouse_y = mouse_pos[1] - item_rect_min[1]
        
        controller.update_mouse_position(mouse_x, mouse_y)


def _on_image_connect(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data

    def connect_thread() -> None:
        try:
            controller.connect_image()
            if controller.ui:
                if controller.ui.image_connect_button:
                    dpg.configure_item(controller.ui.image_connect_button,
                                       label="Disconnect Image",
                                       callback=_on_image_disconnect,
                                       user_data=controller)
                if controller.ui.image_connection_status_label:
                    dpg.set_value(controller.ui.image_connection_status_label,
                                  f"Connected to {controller.image_endpoint.display()}")
                    dpg.configure_item(controller.ui.image_connection_status_label, color=STATUS_CONNECTED)
        except Exception as exc:
            logging.exception("Failed to connect to image server: %s", exc)

    threading.Thread(target=connect_thread, daemon=True).start()


def _on_image_disconnect(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    controller.disconnect_image()
    # Update button - back to default connect button
    if controller.ui and controller.ui.image_connect_button:
        dpg.configure_item(controller.ui.image_connect_button, 
                          label="Connect Image",
                          callback=_on_image_connect,
                          user_data=controller)
    if controller.ui and controller.ui.image_connection_status_label:
        dpg.set_value(controller.ui.image_connection_status_label, "Disconnected")
        dpg.configure_item(controller.ui.image_connection_status_label, color=STATUS_DISCONNECTED)


def _on_due_connect(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data

    def connect_thread() -> None:
        try:
            controller.connect_due()
            if controller.ui:
                if controller.ui.due_connect_button:
                    dpg.configure_item(controller.ui.due_connect_button,
                                       label="Disconnect Due",
                                       callback=_on_due_disconnect,
                                       user_data=controller)
                if controller.ui.due_connection_status_label:
                    dpg.set_value(controller.ui.due_connection_status_label,
                                  f"Connected to {controller.due_endpoint.display()}")
                    dpg.configure_item(controller.ui.due_connection_status_label, color=STATUS_CONNECTED)
        except Exception as exc:
            logging.exception("Failed to connect to Due: %s", exc)

    threading.Thread(target=connect_thread, daemon=True).start()


def _on_due_disconnect(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    controller.disconnect_due()
    # Update button
    if controller.ui and controller.ui.due_connect_button:
        dpg.configure_item(controller.ui.due_connect_button, 
                          label="Connect Due",
                          callback=_on_due_connect,
                          user_data=controller)
    if controller.ui and controller.ui.due_connection_status_label:
        dpg.set_value(controller.ui.due_connection_status_label, "Disconnected")
        dpg.configure_item(controller.ui.due_connection_status_label, color=STATUS_DISCONNECTED)


def _on_slm_connect(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data

    def connect_thread() -> None:
        try:
            controller.connect_slm()
            if controller.ui:
                if controller.ui.slm_connect_button:
                    dpg.configure_item(controller.ui.slm_connect_button,
                                       label="Disconnect SLM",
                                       callback=_on_slm_disconnect,
                                       user_data=controller)
                if controller.ui.slm_connection_status_label:
                    dpg.set_value(controller.ui.slm_connection_status_label,
                                  f"Connected to {controller.slm_endpoint.display()}")
                    dpg.configure_item(controller.ui.slm_connection_status_label, color=STATUS_CONNECTED)
        except Exception as exc:
            logging.exception("Failed to connect to SLM: %s", exc)

    threading.Thread(target=connect_thread, daemon=True).start()


def _on_slm_disconnect(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    controller.disconnect_slm()
    # Update button
    if controller.ui and controller.ui.slm_connect_button:
        dpg.configure_item(controller.ui.slm_connect_button, 
                          label="Connect SLM",
                          callback=_on_slm_connect,
                          user_data=controller)
    if controller.ui and controller.ui.slm_connection_status_label:
        dpg.set_value(controller.ui.slm_connection_status_label, "Disconnected")
        dpg.configure_item(controller.ui.slm_connection_status_label, color=STATUS_DISCONNECTED)


def _on_dac_adjust(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str, int, str]) -> None:
    controller, name, direction, increment_item = user_data
    try:
        step = float(dpg.get_value(increment_item) or 0.01)
    except Exception:
        step = 0.01
    
    delta = step * direction
    try:
        new_value = controller.adjust_dac(name, delta)
        # Update UI
        if controller.ui and name in controller.ui.dac_items:
            label_id, _ = controller.ui.dac_items[name]
            spec = controller.dac_specs[name]
            dpg.set_value(label_id, f"{spec.label}: {new_value:.4f} {spec.unit}")
    except Exception as exc:
        logging.exception("Failed to adjust DAC: %s", exc)


def _on_slm_send(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Force send current SLM points."""
    user_data.force_send_slm()


def _on_slm_clear(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Clear all SLM points."""
    user_data.clear_points()
    user_data.force_send_slm()  # Send empty point list


def _on_slm_affine_changed(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Handle affine parameter change."""
    controller, param_name = user_data
    value = dpg.get_value(sender)
    controller.update_slm_affine(param_name, value)


# SLM Configuration Callbacks

def _on_slm_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle saving current SLM configuration."""
    controller = user_data
    
    def save_config_dialog():
        with dpg.window(label="Save SLM Configuration", modal=True, tag="slm_save_config_dialog"):
            dpg.add_text("Save current SLM configuration:")
            dpg.add_input_text(label="Name", tag="slm_config_name_input", default_value="")
            dpg.add_input_text(label="Description", tag="slm_config_desc_input", default_value="", multiline=True, height=60)
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=_confirm_slm_config_save, user_data=controller)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("slm_save_config_dialog"))
    
    save_config_dialog()


def _confirm_slm_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Confirm and save SLM configuration."""
    controller = user_data
    
    name = dpg.get_value("slm_config_name_input").strip()
    description = dpg.get_value("slm_config_desc_input").strip()
    
    if not name:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="slm_config_error_dialog"):
            dpg.add_text("Configuration name cannot be empty.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("slm_config_error_dialog"))
        return
    
    success = controller.save_slm_config(name, description)
    dpg.delete_item("slm_save_config_dialog")
    
    if success:
        # Update the configuration dropdown if it exists
        if controller.ui and controller.ui.slm_config_combo:
            configs = controller.list_slm_configs()
            dpg.configure_item(controller.ui.slm_config_combo, items=configs, default_value=name)
    else:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="slm_config_save_error_dialog"):
            dpg.add_text(f"Failed to save configuration '{name}'.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("slm_config_save_error_dialog"))


def _on_slm_config_load(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle loading SLM configuration from dropdown."""
    controller = user_data
    if controller.ui and controller.ui.slm_config_combo:
        selected_config = dpg.get_value(controller.ui.slm_config_combo)
        controller.load_slm_config(selected_config)


def _on_slm_config_set_default(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle setting current configuration as default."""
    controller = user_data
    if controller.ui and controller.ui.slm_config_combo:
        selected_config = dpg.get_value(controller.ui.slm_config_combo)
        controller.set_default_slm_config(selected_config)


def _on_slm_config_reset(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle resetting SLM configuration to default."""
    controller = user_data
    controller.reset_slm_config_to_default()
    
    # Update the dropdown selection
    if controller.ui and controller.ui.slm_config_combo:
        dpg.set_value(controller.ui.slm_config_combo, "default")


def _on_slm_config_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle deleting selected SLM configuration."""
    controller = user_data
    if controller.ui and controller.ui.slm_config_combo:
        selected_config = dpg.get_value(controller.ui.slm_config_combo)
        
        if selected_config == "default":
            # Show error - cannot delete default
            with dpg.window(label="Error", modal=True, tag="slm_config_delete_error_dialog"):
                dpg.add_text("Cannot delete the default configuration.")
                dpg.add_button(label="OK", callback=lambda: dpg.delete_item("slm_config_delete_error_dialog"))
            return
        
        # Confirmation dialog
        def confirm_delete():
            with dpg.window(label="Confirm Delete", modal=True, tag="slm_config_delete_confirm_dialog"):
                dpg.add_text(f"Are you sure you want to delete configuration '{selected_config}'?")
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Delete", callback=_confirm_slm_config_delete, user_data=(controller, selected_config))
                    dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("slm_config_delete_confirm_dialog"))
        
        confirm_delete()


def _confirm_slm_config_delete(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Confirm deletion of SLM configuration."""
    controller, config_name = user_data
    
    success = controller.delete_slm_config(config_name)
    dpg.delete_item("slm_config_delete_confirm_dialog")
    
    if success:
        # Update the dropdown
        if controller.ui and controller.ui.slm_config_combo:
            configs = controller.list_slm_configs()
            current_config = controller.get_current_slm_config_name()
            dpg.configure_item(controller.ui.slm_config_combo, items=configs, default_value=current_config)
    else:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="slm_config_delete_error_dialog"):
            dpg.add_text(f"Failed to delete configuration '{config_name}'.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("slm_config_delete_error_dialog"))


# Feature Configuration Callbacks

def _on_feature_apodization_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    value = bool(dpg.get_value(sender))
    controller.set_apodization_enabled(value)


def _on_feature_apodization_strength(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    strength = float(dpg.get_value(sender))
    controller.set_apodization_strength(strength)


def _on_feature_z_focus_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    value = bool(dpg.get_value(sender))
    controller.set_z_focus_enabled(value)


def _on_feature_z_focus_offset(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    offset = float(dpg.get_value(sender))
    controller.set_z_focus_offset(offset)


def _on_feature_z_focus_scale(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    scale = float(dpg.get_value(sender))
    controller.set_z_focus_scale(scale)


def _on_feature_default_point_z(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    default_z = float(dpg.get_value(sender))
    controller.set_default_point_z(default_z)


def _on_feature_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data

    def open_dialog() -> None:
        with dpg.window(label="Save Feature Configuration", modal=True, tag="feature_config_save_dialog"):
            dpg.add_text("Save current feature configuration:")
            dpg.add_input_text(label="Name", tag="feature_config_name_input", default_value="")
            dpg.add_input_text(label="Description", tag="feature_config_desc_input", default_value="", multiline=True, height=60)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=_confirm_feature_config_save, user_data=controller)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("feature_config_save_dialog"))

    open_dialog()


def _confirm_feature_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data

    name = dpg.get_value("feature_config_name_input").strip()
    description = dpg.get_value("feature_config_desc_input").strip()

    if not name:
        with dpg.window(label="Error", modal=True, tag="feature_config_error_dialog"):
            dpg.add_text("Configuration name cannot be empty.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("feature_config_error_dialog"))
        return

    success = controller.save_feature_config(name, description)
    dpg.delete_item("feature_config_save_dialog")

    if success and controller.ui and controller.ui.feature_config_combo:
        configs = controller.list_feature_configs()
        dpg.configure_item(controller.ui.feature_config_combo, items=configs, default_value=name)
        controller.load_feature_config(name)
    elif not success:
        with dpg.window(label="Error", modal=True, tag="feature_config_save_error_dialog"):
            dpg.add_text(f"Failed to save feature configuration '{name}'.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("feature_config_save_error_dialog"))


def _on_feature_config_load(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    if controller.ui and controller.ui.feature_config_combo:
        selected = dpg.get_value(controller.ui.feature_config_combo)
        controller.load_feature_config(selected)


def _on_feature_config_set_default(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    if controller.ui and controller.ui.feature_config_combo:
        selected = dpg.get_value(controller.ui.feature_config_combo)
        controller.set_default_feature_config(selected)


def _on_feature_config_reset(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    controller.reset_feature_config_to_default()
    if controller.ui and controller.ui.feature_config_combo:
        dpg.set_value(controller.ui.feature_config_combo, "default")


def _on_feature_config_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    if controller.ui and controller.ui.feature_config_combo:
        selected = dpg.get_value(controller.ui.feature_config_combo)
        if selected == "default":
            with dpg.window(label="Error", modal=True, tag="feature_config_delete_error_dialog"):
                dpg.add_text("Cannot delete the default configuration.")
                dpg.add_button(label="OK", callback=lambda: dpg.delete_item("feature_config_delete_error_dialog"))
            return

        def confirm() -> None:
            with dpg.window(label="Confirm Delete", modal=True, tag="feature_config_delete_confirm_dialog"):
                dpg.add_text(f"Delete feature configuration '{selected}'?")
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Delete", callback=_confirm_feature_config_delete, user_data=(controller, selected))
                    dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("feature_config_delete_confirm_dialog"))

        confirm()


def _confirm_feature_config_delete(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    controller, name = user_data
    success = controller.delete_feature_config(name)
    dpg.delete_item("feature_config_delete_confirm_dialog")

    if success and controller.ui and controller.ui.feature_config_combo:
        configs = controller.list_feature_configs()
        dpg.configure_item(controller.ui.feature_config_combo, items=configs, default_value="default")
        controller.load_feature_config(controller.get_current_feature_config_name())
    elif not success:
        with dpg.window(label="Error", modal=True, tag="feature_config_delete_error_dialog"):
            dpg.add_text(f"Failed to delete feature configuration '{name}'.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("feature_config_delete_error_dialog"))


# Tracking Configuration Callbacks

def _on_tracking_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle saving current tracking configuration."""
    controller = user_data
    
    def save_config_dialog():
        with dpg.window(label="Save Tracking Configuration", modal=True, tag="tracking_save_config_dialog"):
            dpg.add_text("Save current tracking configuration:")
            dpg.add_input_text(label="Name", tag="tracking_config_name_input", default_value="")
            dpg.add_input_text(label="Description", tag="tracking_config_desc_input", default_value="", multiline=True, height=60)
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=_confirm_tracking_config_save, user_data=controller)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("tracking_save_config_dialog"))
    
    save_config_dialog()


def _confirm_tracking_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Confirm and save tracking configuration."""
    controller = user_data
    
    name = dpg.get_value("tracking_config_name_input").strip()
    description = dpg.get_value("tracking_config_desc_input").strip()
    
    if not name:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="tracking_config_error_dialog"):
            dpg.add_text("Configuration name cannot be empty.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("tracking_config_error_dialog"))
        return
    
    success = controller.save_tracking_config(name, description)
    dpg.delete_item("tracking_save_config_dialog")
    
    if success:
        # Update the configuration dropdown if it exists
        if controller.ui and controller.ui.tracking_config_combo:
            configs = controller.list_tracking_configs()
            dpg.configure_item(controller.ui.tracking_config_combo, items=configs, default_value=name)
    else:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="tracking_config_save_error_dialog"):
            dpg.add_text(f"Failed to save configuration '{name}'.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("tracking_config_save_error_dialog"))


def _on_tracking_config_load(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle loading tracking configuration from dropdown."""
    controller = user_data
    if controller.ui and controller.ui.tracking_config_combo:
        selected_config = dpg.get_value(controller.ui.tracking_config_combo)
        controller.load_tracking_config(selected_config)


def _on_tracking_config_set_default(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle setting current configuration as default."""
    controller = user_data
    if controller.ui and controller.ui.tracking_config_combo:
        selected_config = dpg.get_value(controller.ui.tracking_config_combo)
        controller.set_default_tracking_config(selected_config)


def _on_tracking_config_reset(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle resetting tracking configuration to default."""
    controller = user_data
    controller.reset_tracking_config_to_default()
    
    # Update the dropdown selection
    if controller.ui and controller.ui.tracking_config_combo:
        dpg.set_value(controller.ui.tracking_config_combo, "default")


def _on_tracking_config_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle deleting selected tracking configuration."""
    controller = user_data
    if controller.ui and controller.ui.tracking_config_combo:
        selected_config = dpg.get_value(controller.ui.tracking_config_combo)
        
        if selected_config == "default":
            # Show error - cannot delete default
            with dpg.window(label="Error", modal=True, tag="tracking_config_delete_error_dialog"):
                dpg.add_text("Cannot delete the default configuration.")
                dpg.add_button(label="OK", callback=lambda: dpg.delete_item("tracking_config_delete_error_dialog"))
            return
        
        # Confirmation dialog
        def confirm_delete():
            with dpg.window(label="Confirm Delete", modal=True, tag="tracking_config_delete_confirm_dialog"):
                dpg.add_text(f"Are you sure you want to delete configuration '{selected_config}'?")
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Delete", callback=_confirm_tracking_config_delete, user_data=(controller, selected_config))
                    dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("tracking_config_delete_confirm_dialog"))
        
        confirm_delete()


def _confirm_tracking_config_delete(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Confirm deletion of tracking configuration."""
    controller, config_name = user_data
    
    success = controller.delete_tracking_config(config_name)
    dpg.delete_item("tracking_config_delete_confirm_dialog")
    
    if success:
        # Update the dropdown and load the current config
        if controller.ui and controller.ui.tracking_config_combo:
            configs = controller.list_tracking_configs()
            current_config = controller.get_current_tracking_config_name()
            dpg.configure_item(controller.ui.tracking_config_combo, items=configs, default_value=current_config)
            # Load the current config to update UI values
            controller.load_tracking_config(current_config)
    else:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="tracking_config_delete_error_dialog"):
            dpg.add_text(f"Failed to delete configuration '{config_name}'.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("tracking_config_delete_error_dialog"))


def _on_tracking_config_apply(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle applying current tracking configuration to server."""
    controller = user_data
    success = controller.apply_tracking_config_to_server()
    
    if not success:
        # Show error message
        with dpg.window(label="Error", modal=True, tag="tracking_config_apply_error_dialog"):
            dpg.add_text("Failed to apply tracking configuration to server.")
            dpg.add_button(label="OK", callback=lambda: dpg.delete_item("tracking_config_apply_error_dialog"))


# Dashboard UI Configuration Callbacks

def _on_ui_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle saving current UI configuration."""
    controller = user_data
    
    def save_config_dialog():
        with dpg.window(label="Save UI Configuration", modal=True, tag="ui_save_config_dialog"):
            dpg.add_text("Enter configuration name:")
            dpg.add_input_text(tag="ui_config_name_input", width=300)
            dpg.add_text("Description (optional):")
            dpg.add_input_text(tag="ui_config_desc_input", width=300, multiline=True, height=60)
            dpg.add_button(label="Save", callback=_confirm_ui_config_save, user_data=controller)
            dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("ui_save_config_dialog"))
    
    save_config_dialog()


def _confirm_ui_config_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Confirm and save UI configuration."""
    controller = user_data
    
    name = dpg.get_value("ui_config_name_input").strip()
    description = dpg.get_value("ui_config_desc_input").strip()
    
    if not name:
        logging.warning("Configuration name cannot be empty")
        return
    
    success = controller.save_ui_config(name, description)
    dpg.delete_item("ui_save_config_dialog")
    
    if success:
        logging.info(f"Saved UI configuration: {name}")
        # Update dropdown
        if controller.ui and controller.ui.ui_config_combo:
            configs = controller.list_ui_configs()
            dpg.configure_item(controller.ui.ui_config_combo, items=configs, default_value=name)
    else:
        logging.error(f"Failed to save UI configuration: {name}")


def _on_ui_config_load(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle loading UI configuration from dropdown."""
    controller = user_data
    if controller.ui and controller.ui.ui_config_combo:
        config_name = dpg.get_value(controller.ui.ui_config_combo)
        if config_name:
            success = controller.load_ui_config(config_name)
            if not success:
                logging.error(f"Failed to load UI configuration: {config_name}")


def _on_ui_config_set_default(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle setting current configuration as default."""
    controller = user_data
    if controller.ui and controller.ui.ui_config_combo:
        config_name = dpg.get_value(controller.ui.ui_config_combo)
        if config_name:
            success = controller.set_default_ui_config(config_name)
            if success:
                logging.info(f"Set default UI configuration: {config_name}")
            else:
                logging.error(f"Failed to set default UI configuration: {config_name}")


def _on_ui_config_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle deleting selected UI configuration."""
    controller = user_data
    if controller.ui and controller.ui.ui_config_combo:
        config_name = dpg.get_value(controller.ui.ui_config_combo)
        if config_name:
            if config_name == "default":
                logging.warning("Cannot delete default configuration")
                return
            
            # Show confirmation dialog
            def confirm_delete():
                with dpg.window(label="Confirm Delete", modal=True, tag="ui_config_delete_confirm_dialog"):
                    dpg.add_text(f"Are you sure you want to delete UI configuration '{config_name}'?")
                    dpg.add_button(label="Yes", callback=_confirm_ui_config_delete, user_data=(controller, config_name))
                    dpg.add_button(label="No", callback=lambda: dpg.delete_item("ui_config_delete_confirm_dialog"))
            
            confirm_delete()


def _confirm_ui_config_delete(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Confirm deletion of UI configuration."""
    controller, config_name = user_data
    
    success = controller.delete_ui_config(config_name)
    dpg.delete_item("ui_config_delete_confirm_dialog")
    
    if success:
        logging.info(f"Deleted UI configuration: {config_name}")
        # Update dropdown
        if controller.ui and controller.ui.ui_config_combo:
            configs = controller.list_ui_configs()
            current_config = controller.get_current_ui_config_name()
            dpg.configure_item(controller.ui.ui_config_combo, items=configs, default_value=current_config)
            # Load the current config
            controller.load_ui_config(current_config)
    else:
        logging.error(f"Failed to delete UI configuration: {config_name}")


def _on_circle_color_changed(sender: int, app_data: Sequence[float], user_data: AggregateControllerStreaming) -> None:
    """Handle circle color change."""
    controller = user_data
    # app_data is [r, g, b, a] in range 0-255
    controller.circle_color = (int(app_data[0]), int(app_data[1]), int(app_data[2]), int(app_data[3]))


def _on_circle_size_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle circle size change."""
    controller = user_data
    controller.circle_radius = float(dpg.get_value(sender))


def _on_circle_thickness_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle circle thickness change."""
    controller = user_data
    controller.circle_thickness = float(dpg.get_value(sender))


def _on_display_mode_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle image display mode change (raw/overlay)."""
    controller = user_data
    controller.image_state.set_display_mode(str(app_data))


def _on_tile_grid_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle tile grid visibility toggle."""
    controller = user_data
    controller.image_state.set_show_tile_grid(bool(app_data))


def _on_use_colormap_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle mass colormap toggle."""
    controller = user_data
    controller.image_state.set_use_mass_colormap(bool(app_data))


def _on_mass_cutoff_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle mass cutoff value change."""
    controller = user_data
    try:
        value = float(app_data)
        controller.image_state.set_mass_cutoff(value)
    except (TypeError, ValueError):
        pass


def _on_cutoff_color_changed(sender: int, app_data: Sequence[float], user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Handle cutoff color picker change."""
    controller, which = user_data
    from Camera.main_gui import _dpg_color_to_rgb
    rgb = _dpg_color_to_rgb(app_data)
    if which == "below":
        controller.image_state.set_cutoff_colors(below=rgb)
    else:
        controller.image_state.set_cutoff_colors(above=rgb)


def _on_circle_scale_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle circle size scale change."""
    controller = user_data
    try:
        value = float(app_data)
        controller.image_state.set_circle_size_scale(value)
    except (TypeError, ValueError):
        pass


def _on_zoom_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle zoom slider change."""
    controller = user_data
    controller.image_state.set_zoom(float(app_data))


def _on_start_saving_clicked(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle start saving button click."""
    controller = user_data
    
    # Get the image save directory from the input field
    if controller.ui and controller.ui.raw_dir_display:
        image_dir = dpg.get_value(controller.ui.raw_dir_display).strip()
        if not image_dir:
            logging.error("Image save directory cannot be empty")
            return
    else:
        logging.error("Image directory input not found")
        return
    
    # Get the log directory
    log_dir = None
    log_path = None
    if controller.ui and controller.ui.log_dir_display:
        log_dir = dpg.get_value(controller.ui.log_dir_display).strip()
    
    try:
        # Create directories if they don't exist
        image_path = Path(image_dir)
        image_path.mkdir(parents=True, exist_ok=True)
        
        if log_dir:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            
            # Configure logging to save to the log directory
            log_file = log_path / f"dashboard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            logging.getLogger().addHandler(file_handler)
            logging.info(f"Logging to: {log_file}")
        
        # Update storage config to start saving
        controller.image_client.update_storage_config({
            "enabled": True,
            "output_dir": str(image_path),
            "hdf5_enabled": False,  # Disabled as requested
        })
        
        # Update button appearance
        if controller.ui and controller.ui.auto_save_raw_checkbox:
            dpg.configure_item(controller.ui.auto_save_raw_checkbox, 
                             label="⏹ Stop Saving", 
                             default_value=True)
        
        logging.info(f"Started saving images to: {image_path}")
        if log_dir and log_path:
            logging.info(f"Logs saving to: {log_path}")
    except Exception as exc:
        logging.exception("Failed to start saving: %s", exc)


def _on_stop_saving_clicked(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle stop saving button click."""
    controller = user_data
    
    try:
        # Update storage config to stop saving
        controller.image_client.update_storage_config({"enabled": False})
        
        # Update button appearance
        if controller.ui and controller.ui.auto_save_raw_checkbox:
            dpg.configure_item(controller.ui.auto_save_raw_checkbox, 
                             label="▶ Start Saving", 
                             default_value=False)
        
        logging.info("Stopped saving images")
    except Exception as exc:
        logging.exception("Failed to stop saving: %s", exc)


def _on_saving_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle saving toggle."""
    controller = user_data
    is_saving = bool(app_data)
    
    if is_saving:
        _on_start_saving_clicked(sender, app_data, controller)
    else:
        _on_stop_saving_clicked(sender, app_data, controller)


def _on_browse_session_folder_clicked(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle browse session folder button click."""
    controller = user_data
    
    def folder_dialog_callback(sender: int, app_data: Any) -> None:
        """Callback when folder is selected."""
        selections = app_data.get("selections", {})
        if selections and controller.ui:
            # Get the selected folder path
            base_folder = Path(list(selections.values())[0])
            
            # Create subdirectories: images and logs
            images_folder = base_folder / "images"
            logs_folder = base_folder / "logs"
            
            # Update the UI fields
            if controller.ui.raw_dir_display:
                dpg.set_value(controller.ui.raw_dir_display, str(images_folder))
            if controller.ui.log_dir_display:
                dpg.set_value(controller.ui.log_dir_display, str(logs_folder))
            
            logging.info(f"Selected session folder: {base_folder}")
            logging.info(f"  Images will save to: {images_folder}")
            logging.info(f"  Logs will save to: {logs_folder}")
    
    # Create file dialog for folder selection
    with dpg.file_dialog(
        label="Select Session Folder (images/ and logs/ will be created inside)",
        callback=folder_dialog_callback,
        width=700,
        height=400,
        directory_selector=True,
        modal=True,
    ):
        dpg.add_file_extension(".*")


def _on_storage_fps_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle storage target FPS change."""
    controller = user_data
    try:
        fps = float(app_data)
    except (TypeError, ValueError):
        return
    try:
        controller.image_client.update_storage_config({"target_fps": fps})
    except Exception as exc:
        logging.exception("Failed to update storage FPS: %s", exc)


def _on_save_overlay_clicked(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle save overlay button click."""
    controller = user_data
    # TODO: Implement save_current_overlay method in AppState
    logging.info("Save overlay clicked - not yet implemented")


def _on_tracking_apply(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, Dict[str, int]]) -> None:
    """Handle tracking parameters apply button."""
    controller, tracking_inputs = user_data
    payload = {}
    for field_name, tag in tracking_inputs.items():
        payload[field_name] = dpg.get_value(tag)
    try:
        controller.image_client.update_tracking_config(payload)
        controller.image_client.refresh_tracking_config()
    except Exception as exc:
        logging.exception("Failed to apply tracking parameters: %s", exc)


def _on_tracking_reset(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, Dict[str, int]]) -> None:
    """Handle tracking parameters reset button."""
    controller, tracking_inputs = user_data
    from Camera.main_gui import TrackingParameters
    defaults = TrackingParameters()
    payload = defaults.to_dict()
    try:
        controller.image_client.update_tracking_config(payload)
        controller.image_client.refresh_tracking_config()
        for field_name, tag in tracking_inputs.items():
            dpg.set_value(tag, payload.get(field_name, 0))
    except Exception as exc:
        logging.exception("Failed to reset tracking parameters: %s", exc)


def _on_monitoring_start(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle monitoring start button."""
    controller = user_data
    
    # Update interval from input
    if controller.ui and controller.ui.monitoring_interval_input:
        try:
            interval = float(dpg.get_value(controller.ui.monitoring_interval_input))
            controller.set_monitoring_interval(interval)
        except (ValueError, TypeError):
            pass
    
    def start_thread() -> None:
        try:
            controller.start_monitoring()
        except Exception as exc:
            logging.exception("Failed to start monitoring: %s", exc)
    
    threading.Thread(target=start_thread, daemon=True).start()


def _on_monitoring_stop(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle monitoring stop button."""
    controller = user_data
    controller.stop_monitoring()


def _on_monitoring_path_clicked(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Open monitoring folder in file explorer."""
    controller = user_data
    if controller.monitoring_folder and controller.monitoring_folder.exists():
        try:
            # Windows-specific: open folder in explorer
            if sys.platform == "win32":
                os.startfile(controller.monitoring_folder)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(controller.monitoring_folder)])
            else:
                subprocess.run(["xdg-open", str(controller.monitoring_folder)])
        except Exception as exc:
            logging.error(f"Failed to open folder: {exc}")


# Experiment script callbacks

def _on_experiment_script_selected(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle script selection - populate parameters UI."""
    controller = user_data
    
    if not controller.ui:
        return
    
    script_name = dpg.get_value(sender)
    if not script_name or script_name == "No scripts found":
        return
    
    # Update parameters configuration combo box for this script
    if controller.ui.experiment_params_combo:
        configs = controller.list_experiment_params(script_name)
        dpg.configure_item(controller.ui.experiment_params_combo, 
                          items=configs if configs else ["No saved configs"])
        if configs:
            # Try to load default config if available
            default_config = controller.get_default_experiment_params(script_name)
            if default_config and default_config in configs:
                dpg.set_value(controller.ui.experiment_params_combo, default_config)
            else:
                dpg.set_value(controller.ui.experiment_params_combo, configs[0])
        else:
            dpg.set_value(controller.ui.experiment_params_combo, "No saved configs")
    
    # Get script class and create temporary instance for introspection
    try:
        if script_name in controller.script_manager.available_scripts:
            script_class = controller.script_manager.available_scripts[script_name]
            # Create a temporary instance to introspect parameters (no context needed)
            temp_script = script_class()
            
            # Check if script supports auto-config
            if hasattr(temp_script, 'get_param_specs'):
                controller._populate_experiment_params_ui(temp_script)
                
                # Try to load default params if available
                default_config = controller.get_default_experiment_params(script_name)
                if default_config:
                    controller.load_experiment_params(script_name, default_config)
                    logging.info(f"Loaded default parameters for script: {script_name}")
                else:
                    logging.info(f"Loaded parameters for script: {script_name}")
            else:
                # Script doesn't support auto-config, clear parameters UI
                if controller.ui.experiment_params_container and dpg.does_item_exist(controller.ui.experiment_params_container):
                    dpg.delete_item(controller.ui.experiment_params_container, children_only=True)
                    dpg.add_text("This script does not support auto-configuration", 
                               parent=controller.ui.experiment_params_container,
                               color=(180, 180, 180, 255))
                logging.warning(f"Script {script_name} does not support auto-configuration")
        else:
            logging.error(f"Script not found: {script_name}")
    except Exception as e:
        logging.error(f"Failed to load script parameters: {e}")
        if controller.ui.experiment_params_container and dpg.does_item_exist(controller.ui.experiment_params_container):
            dpg.delete_item(controller.ui.experiment_params_container, children_only=True)
            dpg.add_text(f"Error loading parameters: {str(e)}", 
                       parent=controller.ui.experiment_params_container,
                       color=(255, 100, 100, 255))


def _on_experiment_script_start(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Start the selected experiment script."""
    controller = user_data
    if not controller.ui or not controller.ui.experiment_script_combo:
        return
    
    script_name = dpg.get_value(controller.ui.experiment_script_combo)
    if not script_name:
        logging.warning("No experiment script selected")
        return
    
    # Collect parameter values from UI before starting script
    params_to_apply = {}
    if controller.ui.experiment_param_widgets:
        for param_name, widget_id in controller.ui.experiment_param_widgets.items():
            if dpg.does_item_exist(widget_id):
                value = dpg.get_value(widget_id)
                params_to_apply[param_name] = value
                logging.debug(f"Collected parameter {param_name} = {value}")
    
    ctx = controller._build_experiment_context()
    # Pass initial parameters to start_script so they're applied before setup()
    success = controller.script_manager.start_script(script_name, ctx, initial_params=params_to_apply if params_to_apply else None)
    
    if success:
        logging.info(f"Started experiment script: {script_name}")
    else:
        logging.error(f"Failed to start experiment script: {script_name}")


def _on_experiment_script_stop(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Stop the currently running experiment script."""
    controller = user_data
    if controller.script_manager.current_script:
        ctx = controller._build_experiment_context()
        controller.script_manager.stop_script(ctx)
        logging.info("Stopped experiment script")
    else:
        logging.warning("No experiment script is running")


def _on_experiment_script_pause(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Pause/resume the currently running experiment script."""
    controller = user_data
    if not controller.script_manager.current_script:
        logging.warning("No experiment script is running")
        return
    
    ctx = controller._build_experiment_context()
    if controller.script_manager.current_script.is_paused:
        controller.script_manager.resume_script(ctx)
        logging.info("Resumed experiment script")
    else:
        controller.script_manager.pause_script(ctx)
        logging.info("Paused experiment script")


def _on_experiment_script_reload(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Reload available experiment scripts."""
    controller = user_data
    controller.script_manager.scan_scripts()
    
    # Update combo box items
    if controller.ui and controller.ui.experiment_script_combo:
        available = controller.script_manager.get_available_scripts()
        dpg.configure_item(controller.ui.experiment_script_combo, items=available)
        if available:
            dpg.set_value(controller.ui.experiment_script_combo, available[0])
    
    logging.info(f"Reloaded experiment scripts: {len(controller.script_manager.available_scripts)} found")


def _on_experiment_params_apply(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Apply experiment parameters to the current script (auto-config system)."""
    controller = user_data
    
    if not controller.ui or not controller.ui.experiment_param_widgets:
        logging.warning("No parameter widgets available")
        return
    
    # Get current script instance
    if controller.script_manager.current_script:
        script = controller.script_manager.current_script
        
        # Check if script supports auto-config
        if not hasattr(script, 'set_param_value'):
            logging.warning("Script does not support auto-configuration")
            return
        
        # Apply all parameters from UI widgets to script
        params_applied = 0
        for param_name, widget_id in controller.ui.experiment_param_widgets.items():
            if dpg.does_item_exist(widget_id):
                value = dpg.get_value(widget_id)
                if script.set_param_value(param_name, value):
                    params_applied += 1
                    logging.debug(f"Set {param_name} = {value}")
        
        logging.info(f"Applied {params_applied} parameters to running script")
    else:
        logging.warning("No script running - parameters will be applied when script starts")


# Experiment Parameters Configuration Callbacks

def _on_experiment_params_save(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Show dialog to save current experiment parameters."""
    controller = user_data
    if not controller.ui or not controller.ui.experiment_script_combo:
        return
    
    script_name = dpg.get_value(controller.ui.experiment_script_combo)
    if not script_name or script_name == "No scripts found":
        logging.warning("No script selected to save parameters")
        return
    
    # Create modal for name/description input
    with dpg.window(label="Save Experiment Parameters", modal=True, tag="save_exp_params_modal", 
                   width=400, height=200, pos=(400, 300)):
        dpg.add_text("Enter configuration name:")
        config_name_input = dpg.add_input_text(tag="exp_params_config_name_input", width=-1)
        dpg.add_spacing(count=2)
        dpg.add_text("Description (optional):")
        desc_input = dpg.add_input_text(tag="exp_params_config_desc_input", width=-1, multiline=True, height=60)
        dpg.add_spacing(count=2)
        
        with dpg.group(horizontal=True):
            dpg.add_button(label="Save", width=150, 
                         callback=_confirm_experiment_params_save,
                         user_data=(controller, script_name))
            dpg.add_button(label="Cancel", width=150,
                         callback=lambda: dpg.delete_item("save_exp_params_modal"))


def _confirm_experiment_params_save(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Confirm and save experiment parameters configuration."""
    controller, script_name = user_data
    
    config_name = dpg.get_value("exp_params_config_name_input").strip()
    description = dpg.get_value("exp_params_config_desc_input").strip()
    
    if not config_name:
        logging.warning("Configuration name cannot be empty")
        return
    
    success = controller.save_experiment_params(script_name, config_name, description)
    if success:
        logging.info(f"Saved experiment parameters '{config_name}' for script '{script_name}'")
        
        # Update combo box if it exists
        if controller.ui and controller.ui.experiment_params_combo:
            configs = controller.list_experiment_params(script_name)
            dpg.configure_item(controller.ui.experiment_params_combo, items=configs if configs else ["No saved configs"])
            if configs:
                dpg.set_value(controller.ui.experiment_params_combo, config_name)
    
    dpg.delete_item("save_exp_params_modal")


def _on_experiment_params_load(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Load selected experiment parameters configuration."""
    controller = user_data
    if not controller.ui or not controller.ui.experiment_params_combo or not controller.ui.experiment_script_combo:
        return
    
    script_name = dpg.get_value(controller.ui.experiment_script_combo)
    config_name = dpg.get_value(controller.ui.experiment_params_combo)
    
    if config_name and config_name != "No saved configs":
        controller.load_experiment_params(script_name, config_name)


def _on_experiment_params_set_default(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Set selected configuration as default."""
    controller = user_data
    if not controller.ui or not controller.ui.experiment_params_combo or not controller.ui.experiment_script_combo:
        return
    
    script_name = dpg.get_value(controller.ui.experiment_script_combo)
    config_name = dpg.get_value(controller.ui.experiment_params_combo)
    
    if config_name and config_name != "No saved configs":
        success = controller.set_default_experiment_params(script_name, config_name)
        if success:
            logging.info(f"Set '{config_name}' as default for script '{script_name}'")


def _on_experiment_params_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Show confirmation dialog to delete experiment parameters configuration."""
    controller = user_data
    if not controller.ui or not controller.ui.experiment_params_combo or not controller.ui.experiment_script_combo:
        return
    
    script_name = dpg.get_value(controller.ui.experiment_script_combo)
    config_name = dpg.get_value(controller.ui.experiment_params_combo)
    
    if not config_name or config_name == "No saved configs":
        return
    
    # Confirmation dialog
    with dpg.window(label="Delete Configuration", modal=True, tag="delete_exp_params_modal",
                   width=350, height=120, pos=(450, 350)):
        dpg.add_text(f"Delete experiment parameters '{config_name}'?")
        dpg.add_text("This cannot be undone.", color=(220, 80, 80, 255))
        dpg.add_spacing(count=2)
        
        with dpg.group(horizontal=True):
            dpg.add_button(label="Delete", width=140,
                         callback=_confirm_experiment_params_delete,
                         user_data=(controller, script_name, config_name))
            dpg.add_button(label="Cancel", width=140,
                         callback=lambda: dpg.delete_item("delete_exp_params_modal"))


def _confirm_experiment_params_delete(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str, str]) -> None:
    """Confirm and delete experiment parameters configuration."""
    controller, script_name, config_name = user_data
    
    success = controller.delete_experiment_params(script_name, config_name)
    if success:
        # Update combo box
        if controller.ui and controller.ui.experiment_params_combo:
            configs = controller.list_experiment_params(script_name)
            dpg.configure_item(controller.ui.experiment_params_combo, items=configs if configs else ["No saved configs"])
            if configs:
                dpg.set_value(controller.ui.experiment_params_combo, configs[0])
    
    dpg.delete_item("delete_exp_params_modal")


def _on_experiment_params_combo_changed(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Update UI when experiment params combo selection changes."""
    # This is called when the combo selection changes - could add indicator for default config
    pass


# Fine-tuning callbacks

def _on_finetuning_start(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle fine-tuning start button."""
    controller = user_data
    
    logging.info("Fine-tuning start button clicked")
    
    if not controller.ui:
        logging.error("No UI available")
        return
    
    # Get parameters from inputs
    sample_count = dpg.get_value(controller.ui.finetuning_samples_input) if controller.ui.finetuning_samples_input else 12
    margin_pixels = dpg.get_value(controller.ui.finetuning_margin_input) if controller.ui.finetuning_margin_input else 50.0
    
    logging.info(f"Fine-tuning parameters: samples={sample_count}, margin={margin_pixels}")
    logging.info(f"SLM connected: {controller.slm_client.connected}, Image connected: {controller.image_state.connected}")
    
    # Start fine-tuning
    controller.start_finetuning(int(sample_count), float(margin_pixels))
    
    # Update button states only if fine-tuning actually started
    if controller.finetuning_state.active:
        if controller.ui.finetuning_start_button:
            dpg.configure_item(controller.ui.finetuning_start_button, enabled=False)
        if controller.ui.finetuning_pause_button:
            dpg.configure_item(controller.ui.finetuning_pause_button, enabled=True)
        if controller.ui.finetuning_stop_button:
            dpg.configure_item(controller.ui.finetuning_stop_button, enabled=True)
        logging.info("Fine-tuning started successfully")
    else:
        logging.warning("Fine-tuning did not start - check error messages above")


def _on_finetuning_stop(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle fine-tuning stop button."""
    controller = user_data
    controller.stop_finetuning()
    
    if not controller.ui:
        return
    
    # Update button states
    if controller.ui.finetuning_start_button:
        dpg.configure_item(controller.ui.finetuning_start_button, enabled=True)
    if controller.ui.finetuning_pause_button:
        dpg.configure_item(controller.ui.finetuning_pause_button, enabled=False)
    if controller.ui.finetuning_stop_button:
        dpg.configure_item(controller.ui.finetuning_stop_button, enabled=False)


def _on_finetuning_pause(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle fine-tuning pause/resume button."""
    controller = user_data
    controller.pause_finetuning()
    
    if not controller.ui or not controller.ui.finetuning_pause_button:
        return
    
    # Update button label
    label = "Resume" if controller.finetuning_state.paused else "Pause"
    dpg.configure_item(controller.ui.finetuning_pause_button, label=label)


def _on_finetuning_visualization_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle fine-tuning visualization toggle."""
    controller = user_data
    controller.finetuning_state.show_visualization = bool(app_data)


def _on_manual_calibration_capture(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    if controller.ui and controller.ui.manual_capture_button:
        dpg.configure_item(controller.ui.manual_capture_button, enabled=False)

    def worker() -> None:
        try:
            controller.capture_manual_calibration_sample()
            controller._update_manual_calibration_ui()
        finally:
            if controller.ui and controller.ui.manual_capture_button:
                dpg.configure_item(controller.ui.manual_capture_button, enabled=True)
    threading.Thread(target=worker, daemon=True).start()


def _on_manual_calibration_apply(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    if controller.apply_manual_calibration():
        controller._update_manual_calibration_ui()


def _on_manual_calibration_clear(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    controller = user_data
    controller.clear_manual_calibration_samples()
    controller._update_manual_calibration_ui()


def _on_finetuning_config_load(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle loading a fine-tuning configuration."""
    controller = user_data
    
    if not controller.ui or not controller.ui.finetuning_config_combo:
        return
    
    config_name = dpg.get_value(controller.ui.finetuning_config_combo)
    
    if not config_name or config_name == "No fine-tuned configs":
        return
    
    success = controller.load_finetuning_config(config_name)
    
    if success:
        logging.info(f"Loaded fine-tuning config: {config_name}")
        
        # Update metadata display
        _update_finetuning_metadata_display(controller)
    else:
        logging.error(f"Failed to load fine-tuning config: {config_name}")


def _on_finetuning_config_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle deleting a fine-tuning configuration."""
    controller = user_data
    
    if not controller.ui or not controller.ui.finetuning_config_combo:
        return
    
    config_name = dpg.get_value(controller.ui.finetuning_config_combo)
    
    if not config_name or config_name == "No fine-tuned configs":
        return
    
    # Confirmation dialog
    with dpg.window(label="Delete Fine-Tuning Config", modal=True, tag="delete_finetuning_modal",
                   width=350, height=120, pos=(450, 350)):
        dpg.add_text(f"Delete '{config_name}'?")
        dpg.add_spacing(count=2)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Delete",
                callback=_confirm_finetuning_delete,
                user_data=(controller, config_name),
                width=100
            )
            dpg.add_button(
                label="Cancel",
                callback=lambda: dpg.delete_item("delete_finetuning_modal"),
                width=100
            )


def _confirm_finetuning_delete(sender: int, app_data: Any, user_data: Tuple[AggregateControllerStreaming, str]) -> None:
    """Confirm and delete fine-tuning configuration."""
    controller, config_name = user_data
    
    success = controller.delete_finetuning_config(config_name)
    
    if success and controller.ui and controller.ui.finetuning_config_combo:
        # Update combo box
        configs = controller.list_finetuning_configs()
        dpg.configure_item(
            controller.ui.finetuning_config_combo,
            items=configs if configs else ["No fine-tuned configs"],
            default_value="No fine-tuned configs" if not configs else configs[0]
        )
        
        # Clear metadata display
        if controller.ui.finetuning_metadata_text:
            dpg.set_value(controller.ui.finetuning_metadata_text, "")
    
    dpg.delete_item("delete_finetuning_modal")


def _update_finetuning_metadata_display(controller: AggregateControllerStreaming) -> None:
    """Update the fine-tuning metadata display."""
    if not controller.ui or not controller.ui.finetuning_config_combo or not controller.ui.finetuning_metadata_text:
        return
    
    config_name = dpg.get_value(controller.ui.finetuning_config_combo)
    
    if not config_name or config_name == "No fine-tuned configs":
        dpg.set_value(controller.ui.finetuning_metadata_text, "")
        return
    
    metadata = controller.get_finetuning_metadata(config_name)
    
    if metadata:
        # Format timestamp
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(metadata['timestamp'])
            time_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_str = "Unknown"
        
        # Build metadata text
        text = (
            f"Created: {time_str}\n"
            f"Base: {metadata['base_config']}\n"
            f"Samples: {metadata['samples']}\n"
            f"RMS Error: {metadata['rms_error_before']:.2f}px → {metadata['rms_error_after']:.2f}px\n"
            f"Improvement: {metadata['improvement_pct']:.1f}%"
        )
        
        dpg.set_value(controller.ui.finetuning_metadata_text, text)
    else:
        dpg.set_value(controller.ui.finetuning_metadata_text, "Metadata unavailable")


def create_ui(controller: AggregateControllerStreaming, shtc3_display_labels: Dict[str, str]) -> AggregateUI:
    """Create DearPyGui UI with responsive layout.
    
    The UI uses a viewport resize callback to automatically adjust all window sizes
    and positions when the viewport is resized. Windows maintain their relative 
    proportions and positions, creating a "snug fit" layout that adapts to any 
    screen size.
    
    Layout structure:
    - Left column (16%): Connections, Environment/DAC, Tracking Parameters
    - Center column (42%): Image Viewer, SLM Point Control
    - Right column (42%): Split into two sub-columns
      - Right-left (45%): Display Controls, Image Status, Hardware Monitoring
      - Right-right (55%): Image Saving, Image Metrics, SLM Metrics
    
    Args:
        controller: The aggregate controller instance
        shtc3_display_labels: Dict mapping 'temp' and 'humidity' to their display labels
    """
    dpg.create_context()
    dpg.configure_app(docking=True, docking_space=True)
    
    # Get analog specs for dynamic plot creation
    analog_specs = controller.analog_specs

    # Leica-style color scheme - Dark background with green/red/yellow accents
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            # Background colors - very dark like Leica confocal
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (20, 20, 20, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (25, 25, 25, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (35, 35, 35, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (45, 45, 45, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (55, 55, 55, 255))
            
            # Text colors - light gray for good contrast
            dpg.add_theme_color(dpg.mvThemeCol_Text, (230, 230, 230, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (100, 100, 100, 255))
            
            # Button colors - greenish like Leica
            dpg.add_theme_color(dpg.mvThemeCol_Button, (40, 100, 50, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (50, 130, 60, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (60, 150, 70, 255))
            
            # Header colors - subtle green
            dpg.add_theme_color(dpg.mvThemeCol_Header, (40, 80, 45, 180))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (50, 100, 55, 200))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (60, 120, 65, 220))
            
            # Title colors
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (15, 15, 15, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (30, 70, 35, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, (15, 15, 15, 200))
            
            # Separator and border - dark green
            dpg.add_theme_color(dpg.mvThemeCol_Separator, (40, 90, 45, 150))
            dpg.add_theme_color(dpg.mvThemeCol_Border, (30, 70, 35, 100))
            
            # Scrollbar
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (25, 25, 25, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (50, 100, 55, 200))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, (60, 120, 65, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, (70, 140, 75, 255))
            
            # Slider and check - green accent
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, (60, 140, 70, 255))
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, (70, 160, 80, 255))
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (80, 200, 90, 255))
            
            # Plot colors
            dpg.add_theme_color(dpg.mvThemeCol_PlotLines, (80, 200, 90, 255))
            dpg.add_theme_color(dpg.mvThemeCol_PlotLinesHovered, (100, 220, 110, 255))
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, (80, 200, 90, 255))
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogramHovered, (100, 220, 110, 255))
            
            # Spacing and rounding
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 2)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 2)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 2)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 3)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 8)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 4)
    
    dpg.bind_theme(global_theme)

    texture_registry = dpg.generate_uuid()
    image_texture = dpg.generate_uuid()
    with dpg.texture_registry(tag=texture_registry):
        tex_h, tex_w = DEFAULT_TEXTURE_SIZE
        dummy = np.zeros((tex_h, tex_w, 4), dtype=np.float32)
        dpg.add_raw_texture(
            width=tex_w,
            height=tex_h,
            default_value=dummy.flatten(),
            format=dpg.mvFormat_Float_rgba,
            tag=image_texture,
        )

    # Create viewport first to get its dimensions
    dpg.create_viewport(title="Tweezer Control & Monitoring", width=2560, height=1400)
    dpg.setup_dearpygui()
    
    # Detect and move to largest monitor BEFORE showing viewport
    try:
        # Get all monitors using DearPyGui's API
        monitor_count = dpg.get_monitor_count()
        logging.info("Detected %d monitors", monitor_count)
        
        if monitor_count > 1:
            # Find the largest monitor by area
            largest_area = 0
            largest_monitor_idx = 0
            
            for i in range(monitor_count):
                width = dpg.get_monitor_width(i)
                height = dpg.get_monitor_height(i)
                area = width * height
                x_pos = dpg.get_monitor_x_pos(i)
                y_pos = dpg.get_monitor_y_pos(i)
                logging.info("Monitor %d: %dx%d at (%d, %d) - area: %d", 
                           i, width, height, x_pos, y_pos, area)
                
                if area > largest_area:
                    largest_area = area
                    largest_monitor_idx = i
            
            # Get position of the largest monitor
            monitor_x = dpg.get_monitor_x_pos(largest_monitor_idx)
            monitor_y = dpg.get_monitor_y_pos(largest_monitor_idx)
            
            # Set viewport position BEFORE showing
            dpg.set_viewport_pos([monitor_x, monitor_y])
            logging.info("Set viewport to monitor %d at position (%d, %d)", 
                        largest_monitor_idx, monitor_x, monitor_y)
    except Exception as exc:
        logging.warning("Could not detect monitors or move viewport: %s", exc)
    
    dpg.show_viewport()

    # Connections window
    connection_window = dpg.generate_uuid()
    with dpg.window(label="SYSTEM CONNECTIONS", tag=connection_window, no_close=True):
        # Arduino Due Section
        dpg.add_text("ARDUINO DUE (Streaming)", color=HARDWARE_COLOR)
        due_target_label = dpg.add_text(f"Target: {controller.due_endpoint.display()}", color=TEXT_SECONDARY)
        due_connection_status_label = dpg.add_text("Disconnected", color=STATUS_DISCONNECTED)
        due_connect_button = dpg.add_button(label="Connect Due", callback=_on_due_connect,
                                            user_data=controller, width=-1, height=26)
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        
        # Image Server Section
        dpg.add_text("IMAGE SERVER", color=IMAGE_COLOR)
        image_target_label = dpg.add_text(f"Target: {controller.image_endpoint.display()}", color=TEXT_SECONDARY)
        image_status_label = dpg.add_text("Disconnected", color=STATUS_DISCONNECTED)
        dpg.add_text("Bit Depth:", color=TEXT_SECONDARY)
        image_bit_depth_combo = dpg.add_combo(
            items=["8-bit", "12-bit", "16-bit"],
            default_value="16-bit",
            callback=lambda s, a, u: u.set_image_bit_depth(int(a.split("-")[0])),
            user_data=controller,
            width=-1
        )
        image_connect_button = dpg.add_button(label="Connect Image", callback=_on_image_connect,
                                              user_data=controller, width=-1, height=26)
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        
        # SLM Server Section
        dpg.add_text("SLM SERVER", color=SLM_COLOR)
        slm_target_label = dpg.add_text(f"Target: {controller.slm_endpoint.display()}", color=TEXT_SECONDARY)
        slm_connection_status_label = dpg.add_text("Disconnected", color=STATUS_DISCONNECTED)
        slm_connect_button = dpg.add_button(label="Connect SLM", callback=_on_slm_connect,
                                            user_data=controller, width=-1, height=26)

    # UI Configuration Management window
    ui_config_window = dpg.generate_uuid()
    with dpg.window(label="UI CONFIGURATION", tag=ui_config_window, no_close=True):
        dpg.add_text("LAYOUT & PREFERENCES", color=(180, 140, 255, 255))
        
        # Configuration selector
        ui_configs = controller.list_ui_configs()
        current_ui_config = controller.get_current_ui_config_name()
        ui_config_combo = dpg.add_combo(
            ui_configs, 
            default_value=current_ui_config,
            label="Config",
            width=-1,
            callback=_on_ui_config_load,
            user_data=controller
        )
        
        dpg.add_spacing(count=1)
        
        # Configuration management buttons
        with dpg.group(horizontal=True):
            ui_config_save_button = dpg.add_button(
                label="Save",
                callback=_on_ui_config_save,
                user_data=controller,
                width=75
            )
            ui_config_load_button = dpg.add_button(
                label="Load",
                callback=_on_ui_config_load,
                user_data=controller,
                width=75
            )
        
        with dpg.group(horizontal=True):
            ui_config_set_default_button = dpg.add_button(
                label="Set Default",
                callback=_on_ui_config_set_default,
                user_data=controller,
                width=75
            )
            ui_config_delete_button = dpg.add_button(
                label="Delete",
                callback=_on_ui_config_delete,
                user_data=controller,
                width=75
            )
        
        dpg.add_spacing(count=1)
        dpg.add_text("Save: Captures current UI state", color=TEXT_SECONDARY, wrap=150)
        dpg.add_text("Load: Restores saved UI state", color=TEXT_SECONDARY, wrap=150)

    # Metrics Monitoring window
    monitoring_window = dpg.generate_uuid()
    with dpg.window(label="METRICS MONITOR", tag=monitoring_window, no_close=True):
        dpg.add_text("MONITOR CONTROL", color=(100, 180, 230, 255))
        
        # Monitor path (clickable to open folder)
        with dpg.group(horizontal=True):
            dpg.add_text("Folder:", color=TEXT_SECONDARY)
        monitoring_path_text = dpg.add_button(label="Not started", callback=_on_monitoring_path_clicked,
                                             user_data=controller, width=-1, height=20)
        
        dpg.add_spacing(count=1)
        
        # Interval input
        with dpg.group(horizontal=True):
            dpg.add_text("Interval (s):", color=TEXT_SECONDARY)
            monitoring_interval_input = dpg.add_input_float(default_value=5.0, min_value=5.0, 
                                                           max_value=300.0, step=1.0, width=80,
                                                           format="%.1f")
        
        dpg.add_spacing(count=1)
        
        # Start/Stop button
        monitoring_start_button = dpg.add_button(label="Start Monitor", callback=_on_monitoring_start,
                                                user_data=controller, width=-1, height=26)

    # Experiment Script Control window
    experiment_script_window = dpg.generate_uuid()
    with dpg.window(label="EXPERIMENT SCRIPTS", tag=experiment_script_window, no_close=True):
        dpg.add_text("EXPERIMENT CONTROL", color=(180, 100, 230, 255))
        dpg.add_spacing(count=1)
        
        # Script selector
        available_scripts = controller.script_manager.get_available_scripts()
        experiment_script_combo = dpg.add_combo(
            label="Script",
            items=available_scripts if available_scripts else ["No scripts found"],
            default_value=available_scripts[0] if available_scripts else "No scripts found",
            width=-1,
            callback=_on_experiment_script_selected,
            user_data=controller
        )
        
        dpg.add_spacing(count=1)
        
        # Parameters configuration management
        with dpg.collapsing_header(label="Parameters Config", default_open=False):
            dpg.add_text("Saved Configurations:", color=(180, 180, 180, 255))
            
            # Get initial script name if available
            initial_script = available_scripts[0] if available_scripts and available_scripts[0] != "No scripts found" else ""
            initial_configs = controller.list_experiment_params(initial_script) if initial_script else []
            
            experiment_params_combo = dpg.add_combo(
                label="##params_config",
                items=initial_configs if initial_configs else ["No saved configs"],
                default_value=initial_configs[0] if initial_configs else "No saved configs",
                width=-1,
                callback=_on_experiment_params_combo_changed,
                user_data=controller
            )
            
            dpg.add_spacing(count=1)
            
            with dpg.group(horizontal=True):
                experiment_params_save_button = dpg.add_button(
                    label="Save",
                    callback=_on_experiment_params_save,
                    user_data=controller,
                    width=60
                )
                experiment_params_load_button = dpg.add_button(
                    label="Load",
                    callback=_on_experiment_params_load,
                    user_data=controller,
                    width=60
                )
            
            with dpg.group(horizontal=True):
                experiment_params_set_default_button = dpg.add_button(
                    label="Set Default",
                    callback=_on_experiment_params_set_default,
                    user_data=controller,
                    width=80
                )
                experiment_params_delete_button = dpg.add_button(
                    label="Delete",
                    callback=_on_experiment_params_delete,
                    user_data=controller,
                    width=60
                )
        
        dpg.add_spacing(count=1)
        
        # Script parameters (collapsible section) - Will be dynamically populated
        experiment_params_container = dpg.generate_uuid()
        with dpg.collapsing_header(label="Script Parameters", default_open=False, tag=experiment_params_container):
            # This will be populated dynamically when a script is selected
            dpg.add_text("Select a script to see its parameters", color=(180, 180, 180, 255))
        
        dpg.add_spacing(count=1)
        
        # Control buttons
        with dpg.group(horizontal=True):
            experiment_script_start_button = dpg.add_button(
                label="Start",
                callback=_on_experiment_script_start,
                user_data=controller,
                width=60,
                height=26
            )
            experiment_script_pause_button = dpg.add_button(
                label="Pause",
                callback=_on_experiment_script_pause,
                user_data=controller,
                width=60,
                height=26
            )
            experiment_script_stop_button = dpg.add_button(
                label="Stop",
                callback=_on_experiment_script_stop,
                user_data=controller,
                width=60,
                height=26
            )
        
        dpg.add_spacing(count=1)
        
        experiment_script_reload_button = dpg.add_button(
            label="Reload Scripts",
            callback=_on_experiment_script_reload,
            user_data=controller,
            width=-1,
            height=26
        )
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=1)
        
        # Status display
        dpg.add_text("STATUS", color=TEXT_SECONDARY)
        experiment_script_status_text = dpg.add_text("No script running", color=TEXT_PRIMARY)
        
        dpg.add_spacing(count=2)
        
        # Log display
        dpg.add_text("LOG", color=TEXT_SECONDARY)
        experiment_script_info_text = dpg.add_text("", color=TEXT_SECONDARY, wrap=0)

    # Image viewer - Full size professional display
    viewer_window = dpg.generate_uuid()
    with dpg.window(label="IMAGE VIEWER", tag=viewer_window, no_close=True):
        with dpg.child_window(width=-1, height=-1, border=True):
            image_item = dpg.add_image(image_texture)
        dpg.add_spacing(count=1)
        cursor_label = dpg.add_text("Cursor: --", color=TEXT_PRIMARY)
    
    # Register mouse handlers for the image widget
    with dpg.item_handler_registry() as image_handlers:
        dpg.add_item_clicked_handler(callback=_on_image_clicked, user_data=controller)
        dpg.add_item_hover_handler(callback=_on_image_hover, user_data=controller)
    dpg.bind_item_handler_registry(image_item, image_handlers)
    
    # Add global handlers for drag and release (these work across the whole window)
    with dpg.handler_registry():
        dpg.add_mouse_drag_handler(callback=_on_image_dragged, user_data=controller)
        dpg.add_mouse_release_handler(callback=_on_image_released, user_data=controller)
    
    # Image Display Controls window
    image_display_window = dpg.generate_uuid()
    with dpg.window(label="IMAGE DISPLAY CONTROLS", tag=image_display_window, no_close=True):
        dpg.add_text("Display Mode", color=IMAGE_COLOR)
        display_mode_combo = dpg.add_combo(
            label="##display_mode",
            items=["overlay", "raw"],
            default_value=controller.image_state.display_mode,
            callback=_on_display_mode_changed,
            user_data=controller,
            width=-1,
        )
        tile_grid_checkbox = dpg.add_checkbox(
            label="Show Tile Grid",
            default_value=controller.image_state.show_tile_grid,
            callback=_on_tile_grid_toggled,
            user_data=controller,
        )
        zoom_slider = dpg.add_slider_float(
            label="Display Scale",
            default_value=controller.image_state.zoom,
            min_value=0.1,
            max_value=4.0,
            format="%.2f",
            callback=_on_zoom_changed,
            user_data=controller,
        )
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("Overlay Appearance", color=IMAGE_COLOR)
        
        use_colormap_checkbox = dpg.add_checkbox(
            label="Use Mass Colormap",
            default_value=controller.image_state.use_mass_colormap,
            callback=_on_use_colormap_toggled,
            user_data=controller,
        )
        mass_cutoff_input = dpg.add_input_float(
            label="Mass Cutoff",
            default_value=controller.image_state.mass_cutoff,
            format="%.2f",
            callback=_on_mass_cutoff_changed,
            user_data=controller,
            enabled=not controller.image_state.use_mass_colormap,
        )
        
        from Camera.main_gui import _rgb_to_dpg_color
        import sys
        below_color_picker = dpg.add_color_edit(
            label="Below Cutoff",
            default_value=_rgb_to_dpg_color(controller.image_state.cutoff_below_color),
            no_alpha=True,
            callback=_on_cutoff_color_changed,
            user_data=(controller, "below"),
            enabled=not controller.image_state.use_mass_colormap,
        )
        above_color_picker = dpg.add_color_edit(
            label="Above Cutoff",
            default_value=_rgb_to_dpg_color(controller.image_state.cutoff_above_color),
            no_alpha=True,
            callback=_on_cutoff_color_changed,
            user_data=(controller, "above"),
            enabled=not controller.image_state.use_mass_colormap,
        )
        circle_scale_slider = dpg.add_slider_float(
            label="Circle Size Scale",
            default_value=controller.image_state.circle_size_scale,
            min_value=0.25,
            max_value=3.0,
            format="%.2f",
            callback=_on_circle_scale_changed,
            user_data=controller,
        )
    
    # Image Saving & Capture window
    image_saving_window = dpg.generate_uuid()
    with dpg.window(label="IMAGE SAVING & CAPTURE", tag=image_saving_window, no_close=True):
        dpg.add_text("SAVE CONTROL", color=IMAGE_COLOR)
        dpg.add_spacing(count=1)
        
        # Start/Stop saving toggle button
        auto_save_raw_checkbox = dpg.add_checkbox(
            label="▶ Start Saving",
            default_value=False,  # Default: NOT saving
            callback=_on_saving_toggled,
            user_data=controller,
        )
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        
        dpg.add_text("SAVE LOCATIONS", color=IMAGE_COLOR)
        dpg.add_spacing(count=1)
        
        # Session folder selector (creates images/ and logs/ subdirectories)
        dpg.add_button(
            label="📁 Select Session Folder",
            callback=_on_browse_session_folder_clicked,
            user_data=controller,
            width=-1,
        )
        dpg.add_text("(Will create 'images' and 'logs' subdirectories)", 
                    color=TEXT_SECONDARY, wrap=300)
        
        dpg.add_spacing(count=1)
        
        # Image save directory
        raw_dir_display = dpg.add_input_text(
            label="Images Folder",
            default_value=str(controller.image_state.raw_save_dir),
            width=300,
            hint="Path where images will be saved",
        )
        
        # Log save directory
        log_dir_display = dpg.add_input_text(
            label="Logs Folder",
            default_value=str(Path.cwd() / "logs"),
            width=300,
            hint="Path where log files will be saved",
        )
        
        dpg.add_text("💡 Tip: Use 'Select Session Folder' to set both automatically", 
                    color=TEXT_SECONDARY, wrap=300)
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        
        dpg.add_text("SAVE PARAMETERS", color=IMAGE_COLOR)
        dpg.add_spacing(count=1)
        
        storage_target_fps_input = dpg.add_input_float(
            label="Target FPS",
            default_value=controller.image_state.storage_target_fps,
            min_value=0.0,
            max_value=240.0,
            format="%.2f",
            step=0.1,
            callback=_on_storage_fps_changed,
            user_data=controller,
        )
        
        storage_format_text = dpg.add_text(
            f"Storage format: {controller.image_state.storage_image_format} (TIFF)",
            color=TEXT_SECONDARY,
        )
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("Save Metrics", color=IMAGE_COLOR)
        save_text = dpg.add_text("Last save: -- ms", color=TEXT_PRIMARY)
        storage_ratio_text = dpg.add_text("Compression: -- %", color=TEXT_PRIMARY)
        storage_codec_text = dpg.add_text("Codec: n/a", color=TEXT_PRIMARY)
        storage_bytes_text = dpg.add_text("Bytes: --", color=TEXT_PRIMARY)
        storage_throttle_text = dpg.add_text("Throttle: -- ms", color=TEXT_PRIMARY)
        storage_message_text = dpg.add_text("Save Message: (none)", color=TEXT_SECONDARY, wrap=340)
    
    # Remove these from UI structure (keep for compatibility but hide)
    hdf5_path_display = None
    overlay_dir_display = None
    save_overlay_button = None

    # Environment & Control
    env_window = dpg.generate_uuid()
    with dpg.window(label="ENVIRONMENT & DAC CONTROL", tag=env_window, no_close=True):
        dpg.add_text("HARDWARE STATUS", color=HARDWARE_COLOR)
        due_status_label = dpg.add_text("Due: Disconnected", color=STATUS_DISCONNECTED)
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        
        # DAC controls
        dac_items: Dict[str, Tuple[int, int]] = {}
        if controller.dac_specs:
            dpg.add_text("DAC OUTPUTS", color=HARDWARE_COLOR)
            dpg.add_spacing(count=2)
            for name, spec in controller.dac_specs.items():
                value_label = dpg.add_text(f"{spec.label}: 0.0000 {spec.unit}", color=TEXT_PRIMARY)
                dpg.add_spacing(count=1)
                with dpg.group(horizontal=True):
                    dpg.add_text("Step:", color=TEXT_SECONDARY, indent=10)
                    increment_input = dpg.add_input_float(label=f"##inc_{name}", default_value=0.5, width=80)
                    dpg.add_button(label=" - ", callback=_on_dac_adjust,
                                  user_data=(controller, name, -1, increment_input), width=40)
                    dpg.add_button(label=" + ", callback=_on_dac_adjust,
                                  user_data=(controller, name, 1, increment_input), width=40)
                dpg.add_spacing(count=3)
                dac_items[name] = (value_label, increment_input)
        
        dpg.add_spacing(count=2)
        dpg.add_separator()
        dpg.add_spacing(count=2)
        
        # Analog readings
        analog_labels: Dict[str, int] = {}
        if controller.analog_specs:
            dpg.add_text("ANALOG INPUTS", color=HARDWARE_COLOR)
            dpg.add_spacing(count=1)
            for name, spec in controller.analog_specs.items():
                label_id = dpg.add_text(f"{spec.label}: -- {spec.unit}", color=TEXT_PRIMARY)
                analog_labels[name] = label_id
        
        # SHTC3 sensors (always show, will be populated if sensor is configured)
        shtc3_labels: Dict[str, int] = {}
        if analog_labels:
            dpg.add_spacing(count=2)
            dpg.add_separator()
            dpg.add_spacing(count=2)
        dpg.add_text("ENVIRONMENTAL SENSORS", color=HARDWARE_COLOR)
        dpg.add_spacing(count=1)
        temp_label = shtc3_display_labels.get("temp", "Temperature")
        humidity_label = shtc3_display_labels.get("humidity", "Humidity")
        shtc3_labels["temp"] = dpg.add_text(f"{temp_label}: --", color=(255, 160, 100, 255))
        shtc3_labels["humidity"] = dpg.add_text(f"{humidity_label}: --", color=(255, 140, 120, 255))

    # Hardware Monitoring window - resizable with scrollable content
    hardware_window = dpg.generate_uuid()
    with dpg.window(label="HARDWARE MONITORING", no_close=True, tag=hardware_window):
        dpg.add_text("DUE TELEMETRY", color=HARDWARE_COLOR)
        dpg.add_text("History Limit:", color=TEXT_SECONDARY)
        hardware_history_slider = dpg.add_slider_int(
            default_value=DEFAULT_METRICS_HISTORY,
            min_value=100,
            max_value=50000,
            width=-1,
            callback=lambda s, v: controller.set_hardware_history_limit(v),
            format="%d samples"
        )
        dpg.add_separator()
        
        # Scrollable child window for plots
        with dpg.child_window(width=-1, height=-1, border=False):
            # Temperature plot
            with dpg.plot(label="Temperature (C)", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                temp_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                temp_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Temp (C)")
                temp_series = dpg.add_line_series([], [], label="Temperature", parent=temp_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Humidity plot
            with dpg.plot(label="Humidity (%RH)", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                humidity_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                humidity_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="RH (%)")
                humidity_series = dpg.add_line_series([], [], label="Humidity", parent=humidity_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Dynamically create plots for analog channels
            # Group channels by pair_graph_code
            plot_groups: Dict[str, List[AnalogChannelSpec]] = {}
            for name, spec in analog_specs.items():
                if spec.pair_graph_code is not None:
                    group_key = f"pair_{spec.pair_graph_code}"
                else:
                    group_key = name
                
                if group_key not in plot_groups:
                    plot_groups[group_key] = []
                plot_groups[group_key].append(spec)
            
            # Store plot references
            analog_plot_series: Dict[str, int] = {}
            analog_plot_axes: Dict[str, Tuple[int, int]] = {}
            
            # Create plots for each group
            for group_key in sorted(plot_groups.keys()):
                specs_in_group = plot_groups[group_key]
                
                # Determine plot title and y-axis label
                if len(specs_in_group) == 1:
                    spec = specs_in_group[0]
                    plot_title = spec.label
                    y_label = f"{spec.label} ({spec.unit})"
                else:
                    # Multiple channels in same plot
                    labels = [s.label for s in specs_in_group]
                    plot_title = " & ".join(labels)
                    # Use unit from first spec if all same, otherwise generic
                    units = list(set(s.unit for s in specs_in_group))
                    y_label = f"Value ({units[0]})" if len(units) == 1 else "Value"
                
                with dpg.plot(label=plot_title, height=PLOT_HEIGHT, width=-1):
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                    y_axis = dpg.add_plot_axis(dpg.mvYAxis, label=y_label)
                    
                    # Store axes for this group
                    analog_plot_axes[group_key] = (x_axis, y_axis)
                    
                    # Add series for each channel in the group
                    for spec in specs_in_group:
                        series = dpg.add_line_series([], [], label=spec.label, parent=y_axis)
                        analog_plot_series[spec.name] = series
                
                dpg.add_spacer(height=8)

    # Image Server Metrics window - resizable with scrollable content
    image_metrics_window = dpg.generate_uuid()
    with dpg.window(label="IMAGE SERVER METRICS", no_close=True, tag=image_metrics_window):
        dpg.add_text("IMAGE PERFORMANCE", color=IMAGE_COLOR)
        
        # Add metric text displays at the top
        dpg.add_text("Current Metrics:", color=TEXT_SECONDARY)
        image_metrics_sequence_text = dpg.add_text("Sequence: --", color=TEXT_PRIMARY)
        image_metrics_latency_text = dpg.add_text("Latency: -- ms", color=TEXT_PRIMARY)
        image_metrics_processing_text = dpg.add_text("Processing: -- ms", color=TEXT_PRIMARY)
        image_metrics_render_text = dpg.add_text("Render: -- ms", color=TEXT_PRIMARY)
        image_metrics_features_text = dpg.add_text("Detections: --", color=TEXT_PRIMARY)
        
        dpg.add_spacing(count=2)
        dpg.add_text("History Limit:", color=TEXT_SECONDARY)
        image_history_slider = dpg.add_slider_int(
            default_value=DEFAULT_METRICS_HISTORY,
            min_value=100,
            max_value=50000,
            width=-1,
            callback=lambda s, v: controller.set_image_history_limit(v),
            format="%d samples"
        )
        dpg.add_separator()
        
        # Scrollable child window for plots
        with dpg.child_window(width=-1, height=-1, border=False):
            # Frame latency plot
            dpg.add_text("Frame Latency", color=IMAGE_COLOR)
            with dpg.plot(label="##latency_plot", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                img_latency_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                img_latency_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Latency (ms)")
                img_latency_series = dpg.add_line_series([], [], label="Latency", parent=img_latency_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Processing time plot
            dpg.add_text("Processing Time", color=IMAGE_COLOR)
            with dpg.plot(label="##processing_plot", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                img_processing_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                img_processing_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Processing (ms)")
                img_processing_series = dpg.add_line_series([], [], label="Processing", parent=img_processing_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Render prep time plot
            dpg.add_text("Render Preparation", color=IMAGE_COLOR)
            with dpg.plot(label="##render_plot", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                img_render_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                img_render_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Render (ms)")
                img_render_series = dpg.add_line_series([], [], label="Render", parent=img_render_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Save duration plot
            dpg.add_text("Save Duration", color=IMAGE_COLOR)
            with dpg.plot(label="##save_plot", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                img_save_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                img_save_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Duration (ms)")
                img_save_series = dpg.add_line_series([], [], label="Save", parent=img_save_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Compression ratio plot
            dpg.add_text("Compression Ratio", color=IMAGE_COLOR)
            with dpg.plot(label="##compression_plot", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                img_compression_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                img_compression_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Ratio (%)")
                img_compression_series = dpg.add_line_series([], [], label="Compression", parent=img_compression_y_axis)
            
            dpg.add_spacer(height=8)
            
            # Detection count plot
            dpg.add_text("Feature Detection Count", color=IMAGE_COLOR)
            with dpg.plot(label="##features_plot", height=PLOT_HEIGHT, width=-1):
                dpg.add_plot_legend()
                img_features_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", time=True)
                img_features_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Count")
                img_features_series = dpg.add_line_series([], [], label="Detections", parent=img_features_y_axis)

    # SLM Server Metrics window
    slm_metrics_window = dpg.generate_uuid()
    with dpg.window(label="SLM SERVER METRICS", tag=slm_metrics_window, no_close=True):
        dpg.add_text("SLM TELEMETRY", color=SLM_COLOR)
        slm_generation_text = dpg.add_text("Generation: --", color=TEXT_PRIMARY)
        dpg.add_separator()
        
        # SLM Generation time plot
        with dpg.plot(label="Generation Time (ms)", height=PLOT_HEIGHT, width=-1):
            dpg.add_plot_legend()
            slm_gen_x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)")
            slm_gen_y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Time (ms)")
            slm_gen_series = dpg.add_line_series([], [], label="Generation", parent=slm_gen_y_axis)
            dpg.set_axis_limits_auto(slm_gen_x_axis)
            dpg.set_axis_limits_auto(slm_gen_y_axis)

    # Tracking Parameters window
    tracking_window = dpg.generate_uuid()
    tracking_inputs: Dict[str, int] = {}
    with dpg.window(label="TRACKING PARAMETERS", tag=tracking_window, no_close=True):
        dpg.add_text("CONFIGURATION MANAGEMENT", color=IMAGE_COLOR)
        dpg.add_text("Save, load, and manage tracking configurations", color=TEXT_SECONDARY)
        dpg.add_spacing(count=1)
        
        # Configuration dropdown and controls
        available_tracking_configs = controller.list_tracking_configs()
        current_tracking_config = controller.get_current_tracking_config_name()
        
        tracking_config_combo = dpg.add_combo(
            label="Configuration",
            items=available_tracking_configs,
            default_value=current_tracking_config,
            callback=_on_tracking_config_load,
            user_data=controller,
            width=200
        )
        
        dpg.add_spacing(count=1)
        
        # Configuration buttons - first row
        with dpg.group(horizontal=True):
            tracking_config_save_button = dpg.add_button(
                label="Save",
                callback=_on_tracking_config_save,
                user_data=controller,
                width=75,
                tag="tracking_config_save_btn"
            )
            tracking_config_load_button = dpg.add_button(
                label="Load",
                callback=_on_tracking_config_load,
                user_data=controller,
                width=75,
                tag="tracking_config_load_btn"
            )
            tracking_config_set_default_button = dpg.add_button(
                label="Set Default",
                callback=_on_tracking_config_set_default,
                user_data=controller,
                width=75,
                tag="tracking_config_default_btn"
            )
        
        dpg.add_spacing(count=1)
        
        # Configuration buttons - second row
        with dpg.group(horizontal=True):
            tracking_config_reset_button = dpg.add_button(
                label="Reset",
                callback=_on_tracking_config_reset,
                user_data=controller,
                width=75,
                tag="tracking_config_reset_btn"
            )
            tracking_config_delete_button = dpg.add_button(
                label="Delete",
                callback=_on_tracking_config_delete,
                user_data=controller,
                width=75,
                tag="tracking_config_delete_btn"
            )
            tracking_config_apply_button = dpg.add_button(
                label="Apply",
                callback=_on_tracking_config_apply,
                user_data=controller,
                width=75,
                tag="tracking_config_apply_btn"
            )
        
        dpg.add_separator()
        dpg.add_text("SERVER PARAMETERS", color=IMAGE_COLOR)
        tracking_params_path_text = dpg.add_text("Last JSON: (none)", color=TEXT_SECONDARY, wrap=280)
        dpg.add_separator()
        
        with dpg.child_window(width=-1, height=340, border=True):
            for field_name, label, widget_type in [
                ("diameter", "Diameter", int),
                ("separation", "Separation", int),
                ("percentile", "Percentile", float),
                ("minmass", "Min Mass", float),
                ("maxmass", "Max Mass", float),
                ("pixel_threshold", "Pixel Threshold", float),
                ("preprocess", "Enable Preprocess", bool),
                ("lshort", "Bandpass lshort", int),
                ("llong", "Bandpass llong", int),
                ("min_ecc", "Min Eccentricity", float),
                ("max_ecc", "Max Eccentricity", float),
                ("refine", "Refine Iterations", int),
                ("tile_width", "Tile Width", int),
                ("tile_height", "Tile Height", int),
                ("tile_overlap", "Tile Overlap", int),
                ("max_workers", "Max Workers", int),
                ("worker_backend", "Worker Backend", str),
            ]:
                tag = f"tracking_{field_name}"
                value = controller.current_tracking_params.get(field_name, 0)
                
                def on_tracking_param_changed(sender, app_data, user_data):
                    param_name, controller_ref = user_data
                    new_value = dpg.get_value(sender)
                    controller_ref.current_tracking_params[param_name] = new_value
                
                if widget_type is bool:
                    tracking_inputs[field_name] = dpg.add_checkbox(
                        label=label, default_value=bool(value), tag=tag,
                        callback=on_tracking_param_changed, user_data=(field_name, controller)
                    )
                elif widget_type is int:
                    tracking_inputs[field_name] = dpg.add_input_int(
                        label=label, default_value=int(value), tag=tag,
                        callback=on_tracking_param_changed, user_data=(field_name, controller)
                    )
                elif widget_type is str:
                    tracking_inputs[field_name] = dpg.add_input_text(
                        label=label, default_value=str(value), tag=tag,
                        callback=on_tracking_param_changed, user_data=(field_name, controller)
                    )
                else:
                    tracking_inputs[field_name] = dpg.add_input_float(
                        label=label, default_value=float(value), tag=tag, format="%.2f",
                        callback=on_tracking_param_changed, user_data=(field_name, controller)
                    )
        
        dpg.add_spacing(count=1)
        with dpg.group(horizontal=True):
            tracking_apply_button = dpg.add_button(
                label="Apply To Server",
                callback=_on_tracking_apply,
                user_data=(controller, tracking_inputs),
                width=140,
            )
            tracking_reset_button = dpg.add_button(
                label="Reset Defaults",
                callback=_on_tracking_reset,
                user_data=(controller, tracking_inputs),
                width=140,
            )

    # SLM controls
    slm_window = dpg.generate_uuid()
    with dpg.window(label="SLM POINT CONTROL", tag=slm_window, no_close=True):
        dpg.add_text("POINT MANAGEMENT", color=SLM_COLOR)
        dpg.add_spacing(count=1)
        slm_points_label = dpg.add_text("Active Points: 0", color=TEXT_PRIMARY)
        dpg.add_spacing(count=2)
        with dpg.group(horizontal=True):
            slm_send_button = dpg.add_button(label="Send to SLM", callback=_on_slm_send, user_data=controller, 
                          width=180, height=30)
            slm_clear_button = dpg.add_button(label="Clear All Points", callback=_on_slm_clear, user_data=controller, 
                          width=180, height=30)
        dpg.add_spacing(count=2)
        slm_ack_label = dpg.add_text("Status: Ready", color=SLM_COLOR)
        
        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("AFFINE CALIBRATION", color=SLM_COLOR)
        dpg.add_text("Map camera coordinates to SLM coordinates", color=TEXT_SECONDARY)
        dpg.add_spacing(count=1)
        
        # Create affine parameter inputs
        slm_affine_inputs = {}
        
        # Point 0
        dpg.add_text("Point 0:", color=TEXT_PRIMARY)
        with dpg.group(horizontal=True):
            dpg.add_text("CAM:")
            slm_affine_inputs["cam_x0"] = dpg.add_input_float(
                label="X##cam_x0", default_value=controller.slm_affine_params.get("cam_x0", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "cam_x0")
            )
            slm_affine_inputs["cam_y0"] = dpg.add_input_float(
                label="Y##cam_y0", default_value=controller.slm_affine_params.get("cam_y0", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "cam_y0")
            )
        with dpg.group(horizontal=True):
            dpg.add_text("SLM:")
            slm_affine_inputs["slm_x0"] = dpg.add_input_float(
                label="X##slm_x0", default_value=controller.slm_affine_params.get("slm_x0", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "slm_x0")
            )
            slm_affine_inputs["slm_y0"] = dpg.add_input_float(
                label="Y##slm_y0", default_value=controller.slm_affine_params.get("slm_y0", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "slm_y0")
            )
        
        dpg.add_spacing(count=1)
        
        # Point 1
        dpg.add_text("Point 1:", color=TEXT_PRIMARY)
        with dpg.group(horizontal=True):
            dpg.add_text("CAM:")
            slm_affine_inputs["cam_x1"] = dpg.add_input_float(
                label="X##cam_x1", default_value=controller.slm_affine_params.get("cam_x1", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "cam_x1")
            )
            slm_affine_inputs["cam_y1"] = dpg.add_input_float(
                label="Y##cam_y1", default_value=controller.slm_affine_params.get("cam_y1", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "cam_y1")
            )
        with dpg.group(horizontal=True):
            dpg.add_text("SLM:")
            slm_affine_inputs["slm_x1"] = dpg.add_input_float(
                label="X##slm_x1", default_value=controller.slm_affine_params.get("slm_x1", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "slm_x1")
            )
            slm_affine_inputs["slm_y1"] = dpg.add_input_float(
                label="Y##slm_y1", default_value=controller.slm_affine_params.get("slm_y1", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "slm_y1")
            )
        
        dpg.add_spacing(count=1)
        
        # Point 2
        dpg.add_text("Point 2:", color=TEXT_PRIMARY)
        with dpg.group(horizontal=True):
            dpg.add_text("CAM:")
            slm_affine_inputs["cam_x2"] = dpg.add_input_float(
                label="X##cam_x2", default_value=controller.slm_affine_params.get("cam_x2", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "cam_x2")
            )
            slm_affine_inputs["cam_y2"] = dpg.add_input_float(
                label="Y##cam_y2", default_value=controller.slm_affine_params.get("cam_y2", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "cam_y2")
            )
        with dpg.group(horizontal=True):
            dpg.add_text("SLM:")
            slm_affine_inputs["slm_x2"] = dpg.add_input_float(
                label="X##slm_x2", default_value=controller.slm_affine_params.get("slm_x2", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "slm_x2")
            )
            slm_affine_inputs["slm_y2"] = dpg.add_input_float(
                label="Y##slm_y2", default_value=controller.slm_affine_params.get("slm_y2", 0.0), width=80, step=0.0,
                callback=_on_slm_affine_changed, user_data=(controller, "slm_y2")
            )
        
        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("CONFIGURATION MANAGEMENT", color=SLM_COLOR)
        dpg.add_text("Save, load, and manage SLM calibrations", color=TEXT_SECONDARY)
        dpg.add_spacing(count=1)
        
        # Configuration dropdown and controls
        available_configs = controller.list_slm_configs()
        current_config = controller.get_current_slm_config_name()
        
        slm_config_combo = dpg.add_combo(
            label="Configuration",
            items=available_configs,
            default_value=current_config,
            callback=_on_slm_config_load,
            user_data=controller,
            width=200
        )
        
        dpg.add_spacing(count=1)
        
        # Configuration buttons - first row
        with dpg.group(horizontal=True):
            slm_config_save_button = dpg.add_button(
                label="Save",
                callback=_on_slm_config_save,
                user_data=controller,
                width=90,
                tag="slm_config_save_btn"
            )
            slm_config_load_button = dpg.add_button(
                label="Load",
                callback=_on_slm_config_load,
                user_data=controller,
                width=90,
                tag="slm_config_load_btn"
            )
            slm_config_set_default_button = dpg.add_button(
                label="Set Default",
                callback=_on_slm_config_set_default,
                user_data=controller,
                width=90,
                tag="slm_config_default_btn"
            )
        
        dpg.add_spacing(count=1)
        
        # Configuration buttons - second row
        with dpg.group(horizontal=True):
            slm_config_reset_button = dpg.add_button(
                label="Reset",
                callback=_on_slm_config_reset,
                user_data=controller,
                width=90,
                tag="slm_config_reset_btn"
            )
            slm_config_delete_button = dpg.add_button(
                label="Delete",
                callback=_on_slm_config_delete,
                user_data=controller,
                width=90,
                tag="slm_config_delete_btn"
            )
        
        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("HOLOGRAM FEATURES", color=SLM_COLOR)
        dpg.add_text("Adjust optional hologram processing stages", color=TEXT_SECONDARY)
        feature_apodization_checkbox = dpg.add_checkbox(
            label="Enable Apodization",
            default_value=controller.slm_feature_state.apodization_enabled,
            callback=_on_feature_apodization_toggled,
            user_data=controller,
        )
        feature_apodization_strength_slider = dpg.add_slider_float(
            label="Apodization Strength",
            default_value=controller.slm_feature_state.apodization_strength,
            min_value=0.0,
            max_value=1.0,
            callback=_on_feature_apodization_strength,
            user_data=controller,
        )
        feature_z_focus_checkbox = dpg.add_checkbox(
            label="Enable Z Focus",
            default_value=controller.slm_feature_state.z_focus_enabled,
            callback=_on_feature_z_focus_toggled,
            user_data=controller,
        )
        feature_z_focus_offset_slider = dpg.add_slider_float(
            label="Z Focus Offset",
            default_value=controller.slm_feature_state.z_focus_offset,
            min_value=-20.0,
            max_value=20.0,
            callback=_on_feature_z_focus_offset,
            user_data=controller,
        )
        feature_z_focus_scale_slider = dpg.add_slider_float(
            label="Z Focus Scale",
            default_value=controller.slm_feature_state.z_focus_scale,
            min_value=0.0,
            max_value=10.0,
            callback=_on_feature_z_focus_scale,
            user_data=controller,
        )
        feature_default_point_z_slider = dpg.add_slider_float(
            label="Default Point Z",
            default_value=controller.slm_feature_state.default_point_z,
            min_value=-20.0,
            max_value=20.0,
            callback=_on_feature_default_point_z,
            user_data=controller,
        )

        dpg.add_spacing(count=1)
        feature_configs = controller.list_feature_configs()
        feature_current = controller.get_current_feature_config_name()
        feature_config_combo = dpg.add_combo(
            label="Feature Configuration",
            items=feature_configs,
            default_value=feature_current,
            callback=_on_feature_config_load,
            user_data=controller,
            width=200,
        )

        with dpg.group(horizontal=True):
            feature_config_save_button = dpg.add_button(
                label="Save",
                callback=_on_feature_config_save,
                user_data=controller,
                width=90,
                tag="feature_config_save_btn",
            )
            feature_config_load_button = dpg.add_button(
                label="Load",
                callback=_on_feature_config_load,
                user_data=controller,
                width=90,
                tag="feature_config_load_btn",
            )
            feature_config_set_default_button = dpg.add_button(
                label="Set Default",
                callback=_on_feature_config_set_default,
                user_data=controller,
                width=90,
                tag="feature_config_default_btn",
            )

        with dpg.group(horizontal=True):
            feature_config_reset_button = dpg.add_button(
                label="Reset",
                callback=_on_feature_config_reset,
                user_data=controller,
                width=90,
                tag="feature_config_reset_btn",
            )
            feature_config_delete_button = dpg.add_button(
                label="Delete",
                callback=_on_feature_config_delete,
                user_data=controller,
                width=90,
                tag="feature_config_delete_btn",
            )

        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("POINT LIST", color=SLM_COLOR)
        dpg.add_text("Click on image to add, right-click to remove", color=TEXT_SECONDARY)
        dpg.add_spacing(count=1)
        
        # Scrollable list of points
        with dpg.child_window(height=150, border=True):
            slm_points_list_group = dpg.add_group()
        
        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("AUTO FINE-TUNING", color=(255, 215, 0, 255))  # Gold color for special feature
        dpg.add_text("Automatically refine calibration using tracked particles", color=TEXT_SECONDARY, wrap=360)
        dpg.add_spacing(count=1)
        
        # Fine-tuning configuration display
        finetuning_configs = controller.list_finetuning_configs()
        finetuning_config_combo = dpg.add_combo(
            label="Fine-Tuned Config",
            items=finetuning_configs if finetuning_configs else ["No fine-tuned configs"],
            default_value="No fine-tuned configs" if not finetuning_configs else finetuning_configs[0],
            callback=_on_finetuning_config_load,
            user_data=controller,
            width=200
        )
        
        dpg.add_spacing(count=1)
        
        # Metadata display
        finetuning_metadata_text = dpg.add_text("", color=TEXT_SECONDARY, wrap=360)
        
        dpg.add_spacing(count=1)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Load Selected",
                callback=_on_finetuning_config_load,
                user_data=controller,
                width=130
            )
            finetuning_config_delete_button = dpg.add_button(
                label="Delete",
                callback=_on_finetuning_config_delete,
                user_data=controller,
                width=80
            )
        
        dpg.add_separator()
        dpg.add_spacing(count=1)
        dpg.add_text("NEW FINE-TUNING SESSION", color=(255, 215, 0, 255))
        dpg.add_spacing(count=1)
        
        # Fine-tuning parameters
        with dpg.group(horizontal=True):
            dpg.add_text("Sample Count:", color=TEXT_PRIMARY)
            finetuning_samples_input = dpg.add_input_int(
                default_value=12,
                min_value=3,
                max_value=100,
                min_clamped=True,
                max_clamped=True,
                width=80
            )
        
        with dpg.group(horizontal=True):
            dpg.add_text("Edge Margin (px):", color=TEXT_PRIMARY)
            finetuning_margin_input = dpg.add_input_float(
                default_value=50.0,
                min_value=10.0,
                max_value=200.0,
                min_clamped=True,
                max_clamped=True,
                width=80
            )
        
        finetuning_visualization_checkbox = dpg.add_checkbox(
            label="Show Visualization",
            default_value=True,
            callback=_on_finetuning_visualization_toggled,
            user_data=controller
        )
        
        dpg.add_spacing(count=1)
        
        # Progress bar
        finetuning_progress_bar = dpg.add_progress_bar(
            default_value=0.0,
            width=-1
        )
        
        # Status text
        finetuning_status_text = dpg.add_text("Ready to start", color=SLM_COLOR)
        
        dpg.add_spacing(count=1)
        
        # Control buttons
        with dpg.group(horizontal=True):
            finetuning_start_button = dpg.add_button(
                label="Start",
                callback=_on_finetuning_start,
                user_data=controller,
                width=120,
                height=30
            )
            finetuning_pause_button = dpg.add_button(
                label="Pause",
                callback=_on_finetuning_pause,
                user_data=controller,
                width=80,
                height=30,
                enabled=False
            )
            finetuning_stop_button = dpg.add_button(
                label="Stop",
                callback=_on_finetuning_stop,
                user_data=controller,
                width=80,
                height=30,
                enabled=False
            )
        
        dpg.add_spacing(count=1)
        dpg.add_text("MANUAL CALIBRATION", color=(255, 215, 0, 255))
        dpg.add_text(
            "Capture manual trap↔particle pairs to refine calibration",
            color=TEXT_SECONDARY,
            wrap=360,
        )
        manual_sample_count_text = dpg.add_text("Manual samples: 0", color=TEXT_PRIMARY)
        manual_status_text = dpg.add_text("", color=TEXT_SECONDARY, wrap=360)

        with dpg.group(horizontal=True):
            manual_capture_button = dpg.add_button(
                label="Capture Sample",
                callback=_on_manual_calibration_capture,
                user_data=controller,
                width=150,
            )
            manual_apply_button = dpg.add_button(
                label="Apply Manual Calibration",
                callback=_on_manual_calibration_apply,
                user_data=controller,
                width=220,
                enabled=False,
            )

        manual_clear_button = dpg.add_button(
            label="Clear Manual Samples",
            callback=_on_manual_calibration_clear,
            user_data=controller,
            width=180,
            enabled=False,
        )

        dpg.add_separator()
        dpg.add_spacing(count=2)
        dpg.add_text("CIRCLE VISUALIZATION", color=SLM_COLOR)
        dpg.add_spacing(count=1)
        
        # Color picker
        slm_circle_color_picker = dpg.add_color_edit(
            label="Circle Color",
            default_value=(0, 255, 0, 255),  # Green
            callback=_on_circle_color_changed,
            user_data=controller,
            width=200
        )
        
        # Size slider
        slm_circle_size_slider = dpg.add_slider_float(
            label="Circle Radius",
            default_value=15.0,
            min_value=5.0,
            max_value=50.0,
            callback=_on_circle_size_changed,
            user_data=controller,
            width=200
        )
        
        # Thickness slider
        slm_circle_thickness_slider = dpg.add_slider_float(
            label="Circle Thickness",
            default_value=2.0,
            min_value=1.0,
            max_value=10.0,
            callback=_on_circle_thickness_changed,
            user_data=controller,
            width=200
        )

    ui = AggregateUI(
        texture_registry=texture_registry,
        texture_id=image_texture,
        texture_size=(tex_w, tex_h),
        image_item=image_item,
        cursor_label=cursor_label,
        slm_points_label=slm_points_label,
        slm_ack_label=slm_ack_label,
        due_status_label=due_status_label,
        dac_items=dac_items,
        analog_labels=analog_labels,
        shtc3_labels=shtc3_labels,
        shtc3_display_labels=shtc3_display_labels,
        image_connect_button=image_connect_button,
        due_connect_button=due_connect_button,
        slm_connect_button=slm_connect_button,
        image_bit_depth_combo=image_bit_depth_combo,
        slm_send_button=slm_send_button,
        slm_clear_button=slm_clear_button,
        slm_affine_inputs=slm_affine_inputs,
        slm_config_combo=slm_config_combo,
        slm_config_save_button=slm_config_save_button,
        slm_config_load_button=slm_config_load_button,
        slm_config_set_default_button=slm_config_set_default_button,
        slm_config_reset_button=slm_config_reset_button,
        slm_config_delete_button=slm_config_delete_button,
        slm_points_list_group=slm_points_list_group,
        slm_circle_color_picker=slm_circle_color_picker,
        slm_circle_size_slider=slm_circle_size_slider,
        slm_circle_thickness_slider=slm_circle_thickness_slider,
    feature_apodization_checkbox=feature_apodization_checkbox,
    feature_apodization_strength_slider=feature_apodization_strength_slider,
    feature_z_focus_checkbox=feature_z_focus_checkbox,
    feature_z_focus_offset_slider=feature_z_focus_offset_slider,
    feature_z_focus_scale_slider=feature_z_focus_scale_slider,
    feature_default_point_z_slider=feature_default_point_z_slider,
    feature_config_combo=feature_config_combo,
    feature_config_save_button=feature_config_save_button,
    feature_config_load_button=feature_config_load_button,
    feature_config_set_default_button=feature_config_set_default_button,
    feature_config_reset_button=feature_config_reset_button,
    feature_config_delete_button=feature_config_delete_button,
        image_connection_status_label=image_status_label,
        due_connection_status_label=due_connection_status_label,
        slm_connection_status_label=slm_connection_status_label,
        image_target_label=image_target_label,
        due_target_label=due_target_label,
        slm_target_label=slm_target_label,
        image_sequence_text=None,
        image_latency_text=None,
        image_processing_text=None,
        image_detection_text=None,
        image_request_latency_text=None,
        image_render_latency_text=None,
        image_metrics_sequence_text=image_metrics_sequence_text,
        image_metrics_latency_text=image_metrics_latency_text,
        image_metrics_processing_text=image_metrics_processing_text,
        image_metrics_render_text=image_metrics_render_text,
        image_metrics_features_text=image_metrics_features_text,
        display_mode_combo=display_mode_combo,
        tile_grid_checkbox=tile_grid_checkbox,
        zoom_slider=zoom_slider,
        use_colormap_checkbox=use_colormap_checkbox,
        mass_cutoff_input=mass_cutoff_input,
        below_color_picker=below_color_picker,
        above_color_picker=above_color_picker,
        circle_scale_slider=circle_scale_slider,
        auto_save_raw_checkbox=auto_save_raw_checkbox,
        auto_save_overlay_checkbox=None,  # Removed from UI
        save_hdf5_checkbox=None,  # Removed from UI
        storage_target_fps_input=storage_target_fps_input,
        save_overlay_button=None,  # Removed from UI
        raw_dir_display=raw_dir_display,
        log_dir_display=log_dir_display,
        overlay_dir_display=None,  # Removed from UI
        hdf5_path_display=None,  # Removed from UI
        storage_format_text=storage_format_text,
        save_text=save_text,
        storage_ratio_text=storage_ratio_text,
        storage_codec_text=storage_codec_text,
        storage_bytes_text=storage_bytes_text,
        storage_throttle_text=storage_throttle_text,
        storage_message_text=storage_message_text,
        tracking_params_path_text=tracking_params_path_text,
        tracking_apply_button=tracking_apply_button,
        tracking_reset_button=tracking_reset_button,
        tracking_inputs=tracking_inputs,
        tracking_config_combo=tracking_config_combo,
        tracking_config_save_button=tracking_config_save_button,
        tracking_config_load_button=tracking_config_load_button,
        tracking_config_set_default_button=tracking_config_set_default_button,
        tracking_config_reset_button=tracking_config_reset_button,
        tracking_config_delete_button=tracking_config_delete_button,
        slm_last_command_text=None,
        slm_generation_text=slm_generation_text,
        slm_roundtrip_text=None,
        temp_series=temp_series,
        humidity_series=humidity_series,
        temp_x_axis=temp_x_axis,
        temp_y_axis=temp_y_axis,
        humidity_x_axis=humidity_x_axis,
        humidity_y_axis=humidity_y_axis,
        analog_plot_series=analog_plot_series,
        analog_plot_axes=analog_plot_axes,
        hardware_history_slider=hardware_history_slider,
        img_latency_series=img_latency_series,
        img_processing_series=img_processing_series,
        img_render_series=img_render_series,
        img_features_series=img_features_series,
        img_save_series=img_save_series,
        img_compression_series=img_compression_series,
        img_latency_x_axis=img_latency_x_axis,
        img_latency_y_axis=img_latency_y_axis,
        img_processing_x_axis=img_processing_x_axis,
        img_processing_y_axis=img_processing_y_axis,
        img_render_x_axis=img_render_x_axis,
        img_render_y_axis=img_render_y_axis,
        img_features_x_axis=img_features_x_axis,
        img_features_y_axis=img_features_y_axis,
        img_save_x_axis=img_save_x_axis,
        img_save_y_axis=img_save_y_axis,
        img_compression_x_axis=img_compression_x_axis,
        img_compression_y_axis=img_compression_y_axis,
        image_history_slider=image_history_slider,
        monitoring_path_text=monitoring_path_text,
        monitoring_interval_input=monitoring_interval_input,
        monitoring_start_button=monitoring_start_button,
        experiment_script_combo=experiment_script_combo,
        experiment_script_start_button=experiment_script_start_button,
        experiment_script_stop_button=experiment_script_stop_button,
        experiment_script_pause_button=experiment_script_pause_button,
        experiment_script_reload_button=experiment_script_reload_button,
        experiment_script_status_text=experiment_script_status_text,
        experiment_script_info_text=experiment_script_info_text,
        experiment_params_container=experiment_params_container,
        experiment_params_combo=experiment_params_combo,
        experiment_params_save_button=experiment_params_save_button,
        experiment_params_load_button=experiment_params_load_button,
        experiment_params_set_default_button=experiment_params_set_default_button,
        experiment_params_delete_button=experiment_params_delete_button,
        ui_config_combo=ui_config_combo,
        ui_config_save_button=ui_config_save_button,
        ui_config_load_button=ui_config_load_button,
        ui_config_set_default_button=ui_config_set_default_button,
        ui_config_delete_button=ui_config_delete_button,
        finetuning_start_button=finetuning_start_button,
        finetuning_stop_button=finetuning_stop_button,
        finetuning_pause_button=finetuning_pause_button,
        finetuning_progress_bar=finetuning_progress_bar,
        finetuning_status_text=finetuning_status_text,
        finetuning_margin_input=finetuning_margin_input,
        finetuning_samples_input=finetuning_samples_input,
        finetuning_visualization_checkbox=finetuning_visualization_checkbox,
        finetuning_config_combo=finetuning_config_combo,
        finetuning_config_delete_button=finetuning_config_delete_button,
        finetuning_metadata_text=finetuning_metadata_text,
    manual_sample_count_text=manual_sample_count_text,
    manual_status_text=manual_status_text,
    manual_capture_button=manual_capture_button,
    manual_apply_button=manual_apply_button,
    manual_clear_button=manual_clear_button,
        connection_window=connection_window,
        ui_config_window=ui_config_window,
        monitoring_window=monitoring_window,
        experiment_script_window=experiment_script_window,
        viewer_window=viewer_window,
        image_display_window=image_display_window,
        image_saving_window=image_saving_window,
        env_window=env_window,
        hardware_window=hardware_window,
        image_metrics_window=image_metrics_window,
        slm_metrics_window=slm_metrics_window,
        tracking_window=tracking_window,
        slm_window=slm_window,
    )

    # Setup initial window layout - responsive sizing
    # Use viewport callback to maintain layout on resize
    def on_viewport_resize(sender: int, app_data: Any) -> None:
        """Handle viewport resize to maintain responsive layout."""
        try:
            viewport_width = dpg.get_viewport_width()
            viewport_height = dpg.get_viewport_height()
            
            # Calculate responsive dimensions
            left_col_width = max(300, int(viewport_width * 0.13))
            center_width = max(800, int(viewport_width * 0.55))
            right_col_width = viewport_width - left_col_width - center_width - 40
            
            top_row_height = int(viewport_height * 0.70)
            bottom_row_height = viewport_height - top_row_height - 80
            
            # Left column
            dpg.configure_item(connection_window, 
                             pos=(10, 35), 
                             width=left_col_width, 
                             height=int(viewport_height * 0.18))
            dpg.configure_item(ui_config_window,
                             pos=(10, int(viewport_height * 0.18) + 45),
                             width=left_col_width,
                             height=int(viewport_height * 0.10))
            dpg.configure_item(monitoring_window,
                             pos=(10, int(viewport_height * 0.28) + 55),
                             width=left_col_width,
                             height=int(viewport_height * 0.10))
            dpg.configure_item(experiment_script_window,
                             pos=(10, int(viewport_height * 0.38) + 65),
                             width=left_col_width,
                             height=int(viewport_height * 0.15))
            dpg.configure_item(env_window, 
                             pos=(10, int(viewport_height * 0.53) + 75), 
                             width=left_col_width, 
                             height=int(viewport_height * 0.20))
            dpg.configure_item(tracking_window, 
                             pos=(10, int(viewport_height * 0.73) + 85), 
                             width=left_col_width, 
                             height=viewport_height - int(viewport_height * 0.73) - 95)
            
            # Center column
            dpg.configure_item(viewer_window, 
                             pos=(left_col_width + 20, 35), 
                             width=center_width, 
                             height=top_row_height)
            dpg.configure_item(slm_window, 
                             pos=(left_col_width + 20, top_row_height + 45), 
                             width=center_width, 
                             height=bottom_row_height)
            
            # Right column - split into two equal sub-columns
            right_left_width = int(right_col_width * 0.50)
            right_right_width = right_col_width - right_left_width - 10
            right_x = left_col_width + center_width + 30
            
            # Right-left: Display controls, hardware monitoring (no image status)
            dpg.configure_item(image_display_window, 
                             pos=(right_x, 35), 
                             width=right_left_width, 
                             height=int(viewport_height * 0.35))
            dpg.configure_item(hardware_window, 
                             pos=(right_x, int(viewport_height * 0.35) + 45), 
                             width=right_left_width, 
                             height=viewport_height - int(viewport_height * 0.35) - 55)
            
            # Right-right: Saving, metrics
            right_right_x = right_x + right_left_width + 10
            dpg.configure_item(image_saving_window, 
                             pos=(right_right_x, 35), 
                             width=right_right_width, 
                             height=int(viewport_height * 0.35))
            dpg.configure_item(image_metrics_window, 
                             pos=(right_right_x, int(viewport_height * 0.35) + 45), 
                             width=right_right_width, 
                             height=int(viewport_height * 0.45))
            dpg.configure_item(slm_metrics_window, 
                             pos=(right_right_x, int(viewport_height * 0.80) + 55), 
                             width=right_right_width, 
                             height=viewport_height - int(viewport_height * 0.80) - 65)
            
        except Exception as exc:
            logging.exception("Error in viewport resize handler: %s", exc)
    
    # Set initial layout
    dpg.set_viewport_resize_callback(on_viewport_resize)
    on_viewport_resize(0, None)  # Call once to set initial positions

    return ui


def run_ui(controller: AggregateControllerStreaming, ui: AggregateUI) -> None:
    """Run the UI main loop."""
    controller.set_ui(ui)
    # Note: viewport is already shown in create_ui
    
    # Automatically load the active configuration
    try:
        active_config = controller.dashboard_config_manager.get_active_config()
        if active_config:
            config_name = controller.dashboard_config_manager.active_config_name
            logging.info(f"Auto-loading active configuration: {config_name}")
            # Apply the configuration after a short delay to ensure UI is fully initialized
            def auto_load():
                import time
                time.sleep(0.1)  # Small delay to ensure viewport is ready
                
                # Update combo box items to reflect current configs
                if ui.ui_config_combo and dpg.does_item_exist(ui.ui_config_combo):
                    configs = controller.list_ui_configs()
                    dpg.configure_item(ui.ui_config_combo, items=configs)
                
                # Load the active configuration
                success = controller.load_ui_config(config_name)
                if success:
                    logging.info(f"Successfully auto-loaded configuration: {config_name}")
                    # Update combo box value to show loaded config
                    if ui.ui_config_combo and dpg.does_item_exist(ui.ui_config_combo):
                        dpg.set_value(ui.ui_config_combo, config_name)
                else:
                    logging.warning(f"Failed to auto-load configuration: {config_name}")
            
            import threading
            threading.Thread(target=auto_load, daemon=True).start()
    except Exception as exc:
        logging.warning(f"Failed to auto-load active configuration: {exc}")
    
    # Initialize experiment parameters UI with first script if available
    if ui.experiment_script_combo and dpg.does_item_exist(ui.experiment_script_combo):
        script_name = dpg.get_value(ui.experiment_script_combo)
        if script_name and script_name != "No scripts found":
            try:
                if script_name in controller.script_manager.available_scripts:
                    script_class = controller.script_manager.available_scripts[script_name]
                    temp_script = script_class()
                    if hasattr(temp_script, 'get_param_specs'):
                        controller._populate_experiment_params_ui(temp_script)
                        logging.info(f"Initialized parameters for default script: {script_name}")
            except Exception as e:
                logging.warning(f"Failed to initialize parameters for default script: {e}")
    
    while dpg.is_dearpygui_running():
        controller.update()
        dpg.render_dearpygui_frame()
    
    dpg.destroy_context()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Aggregate UI with streaming gRPC")
    parser.add_argument("--services-config", type=Path, default=DEFAULT_SERVICES_CONFIG,
                        help="Path to services configuration YAML")
    parser.add_argument("--image-host", default=None, help="Override image server host")
    parser.add_argument("--image-port", type=int, default=None, help="Override image server port")
    parser.add_argument("--due-host", default=None, help="Override Due server host")
    parser.add_argument("--due-port", type=int, default=None, help="Override Due streaming server port")
    parser.add_argument("--slm-host", default=None, help="Override SLM server host")
    parser.add_argument("--slm-port", type=int, default=None, help="Override SLM server port")
    parser.add_argument("--pin-config", type=Path, default=None,
                       help="Pin configuration JSON (default from services config)")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    args = parse_args(argv)

    # Load dashboard endpoints from services YAML, allowing CLI overrides
    endpoints = _load_dashboard_endpoints(args.services_config)

    image_endpoint = endpoints.get("image", EndpointConfig("127.0.0.1", 50053))
    due_endpoint = endpoints.get("due", EndpointConfig("127.0.0.1", 50052))
    slm_endpoint = endpoints.get("slm", EndpointConfig("127.0.0.1", 50054))

    image_host = args.image_host or image_endpoint.host
    image_port = args.image_port if args.image_port is not None else image_endpoint.port
    due_host = args.due_host or due_endpoint.host
    due_port = args.due_port if args.due_port is not None else due_endpoint.port
    slm_host = args.slm_host or slm_endpoint.host
    slm_port = args.slm_port if args.slm_port is not None else slm_endpoint.port

    image_endpoint = EndpointConfig(image_host, image_port)
    due_endpoint = EndpointConfig(due_host, due_port)
    slm_endpoint = EndpointConfig(slm_host, slm_port)
    
    # Load services config for global settings
    services_config = _load_services_config(args.services_config)
    global_config = services_config.get("global", {})
    
    # Get pin config path from services config or CLI arg
    if args.pin_config:
        pin_config_path = args.pin_config
    else:
        pin_config_default = global_config.get("pin_config_path", "../Arduino/pin_config.json")
        pin_config_path = Path(__file__).parent / pin_config_default
    
    # Load configuration
    config = _load_pin_config(pin_config_path)
    dac_specs, analog_specs, shtc3_labels = _build_channel_specs(config)
    
    # Find SHTC3 spec for connection (if any)
    shtc3_spec: Optional[Shtc3Spec] = None
    for entry in config.values():
        if isinstance(entry, dict) and entry.get("sensor", "").upper() == "SHTC3":
            shtc3_spec = Shtc3Spec(
                name=entry.get("name", "SHTC3"),
                bus=entry.get("bus", 0),
                address=entry.get("address", 0x70),
                frequency_khz=entry.get("frequency_khz", 400),
                unit=entry.get("unit", "DEGREE CELSIUS"),
                label=entry.get("alias", "SHTC3"),
            )
            break
    
    # Load SLM configuration manager (services_config already loaded above)
    slm_config_dir = global_config.get("slm_config_dir", "slm_config")
    feature_config_dir = global_config.get("slm_feature_config_dir", "slm_feature_config")
    
    # Resolve relative path to absolute
    config_path = Path(__file__).parent / slm_config_dir
    feature_config_path = Path(__file__).parent / feature_config_dir
    slm_config_manager = SlmConfigManager(config_path)
    slm_feature_config_manager = SlmFeatureConfigManager(feature_config_path)
    
    # Load tracking configuration manager
    tracking_config_dir = global_config.get("tracking_config_dir", "../Camera/tracking_config")
    tracking_config_path = Path(__file__).parent / tracking_config_dir
    tracking_config_manager = TrackingConfigManager(tracking_config_path)
    
    # Load dashboard UI configuration manager
    dashboard_config_path = Path(__file__).parent / "dashboard_config.yaml"
    dashboard_config_manager = DashboardConfigManager(dashboard_config_path)
    
    # Create components
    image_state = ImageAppState(
        host=image_endpoint.host,
        port=image_endpoint.port,
        zoom=DEFAULT_DISPLAY_SCALE,
    )
    image_client = ImageClient(image_state)
    
    controller = AggregateControllerStreaming(
        image_state=image_state,
        image_client=image_client,
        dac_specs=dac_specs,
        analog_specs=analog_specs,
        shtc3_spec=shtc3_spec,
        due_endpoint=due_endpoint,
        slm_endpoint=slm_endpoint,
        slm_config_manager=slm_config_manager,
        slm_feature_config_manager=slm_feature_config_manager,
        tracking_config_manager=tracking_config_manager,
        dashboard_config_manager=dashboard_config_manager,
    )
    
    ui = create_ui(controller, shtc3_labels)
    
    try:
        run_ui(controller, ui)
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        controller.shutdown()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
