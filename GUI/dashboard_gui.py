"""Aggregate control dashboard with streaming gRPC for reduced latency."""

from __future__ import annotations

import argparse
import csv
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
from dataclasses import dataclass
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

# Tracking Config imports
import sys
sys.path.insert(0, str(_REPO_ROOT / "Camera"))
from tracking_config.tracking_config_manager import TrackingConfigManager, TrackingConfig  # type: ignore  # noqa: E402


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
    intensity: float


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
    image_connection_status_label: Optional[int] = None
    due_connection_status_label: Optional[int] = None
    slm_connection_status_label: Optional[int] = None
    image_target_label: Optional[int] = None
    due_target_label: Optional[int] = None
    slm_target_label: Optional[int] = None
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

    def send_command(self, points: List[SlmPoint], affine_params: Optional[Dict[str, float]] = None) -> None:
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
            point.z = 0.0
            point.intensity = pt.intensity
        
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


AFFINE_FIELDS = [
    "translate_x", "translate_y", "translate_z",
    "rotate_x_deg", "rotate_y_deg", "rotate_z_deg",
    "scale_x", "scale_y", "scale_z",
    "shear_xy", "shear_yz", "shear_xz",
]

DEFAULT_SLM_WIDTH = 1920
DEFAULT_SLM_HEIGHT = 1152
DEFAULT_POINT_INTENSITY = 0.9
POINT_SELECTION_RADIUS_PX = 18.0
SLM_SEND_DEBOUNCE_SECONDS = 1.0 / 30.0  # 30 FPS (0.0333 seconds)
DEFAULT_METRICS_HISTORY = 1000
MOVING_AVERAGE_WINDOW = 50  # Number of points to average for plot display

# UI Layout Constants
PLOT_HEIGHT = 200
HARDWARE_WINDOW_HEIGHT = 900
IMAGE_METRICS_WINDOW_HEIGHT = 600
SLM_METRICS_WINDOW_HEIGHT = 300

