"""
Base classes for experiment automation scripts.

Provides abstract interfaces that experiment scripts should implement,
similar to backtesting frameworks for algorithmic trading.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Callable, Union
import numpy as np
from datetime import datetime


@dataclass
class ParamSpec:
    """
    Specification for a configurable parameter.
    
    Defines metadata about a parameter that the dashboard can use to
    automatically generate UI controls and handle serialization.
    """
    name: str
    """Internal parameter name (attribute name on the script)"""
    
    label: str
    """Human-readable label for UI"""
    
    param_type: type
    """Python type: float, int, bool, str, or tuple"""
    
    default: Any
    """Default value"""
    
    min_value: Optional[Union[float, int]] = None
    """Minimum value for numeric types"""
    
    max_value: Optional[Union[float, int]] = None
    """Maximum value for numeric types"""
    
    step: Optional[Union[float, int]] = None
    """Step size for numeric inputs"""
    
    description: str = ""
    """Tooltip/help text"""
    
    unit: str = ""
    """Unit of measurement (e.g., 'px', 's', 'Hz')"""
    
    category: str = "General"
    """Parameter category for grouping in UI"""
    
    format_str: str = "%.2f"
    """Format string for display"""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'name': self.name,
            'label': self.label,
            'param_type': self.param_type.__name__,
            'default': self.default,
            'min_value': self.min_value,
            'max_value': self.max_value,
            'step': self.step,
            'description': self.description,
            'unit': self.unit,
            'category': self.category,
            'format_str': self.format_str,
        }


@dataclass
class ExperimentContext:
    """
    Provides read/write access to all dashboard components.
    
    This context object is passed to experiment scripts on each frame,
    giving them access to current state and control handles.
    """
    
    # ===== Read-only data streams =====
    current_image: Optional[np.ndarray] = None
    """Current raw image from camera (numpy array)"""
    
    tracked_positions: List[Tuple[float, float, float]] = field(default_factory=list)
    """List of tracked particle positions: [(x, y, mass), ...]"""
    
    tracking_metadata: Dict[str, Any] = field(default_factory=dict)
    """Additional tracking information (tile info, processing time, etc.)"""
    
    frame_number: int = 0
    """Current frame sequence number"""
    
    timestamp: float = 0.0
    """Timestamp of current frame (seconds since epoch)"""
    
    # ===== Control handles (for script to call methods) =====
    slm_client: Any = None
    """SLM client instance for hologram control"""
    
    due_manager: Any = None
    """Arduino Due manager for DAC/analog control"""
    
    # ===== Current state snapshots =====
    slm_points: List[Tuple[float, float, float, float]] = field(default_factory=list)
    """Current SLM trap positions: [(x, y, z, intensity), ...]"""
    
    dac_states: Dict[str, float] = field(default_factory=dict)
    """Current DAC channel values: {channel_name: voltage}"""
    
    analog_states: Dict[str, float] = field(default_factory=dict)
    """Current analog input readings: {channel_name: value}"""
    
    # ===== Configuration objects =====
    slm_config: Any = None
    """Current SLM configuration"""
    
    tracking_config: Any = None
    """Current tracking configuration"""
    
    feature_config: Any = None
    """Current feature configuration"""
    
    # ===== Metrics history (read-only) =====
    metrics_history: Dict[str, Any] = field(default_factory=dict)
    """Historical performance metrics"""
    
    # ===== Internal callback handles (set by dashboard) =====
    _set_slm_points_callback: Optional[Callable] = None
    _set_dac_value_callback: Optional[Callable] = None
    _log_callback: Optional[Callable] = None
    _get_particle_at_callback: Optional[Callable] = None
    
    # ===== Public API methods for scripts =====
    
    def set_slm_points(self, points: List[Tuple[float, float, float, float]]) -> None:
        """
        Update SLM trap positions.
        
        Args:
            points: List of (x, y, z, intensity) tuples
                   x, y: pixel coordinates in SLM space
                   z: focal depth offset (arbitrary units)
                   intensity: 0.0 to 1.0
        """
        if self._set_slm_points_callback:
            self._set_slm_points_callback(points)
    
    def set_dac_value(self, channel: str, value: float) -> None:
        """
        Set DAC channel voltage.
        
        Args:
            channel: Channel identifier (e.g., 'vdc_1', 'vac_x')
            value: Voltage value (range depends on channel config)
        """
        if self._set_dac_value_callback:
            self._set_dac_value_callback(channel, value)
    
    def get_particle_at(self, x: float, y: float, radius: float = 20.0) -> Optional[Dict[str, Any]]:
        """
        Find tracked particle near specified coordinates.
        
        Args:
            x: X coordinate (pixels)
            y: Y coordinate (pixels)
            radius: Search radius (pixels)
            
        Returns:
            Dictionary with particle info: {'x': float, 'y': float, 'mass': float}
            or None if no particle found
        """
        if self._get_particle_at_callback:
            return self._get_particle_at_callback(x, y, radius)
        
        # Fallback: manual search
        closest_particle = None
        min_distance = radius
        
        for px, py, mass in self.tracked_positions:
            dist = np.sqrt((px - x)**2 + (py - y)**2)
            if dist < min_distance:
                min_distance = dist
                closest_particle = {'x': px, 'y': py, 'mass': mass, 'distance': dist}
        
        return closest_particle
    
    def log(self, message: str, level: str = "INFO") -> None:
        """
        Log message to experiment log.
        
        Args:
            message: Log message
            level: Log level ('DEBUG', 'INFO', 'WARNING', 'ERROR')
        """
        if self._log_callback:
            self._log_callback(message, level)
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] [{level}] {message}")


class ExperimentScript(ABC):
    """
    Abstract base class for experiment automation scripts.
    
    Subclass this to create custom experiment behaviors. The script will be
    called on each frame with updated context information.
    
    Example:
        class MyExperiment(ExperimentScript):
            def __init__(self):
                super().__init__()
                self.name = "My Experiment"
                self.description = "Does something cool"
            
            def setup(self, ctx: ExperimentContext) -> bool:
                ctx.log("Starting experiment")
                return True
            
            def on_frame(self, ctx: ExperimentContext) -> bool:
                # Your experiment logic here
                return True  # Continue running
            
            def teardown(self, ctx: ExperimentContext) -> None:
                ctx.log("Experiment complete")
    """
    
    def __init__(self):
        self.name: str = "Unnamed Script"
        """Display name for this script"""
        
        self.description: str = "No description"
        """Brief description of what this script does"""
        
        self.is_running: bool = False
        """True if script is currently running"""
        
        self.is_paused: bool = False
        """True if script is paused"""
        
        self.frame_count: int = 0
        """Number of frames processed by this script"""
        
        self.error_count: int = 0
        """Number of errors encountered"""
        
        # Parameter specifications for auto-configuration
        self._param_specs: Dict[str, ParamSpec] = {}
        """Registry of configurable parameters"""
    
    def register_param(self, spec: ParamSpec) -> None:
        """
        Register a parameter for automatic UI generation.
        
        Args:
            spec: Parameter specification with metadata
        """
        self._param_specs[spec.name] = spec
        # Set default value if not already set
        if not hasattr(self, spec.name):
            setattr(self, spec.name, spec.default)
    
    def get_param_specs(self) -> Dict[str, ParamSpec]:
        """Get all registered parameter specifications."""
        return self._param_specs.copy()
    
    def get_param_value(self, name: str) -> Any:
        """Get current value of a parameter."""
        return getattr(self, name, None)
    
    def set_param_value(self, name: str, value: Any) -> bool:
        """
        Set value of a parameter.
        
        Args:
            name: Parameter name
            value: New value
            
        Returns:
            True if successful, False if parameter doesn't exist
        """
        if hasattr(self, name):
            setattr(self, name, value)
            return True
        return False
    
    def get_all_param_values(self) -> Dict[str, Any]:
        """Get current values of all registered parameters."""
        return {name: self.get_param_value(name) for name in self._param_specs.keys()}
    
    def set_params_from_dict(self, params: Dict[str, Any]) -> None:
        """Set multiple parameters from a dictionary."""
        for name, value in params.items():
            if name in self._param_specs:
                self.set_param_value(name, value)
    
    @abstractmethod
    def setup(self, ctx: ExperimentContext) -> bool:
        """
        Called once before script execution starts.
        
        Use this to initialize state, validate conditions, etc.
        
        Args:
            ctx: Experiment context with current system state
            
        Returns:
            True to proceed with script execution, False to abort
        """
        pass
    
    @abstractmethod
    def on_frame(self, ctx: ExperimentContext) -> bool:
        """
        Called for each new frame from the camera.
        
        This is the main entry point for experiment logic. It will be called
        repeatedly until it returns False or is stopped externally.
        
        Args:
            ctx: Experiment context with current system state
            
        Returns:
            True to continue execution, False to stop script
        """
        pass
    
    @abstractmethod
    def teardown(self, ctx: ExperimentContext) -> None:
        """
        Called once after script stops (either completed or stopped).
        
        Use this to clean up resources, save data, reset hardware state, etc.
        
        Args:
            ctx: Experiment context with current system state
        """
        pass
    
    def on_pause(self, ctx: ExperimentContext) -> None:
        """
        Called when script is paused.
        
        Override to perform actions when paused (optional).
        
        Args:
            ctx: Experiment context with current system state
        """
        pass
    
    def on_resume(self, ctx: ExperimentContext) -> None:
        """
        Called when script resumes from pause.
        
        Override to perform actions when resuming (optional).
        
        Args:
            ctx: Experiment context with current system state
        """
        pass
    
    def on_error(self, ctx: ExperimentContext, error: Exception) -> bool:
        """
        Called when an error occurs during execution.
        
        Override to implement custom error handling.
        
        Args:
            ctx: Experiment context with current system state
            error: The exception that was raised
            
        Returns:
            True to continue execution, False to stop script
        """
        self.error_count += 1
        ctx.log(f"Error in {self.name}: {error}", "ERROR")
        return False  # Stop by default
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get script statistics.
        
        Returns:
            Dictionary with script statistics
        """
        return {
            'name': self.name,
            'is_running': self.is_running,
            'is_paused': self.is_paused,
            'frame_count': self.frame_count,
            'error_count': self.error_count,
        }
