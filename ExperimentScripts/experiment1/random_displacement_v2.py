"""
Experiment 1: Random Particle Displacement (Version 2 - Auto-Configured)

This version uses the new auto-configuration system where parameters
are defined once and automatically become configurable in the dashboard.
"""

import sys
from pathlib import Path
import csv
from datetime import datetime

# Add parent directory to path for imports
_script_dir = Path(__file__).resolve().parent
_experiments_root = _script_dir.parent
if str(_experiments_root) not in sys.path:
    sys.path.insert(0, str(_experiments_root))

from base_script import ExperimentScript, ExperimentContext, ParamSpec
import numpy as np
import time
from enum import Enum
from typing import Optional, Tuple


class ExperimentState(Enum):
    """States for the experiment state machine"""
    SELECTING_PARTICLE = 1
    MOVING_PARTICLE = 2
    WAITING = 3


class RandomParticleDisplacementV2(ExperimentScript):
    """
    Randomly displaces particles using SLM traps (Auto-Configured Version).
    
    All parameters are automatically configurable through the dashboard
    by registering them with ParamSpec metadata.
    """
    
    def __init__(self):
        super().__init__()
        self.name = "Random Particle Displacement (Auto-Config)"
        self.description = (
            "Randomly selects particles and moves them to new positions using SLM traps. "
            "All parameters are automatically configurable."
        )
        
        # Register all configurable parameters with metadata
        # Movement parameters
        self.register_param(ParamSpec(
            name='movement_duration_min',
            label='Move Time Min',
            param_type=float,
            default=0.5,
            min_value=0.1,
            max_value=10.0,
            step=0.1,
            unit='s',
            category='Movement',
            description='Minimum time to complete a movement',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='movement_duration_max',
            label='Move Time Max',
            param_type=float,
            default=4.0,
            min_value=0.1,
            max_value=10.0,
            step=0.1,
            unit='s',
            category='Movement',
            description='Maximum time to complete a movement',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='move_distance_min',
            label='Distance Min',
            param_type=float,
            default=20.0,
            min_value=1.0,
            max_value=500.0,
            step=1.0,
            unit='px',
            category='Movement',
            description='Minimum displacement distance',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='move_distance_max',
            label='Distance Max',
            param_type=float,
            default=32.0,
            min_value=1.0,
            max_value=500.0,
            step=1.0,
            unit='px',
            category='Movement',
            description='Maximum displacement distance',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='delay_between_actions',
            label='Delay',
            param_type=float,
            default=4.0,
            min_value=0.0,
            max_value=60.0,
            step=0.5,
            unit='s',
            category='Timing',
            description='Wait time between consecutive actions',
            format_str='%.1f'
        ))
        
        # Spatial constraints
        self.register_param(ParamSpec(
            name='min_separation_distance',
            label='Min Separation',
            param_type=float,
            default=64.0,
            min_value=0.0,
            max_value=500.0,
            step=1.0,
            unit='px',
            category='Constraints',
            description='Minimum distance between consecutive actions',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='edge_margin',
            label='Edge Margin',
            param_type=float,
            default=64.0,
            min_value=0.0,
            max_value=500.0,
            step=1.0,
            unit='px',
            category='Constraints',
            description='Minimum distance from image edges',
            format_str='%.1f'
        ))
        
        # SLM parameters
        self.register_param(ParamSpec(
            name='slm_max_refresh_rate',
            label='SLM Refresh',
            param_type=float,
            default=30.0,
            min_value=1.0,
            max_value=240.0,
            step=1.0,
            unit='Hz',
            category='SLM',
            description='Maximum SLM refresh rate',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='trap_intensity',
            label='Trap Intensity',
            param_type=float,
            default=0.9,
            min_value=0.0,
            max_value=1.0,
            step=0.05,
            unit='',
            category='SLM',
            description='Trap intensity (0-1)',
            format_str='%.2f'
        ))
        
        self.register_param(ParamSpec(
            name='trap_z_offset',
            label='Trap Z Offset',
            param_type=float,
            default=0.0,
            min_value=-100.0,
            max_value=100.0,
            step=1.0,
            unit='',
            category='SLM',
            description='Z-axis offset for traps',
            format_str='%.1f'
        ))
        
        # Performance parameters
        self.register_param(ParamSpec(
            name='assumed_fps',
            label='Assumed FPS',
            param_type=float,
            default=30.0,
            min_value=1.0,
            max_value=240.0,
            step=1.0,
            unit='Hz',
            category='Performance',
            description='Assumed frame rate for speed calculations',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='target_reached_threshold',
            label='Target Threshold',
            param_type=float,
            default=5.0,
            min_value=0.1,
            max_value=50.0,
            step=0.5,
            unit='px',
            category='Performance',
            description='Distance to consider target reached',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='step_close_enough_threshold',
            label='Step Threshold',
            param_type=float,
            default=0.5,
            min_value=0.1,
            max_value=10.0,
            step=0.1,
            unit='px',
            category='Performance',
            description='Threshold for multi-step movement precision',
            format_str='%.1f'
        ))
        
        # Logging parameters
        self.register_param(ParamSpec(
            name='log_progress_interval',
            label='Log Progress',
            param_type=int,
            default=10,
            min_value=1,
            max_value=300,
            step=5,
            unit='frames',
            category='Logging',
            description='How often to log movement progress',
            format_str='%d'
        ))
        
        self.register_param(ParamSpec(
            name='log_status_interval',
            label='Log Status',
            param_type=int,
            default=30,
            min_value=1,
            max_value=300,
            step=5,
            unit='frames',
            category='Logging',
            description='How often to log status messages',
            format_str='%d'
        ))
        
        # Image dimensions (will be set from context)
        self.image_width = 0.0
        self.image_height = 0.0
        
        # Current cycle parameters (randomized each cycle)
        self.current_movement_duration = 0.0
        self.current_movement_speed = 0.0
        self.last_action_position: Optional[Tuple[float, float]] = None
        
        # CSV logging
        self.csv_file: Optional[Path] = None
        self.csv_writer: Optional[csv.DictWriter] = None
        self.csv_file_handle = None
        
        # Current action tracking for CSV
        self.action_start_frame = 0
        self.action_initial_x = 0.0
        self.action_initial_y = 0.0
        self.action_target_x = 0.0
        self.action_target_y = 0.0
        self.action_start_time = 0.0
        
        # State machine
        self.state = ExperimentState.SELECTING_PARTICLE
        self.state_start_time = 0.0
        
        # Current operation tracking
        self.selected_particle: Optional[Tuple[float, float, float]] = None
        self.target_position: Optional[Tuple[float, float]] = None
        self.trap_positions: list = []
        self.cycles_completed = 0
        
        # Frame counting for movement
        self.frames_in_state = 0
    
    def setup(self, ctx: ExperimentContext) -> bool:
        """Initialize the experiment"""
        ctx.log(f"=== {self.name} Starting ===", "INFO")
        ctx.log(f"Parameters:", "INFO")
        
        # Automatically log all registered parameters
        for name, spec in self._param_specs.items():
            value = self.get_param_value(name)
            if spec.unit:
                ctx.log(f"  {spec.label}: {value} {spec.unit}", "INFO")
            else:
                ctx.log(f"  {spec.label}: {value}", "INFO")
        
        # Get image dimensions from context
        if ctx.current_image is not None:
            self.image_height, self.image_width = ctx.current_image.shape[:2]
            ctx.log(f"  Image dimensions: {self.image_width}x{self.image_height} pixels", "INFO")
        else:
            ctx.log("WARNING: No image available, using default dimensions 1920x1200", "WARNING")
            self.image_width = 1920.0
            self.image_height = 1200.0
        
        # Check if we have tracking data
        if not ctx.tracked_positions:
            ctx.log("WARNING: No particles currently tracked. Experiment will start when particles are detected.", "WARNING")
        
        self.state = ExperimentState.SELECTING_PARTICLE
        self.state_start_time = time.time()
        self.cycles_completed = 0
        self.last_action_position = None
        
        # Setup CSV logging
        self._setup_csv_logging(ctx)
        
        return True
    
    def on_frame(self, ctx: ExperimentContext) -> bool:
        """Process each frame based on current state"""
        self.frames_in_state += 1
        
        # State machine
        if self.state == ExperimentState.SELECTING_PARTICLE:
            return self._state_selecting_particle(ctx)
        
        elif self.state == ExperimentState.MOVING_PARTICLE:
            return self._state_moving_particle(ctx)
        
        elif self.state == ExperimentState.WAITING:
            return self._state_waiting(ctx)
        
        return True
    
    def _state_selecting_particle(self, ctx: ExperimentContext) -> bool:
        """Select a random particle and decide where to move it"""
        
        # Check if we have particles
        if not ctx.tracked_positions:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log("Waiting for particles to be tracked...", "INFO")
            return True
        
        # Update image dimensions if available
        if ctx.current_image is not None:
            self.image_height, self.image_width = ctx.current_image.shape[:2]
        
        # Filter particles that meet the constraints
        valid_particles = []
        for idx, (px, py, mass) in enumerate(ctx.tracked_positions):
            # Check edge margin constraint
            if (px < self.edge_margin or px > self.image_width - self.edge_margin or
                py < self.edge_margin or py > self.image_height - self.edge_margin):
                continue
            
            # Check separation from last action
            if self.last_action_position is not None:
                last_x, last_y = self.last_action_position
                distance_from_last = np.sqrt((px - last_x)**2 + (py - last_y)**2)
                if distance_from_last < self.min_separation_distance:
                    continue
            
            valid_particles.append((idx, px, py, mass))
        
        if not valid_particles:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log(f"No valid particles found (edge margin: {self.edge_margin}px, separation: {self.min_separation_distance}px)", "WARNING")
            return True
        
        # Randomly select a valid particle
        _, px, py, mass = valid_particles[np.random.randint(0, len(valid_particles))]
        self.selected_particle = (px, py, mass)
        
        ctx.log(f"Selected particle at ({px:.1f}, {py:.1f}) with mass {mass:.1f}", "INFO")
        
        # Decide on random displacement distance and direction
        distance = np.random.uniform(self.move_distance_min, self.move_distance_max)
        angle = np.random.uniform(0, 2 * np.pi)
        
        dx = distance * np.cos(angle)
        dy = distance * np.sin(angle)
        
        self.target_position = (px + dx, py + dy)
        tx, ty = self.target_position
        
        # Record this action position
        self.last_action_position = (px, py)
        
        # Randomize movement duration and calculate required speed
        self.current_movement_duration = np.random.uniform(self.movement_duration_min, self.movement_duration_max)
        expected_frames = self.current_movement_duration * self.assumed_fps
        self.current_movement_speed = distance / expected_frames if expected_frames > 0 else distance
        
        ctx.log(f"Target position: ({tx:.1f}, {ty:.1f}) - displacement: {distance:.1f} pixels", "INFO")
        ctx.log(f"Movement duration: {self.current_movement_duration:.2f}s at {self.current_movement_speed:.2f} px/frame", "INFO")
        
        # Record action start for CSV logging
        self.action_start_frame = ctx.frame_number
        self.action_initial_x = px
        self.action_initial_y = py
        self.action_target_x = tx
        self.action_target_y = ty
        self.action_start_time = time.time()
        
        # Create SLM trap at starting position
        self.trap_positions = [(px, py, self.trap_z_offset, self.trap_intensity)]
        ctx.set_slm_points(self.trap_positions)
        
        # Transition to moving state
        self._transition_to_state(ExperimentState.MOVING_PARTICLE, ctx)
        
        return True
    
    def _state_moving_particle(self, ctx: ExperimentContext) -> bool:
        """Gradually move the trap from current position to target"""
        
        if not self.selected_particle or not self.target_position:
            ctx.log("ERROR: Missing particle or target in MOVING state", "ERROR")
            self._transition_to_state(ExperimentState.SELECTING_PARTICLE, ctx)
            return True
        
        # Get current trap position
        if not self.trap_positions:
            ctx.log("ERROR: No trap positions in MOVING state", "ERROR")
            self._transition_to_state(ExperimentState.SELECTING_PARTICLE, ctx)
            return True
        
        current_x, current_y, current_z, current_intensity = self.trap_positions[0]
        target_x, target_y = self.target_position
        
        # Calculate distance to target
        dx = target_x - current_x
        dy = target_y - current_y
        distance = np.sqrt(dx**2 + dy**2)
        
        # Check if we've reached the target
        if distance < self.target_reached_threshold:
            ctx.log(f"Reached target position ({target_x:.1f}, {target_y:.1f})", "INFO")
            
            # Log action completion to CSV
            self._log_action_to_csv(ctx)
            
            # Clear SLM traps immediately when action is complete
            ctx.set_slm_points([])
            self.trap_positions = []
            ctx.log("Action complete - cleared SLM hologram", "INFO")
            
            # Transition to waiting state
            self._transition_to_state(ExperimentState.WAITING, ctx)
            return True
        
        # Calculate movement considering SLM refresh rate
        refresh_period = 1.0 / self.slm_max_refresh_rate if self.slm_max_refresh_rate > 0 else 1.0 / 30.0
        frame_period = 1.0 / self.assumed_fps if self.assumed_fps > 0 else 1.0 / 30.0
        updates_per_frame = max(1, int(frame_period / refresh_period))
        
        # Adjust step size based on refresh rate
        base_step_size = min(self.current_movement_speed, distance)
        step_size = base_step_size / updates_per_frame
        
        # Perform multiple updates if refresh rate allows
        for update_idx in range(updates_per_frame):
            # Recalculate distance to target
            dx = target_x - current_x
            dy = target_y - current_y
            distance = np.sqrt(dx**2 + dy**2)
            
            if distance < self.step_close_enough_threshold:  # Close enough
                break
            
            # Move trap incrementally
            actual_step = min(step_size, distance)
            move_x = current_x + (dx / distance) * actual_step
            move_y = current_y + (dy / distance) * actual_step
            
            # Update trap position
            self.trap_positions = [(move_x, move_y, self.trap_z_offset, self.trap_intensity)]
            ctx.set_slm_points(self.trap_positions)
            
            # Update current position for next iteration
            current_x, current_y = move_x, move_y
        
        # Log progress occasionally
        if self.frames_in_state % self.log_progress_interval == 0:
            ctx.log(f"Moving trap: ({current_x:.1f}, {current_y:.1f}) - {distance:.1f} px remaining ({updates_per_frame} updates/frame)", "DEBUG")
        
        return True
    
    def _state_waiting(self, ctx: ExperimentContext) -> bool:
        """Wait for delay period between actions"""
        
        # Clear SLM traps at the start of waiting period (first frame only)
        if self.frames_in_state == 0:
            ctx.set_slm_points([])
            self.trap_positions = []
            ctx.log("Released SLM trap - cleared all points", "INFO")
        
        elapsed = time.time() - self.state_start_time
        
        # Log countdown occasionally
        if self.frames_in_state % self.log_status_interval == 0:
            remaining = self.delay_between_actions - elapsed
            ctx.log(f"Waiting between actions... {remaining:.1f}s remaining", "INFO")
        
        # Check if delay period is over
        if elapsed >= self.delay_between_actions:
            self.cycles_completed += 1
            ctx.log(f"Wait complete. Starting next cycle. Cycles completed: {self.cycles_completed}", "INFO")
            
            # Start next cycle
            self._transition_to_state(ExperimentState.SELECTING_PARTICLE, ctx)
        
        return True
    
    def _transition_to_state(self, new_state: ExperimentState, ctx: ExperimentContext) -> None:
        """Transition to a new state"""
        ctx.log(f"State transition: {self.state.name} -> {new_state.name}", "DEBUG")
        self.state = new_state
        self.state_start_time = time.time()
        self.frames_in_state = 0
    
    def _setup_csv_logging(self, ctx: ExperimentContext) -> None:
        """Setup CSV file for logging experiment actions."""
        try:
            # Create logs directory if it doesn't exist
            logs_dir = Path(__file__).resolve().parents[2] / "logs" / "experiment_logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            
            # Create timestamped CSV file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.csv_file = logs_dir / f"random_displacement_v2_{timestamp}.csv"
            
            # Open file and create CSV writer
            self.csv_file_handle = open(self.csv_file, 'w', newline='')
            fieldnames = [
                'action_number',
                'start_frame',
                'end_frame',
                'initial_x',
                'initial_y',
                'target_x',
                'target_y',
                'displacement_distance',
                'action_duration_seconds',
                'actual_duration_seconds',
                'timestamp'
            ]
            self.csv_writer = csv.DictWriter(self.csv_file_handle, fieldnames=fieldnames)
            self.csv_writer.writeheader()
            self.csv_file_handle.flush()
            
            ctx.log(f"CSV logging initialized: {self.csv_file}", "INFO")
        except Exception as e:
            ctx.log(f"Failed to setup CSV logging: {e}", "ERROR")
            self.csv_writer = None
    
    def _log_action_to_csv(self, ctx: ExperimentContext) -> None:
        """Log completed action to CSV file."""
        if self.csv_writer is None or self.csv_file_handle is None:
            return
        
        try:
            # Calculate metrics
            end_frame = ctx.frame_number
            actual_duration = time.time() - self.action_start_time
            displacement_distance = np.sqrt(
                (self.action_target_x - self.action_initial_x)**2 + 
                (self.action_target_y - self.action_initial_y)**2
            )
            
            # Write row to CSV
            row = {
                'action_number': self.cycles_completed + 1,
                'start_frame': self.action_start_frame,
                'end_frame': end_frame,
                'initial_x': f"{self.action_initial_x:.2f}",
                'initial_y': f"{self.action_initial_y:.2f}",
                'target_x': f"{self.action_target_x:.2f}",
                'target_y': f"{self.action_target_y:.2f}",
                'displacement_distance': f"{displacement_distance:.2f}",
                'action_duration_seconds': f"{self.current_movement_duration:.3f}",
                'actual_duration_seconds': f"{actual_duration:.3f}",
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            }
            self.csv_writer.writerow(row)
            self.csv_file_handle.flush()
            
            ctx.log(f"Action logged to CSV: Action {self.cycles_completed + 1}", "DEBUG")
        except Exception as e:
            ctx.log(f"Failed to log action to CSV: {e}", "ERROR")
    
    def _close_csv_logging(self, ctx: ExperimentContext) -> None:
        """Close CSV logging file."""
        if self.csv_file_handle is not None:
            try:
                self.csv_file_handle.close()
                ctx.log(f"CSV log file closed: {self.csv_file}", "INFO")
            except Exception as e:
                ctx.log(f"Failed to close CSV file: {e}", "ERROR")
            finally:
                self.csv_file_handle = None
                self.csv_writer = None
    
    def teardown(self, ctx: ExperimentContext) -> None:
        """Clean up when experiment ends"""
        ctx.log(f"=== {self.name} Stopping ===", "INFO")
        ctx.log(f"Total cycles completed: {self.cycles_completed}", "INFO")
        ctx.log(f"Total frames processed: {self.frame_count}", "INFO")
        
        # Close CSV file
        self._close_csv_logging(ctx)
        
        # Clear SLM traps
        ctx.set_slm_points([])
        ctx.log("SLM traps cleared", "INFO")
    
    def on_pause(self, ctx: ExperimentContext) -> None:
        """Called when paused"""
        ctx.log("Experiment paused", "INFO")
    
    def on_resume(self, ctx: ExperimentContext) -> None:
        """Called when resumed"""
        ctx.log("Experiment resumed", "INFO")
        # Reset state timer to avoid time jump issues
        self.state_start_time = time.time()
    
    def on_error(self, ctx: ExperimentContext, error: Exception) -> bool:
        """Handle errors gracefully"""
        ctx.log(f"Error occurred: {error}", "ERROR")
        
        # Try to recover by going back to particle selection
        if self.error_count < 5:
            ctx.log("Attempting to recover...", "WARNING")
            self._transition_to_state(ExperimentState.SELECTING_PARTICLE, ctx)
            return True  # Continue
        else:
            ctx.log("Too many errors, stopping experiment", "ERROR")
            return False  # Stop


# Export the script class (this is what the manager will find)
__all__ = ['RandomParticleDisplacementV2']