# Color Scheme - Distinct section colors
HARDWARE_COLOR = (220, 80, 80, 255)      # Reddish - Hardware/Arduino
IMAGE_COLOR = (80, 200, 90, 255)         # Greenish - Image/Camera
SLM_COLOR = (230, 180, 60, 255)          # Yellowish - SLM/Hologram
STATUS_CONNECTED = (80, 220, 90, 255)    # Bright green for connected
STATUS_DISCONNECTED = (220, 60, 60, 255) # Red for disconnected
TEXT_PRIMARY = (230, 230, 230, 255)      # Light gray text
TEXT_SECONDARY = (180, 180, 180, 255)    # Secondary text


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
        tracking_config_manager: TrackingConfigManager,
    ) -> None:
        self.image_state = image_state
        self.image_client = image_client
        self.due_manager = DueManagerStreaming()
        self.slm_client = SLMClient()
        self.image_endpoint = EndpointConfig(image_state.host, image_state.port)
        self.due_endpoint = due_endpoint
        self.slm_endpoint = slm_endpoint
        
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
        
        # Tracking Configuration Manager
        self.tracking_config_manager = tracking_config_manager
        
        # Load current tracking configuration
        current_tracking_config = self.tracking_config_manager.get_current_config()
        self.current_tracking_params = current_tracking_config.get_detection_params()
        
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

    def set_ui(self, ui: AggregateUI) -> None:
        self.ui = ui

    # Connection management
    
    def connect_image(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        host = host or self.image_endpoint.host
        port = port if port is not None else self.image_endpoint.port
        self.image_state.host = host
        self.image_state.port = port
        self.image_endpoint = EndpointConfig(host, port)
        self.image_client.connect(host, port)
        logging.info("Connected to image server at %s:%d", host, port)

    def disconnect_image(self) -> None:
        self.image_client.disconnect()

    def connect_due(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        host = host or self.due_endpoint.host
        port = port if port is not None else self.due_endpoint.port
        target = f"{host}:{port}"
        self.due_endpoint = EndpointConfig(host, port)
        self.due_manager.connect(target, shtc3_spec=self.shtc3_spec)

    def disconnect_due(self) -> None:
        self.due_manager.disconnect()

    def connect_slm(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        host = host or self.slm_endpoint.host
        port = port if port is not None else self.slm_endpoint.port
        self.slm_endpoint = EndpointConfig(host, port)
        target = f"{host}:{port}"
        self.slm_client.connect(target)

    def disconnect_slm(self) -> None:
        self.slm_client.disconnect()

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
        self.slm_points.append(SlmPoint(x, y, DEFAULT_POINT_INTENSITY))
        self._mark_slm_dirty()
        logging.info("Added SLM point at (%.1f, %.1f), total: %d", x, y, len(self.slm_points))

    def move_point(self, index: int, x: float, y: float) -> None:
        """Move an existing SLM point to new coordinates."""
        if 0 <= index < len(self.slm_points):
            point = self.slm_points[index]
            self.slm_points[index] = SlmPoint(x, y, point.intensity)
            self._mark_slm_dirty()

    def remove_point(self, index: int) -> None:
        """Remove an SLM point by index."""
        if 0 <= index < len(self.slm_points):
            self.slm_points.pop(index)
            self._mark_slm_dirty()
            logging.info("Removed SLM point at index %d, remaining: %d", index, len(self.slm_points))

    def clear_points(self) -> None:
        """Clear all SLM points."""
        self.slm_points.clear()
        self._mark_slm_dirty()
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
            self.slm_client.send_command(self.slm_points, self.slm_affine_params)
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
        if self.ui is None or not hasattr(self.ui, 'tracking_inputs'):
            return
        
        try:
            for param_name, input_id in self.ui.tracking_inputs.items():
                if param_name in self.current_tracking_params:
                    value = self.current_tracking_params[param_name]
                    dpg.set_value(input_id, value)
        except Exception as exc:
            logging.error("Failed to update tracking parameters UI: %s", exc)
    
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
            
        except Exception as exc:
            logging.exception("Error in update loop: %s", exc)

    def _update_image_view_from_state(self) -> None:
        """Update image texture from image state."""
        if not self.ui:
            return
        
        # Use overlay if available, otherwise use latest image
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
            else:
                # Same size, just update the data
                dpg.set_value(self.ui.texture_id, flat)
                dpg.configure_item(self.ui.image_item, width=display_w, height=display_h)
        except Exception as exc:
            logging.exception("Error updating image view: %s", exc)

    def _draw_slm_circles(self, image: np.ndarray, scale: float) -> np.ndarray:
        """Draw circles on the image at SLM point locations.
        
        Args:
            image: RGB image array (H, W, 3)
            scale: Current zoom scale factor
            
        Returns:
            Image with circles drawn
        """
        if not self.slm_points or not self.slm_client.connected:
            return image
        
        import cv2
        
        # Make a copy so we don't modify the original
        img_with_circles = image.copy()
        
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
                    dpg.add_text(f"{idx}: ({point.x:.1f}, {point.y:.1f})", color=TEXT_PRIMARY, tag=f"slm_point_{idx}")
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

    def shutdown(self) -> None:
        """Shutdown all connections."""
        self.stop_monitoring()
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
        if controller.ui and hasattr(controller.ui, 'tracking_config_combo'):
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
    if controller.ui and hasattr(controller.ui, 'tracking_config_combo'):
        selected_config = dpg.get_value(controller.ui.tracking_config_combo)
        controller.load_tracking_config(selected_config)


def _on_tracking_config_set_default(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle setting current configuration as default."""
    controller = user_data
    if controller.ui and hasattr(controller.ui, 'tracking_config_combo'):
        selected_config = dpg.get_value(controller.ui.tracking_config_combo)
        controller.set_default_tracking_config(selected_config)


def _on_tracking_config_reset(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle resetting tracking configuration to default."""
    controller = user_data
    controller.reset_tracking_config_to_default()
    
    # Update the dropdown selection
    if controller.ui and hasattr(controller.ui, 'tracking_config_combo'):
        dpg.set_value(controller.ui.tracking_config_combo, "default")


def _on_tracking_config_delete(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle deleting selected tracking configuration."""
    controller = user_data
    if controller.ui and hasattr(controller.ui, 'tracking_config_combo'):
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
        # Update the dropdown
        if controller.ui and hasattr(controller.ui, 'tracking_config_combo'):
            configs = controller.list_tracking_configs()
            current_config = controller.get_current_tracking_config_name()
            dpg.configure_item(controller.ui.tracking_config_combo, items=configs, default_value=current_config)
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


def _on_auto_save_raw_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle auto save raw toggle."""
    controller = user_data
    desired = bool(app_data)
    try:
        controller.image_client.update_storage_config({"enabled": desired})
    except Exception as exc:
        logging.exception("Failed to update storage config: %s", exc)


def _on_auto_save_overlay_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle auto save overlay toggle."""
    controller = user_data
    controller.image_state.set_auto_save_overlay(bool(app_data))


def _on_save_hdf5_toggled(sender: int, app_data: Any, user_data: AggregateControllerStreaming) -> None:
    """Handle HDF5 saving toggle."""
    controller = user_data
    desired = bool(app_data)
    try:
        controller.image_client.update_storage_config({"hdf5_enabled": desired})
    except Exception as exc:
        logging.exception("Failed to update HDF5 config: %s", exc)


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
        auto_save_raw_checkbox = dpg.add_checkbox(
            label="Server File Saving",
            default_value=controller.image_state.auto_save_raw,
            callback=_on_auto_save_raw_toggled,
            user_data=controller,
        )
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
            f"Storage format: {controller.image_state.storage_image_format} (TIFF + HDF5)",
            color=TEXT_SECONDARY,
        )
        save_hdf5_checkbox = dpg.add_checkbox(
            label="Server HDF5 Recording",
            default_value=controller.image_state.save_to_hdf5,
            callback=_on_save_hdf5_toggled,
            user_data=controller,
        )
        
        dpg.add_spacing(count=1)
        raw_dir_display = dpg.add_input_text(
            label="Raw Folder",
            default_value=str(controller.image_state.raw_save_dir),
            readonly=True,
        )
        hdf5_path_display = dpg.add_input_text(
            label="HDF5 File",
            default_value=str(controller.image_state.hdf5_path) if controller.image_state.hdf5_path else "(auto)",
            readonly=True,
        )
        overlay_dir_display = dpg.add_input_text(
            label="Overlay Folder",
            default_value=str(controller.image_state.overlay_save_dir),
            readonly=True,
        )
        
        dpg.add_spacing(count=2)
        save_overlay_button = dpg.add_button(
            label="Save Current Overlay",
            callback=_on_save_overlay_clicked,
            user_data=controller,
            width=-1,
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
                    increment_input = dpg.add_input_float(label=f"##inc_{name}", default_value=0.01, width=80)
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
        dpg.add_text("POINT LIST", color=SLM_COLOR)
        dpg.add_text("Click on image to add, right-click to remove", color=TEXT_SECONDARY)
        dpg.add_spacing(count=1)
        
        # Scrollable list of points
        with dpg.child_window(height=150, border=True):
            slm_points_list_group = dpg.add_group()
        
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
        auto_save_overlay_checkbox=None,  # Not implemented yet
        save_hdf5_checkbox=save_hdf5_checkbox,
        storage_target_fps_input=storage_target_fps_input,
        save_overlay_button=save_overlay_button,
        raw_dir_display=raw_dir_display,
        overlay_dir_display=overlay_dir_display,
        hdf5_path_display=hdf5_path_display,
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
                             height=int(viewport_height * 0.23))
            dpg.configure_item(monitoring_window,
                             pos=(10, int(viewport_height * 0.23) + 45),
                             width=left_col_width,
                             height=int(viewport_height * 0.12))
            dpg.configure_item(env_window, 
                             pos=(10, int(viewport_height * 0.35) + 55), 
                             width=left_col_width, 
                             height=int(viewport_height * 0.32))
            dpg.configure_item(tracking_window, 
                             pos=(10, int(viewport_height * 0.67) + 65), 
                             width=left_col_width, 
                             height=viewport_height - int(viewport_height * 0.67) - 75)
            
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
    parser.add_argument("--pin-config", type=Path, default=_REPO_ROOT / "Arduino" / "pin_config.json",
                       help="Pin configuration JSON")
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
    
    # Load configuration
    config = _load_pin_config(args.pin_config)
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
    
    # Load SLM configuration manager
    dashboard_endpoints = _load_dashboard_endpoints(args.services_config)
    services_config = _load_services_config(args.services_config)
    global_config = services_config.get("global", {})
    slm_config_dir = global_config.get("slm_config_dir", "slm_config")
    
    # Resolve relative path to absolute
    config_path = Path(__file__).parent / slm_config_dir
    slm_config_manager = SlmConfigManager(config_path)
    
    # Load tracking configuration manager
    tracking_config_dir = global_config.get("tracking_config_dir", "../Camera/tracking_config")
    tracking_config_path = Path(__file__).parent / tracking_config_dir
    tracking_config_manager = TrackingConfigManager(tracking_config_path)
    
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
        tracking_config_manager=tracking_config_manager,
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
