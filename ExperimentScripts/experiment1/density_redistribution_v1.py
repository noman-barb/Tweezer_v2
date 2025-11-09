"""
Experiment: Particle Density Redistribution

This experiment analyzes the spatial distribution of particles in the system,
identifies high-density and low-density regions, and uses SLM traps to move
particles from crowded areas to sparse areas to achieve a more uniform distribution.
"""

import sys
from pathlib import Path
import csv
from datetime import datetime
import numpy as np
import time
from enum import Enum
from typing import Optional, Tuple, List, Dict
from scipy.spatial import KDTree, Voronoi
from scipy.ndimage import gaussian_filter

# Add parent directory to path for imports
_script_dir = Path(__file__).resolve().parent
_experiments_root = _script_dir.parent
if str(_experiments_root) not in sys.path:
    sys.path.insert(0, str(_experiments_root))

from base_script import ExperimentScript, ExperimentContext, ParamSpec


class ExperimentState(Enum):
    """States for the experiment state machine"""
    ANALYZING_DENSITY = 1
    SELECTING_SOURCE = 2
    SELECTING_TARGET = 3
    MOVING_PARTICLE = 4
    WAITING = 5


class DensityRedistribution(ExperimentScript):
    """
    Redistributes particles to achieve uniform density distribution.
    
    The experiment:
    1. Analyzes current particle spatial distribution
    2. Identifies high-density (crowded) and low-density (sparse) regions
    3. Selects particles from high-density regions
    4. Moves them to low-density regions using SLM traps
    5. Repeats to progressively equalize density
    """
    
    def __init__(self):
        super().__init__()
        self.name = "Particle Density Redistribution"
        self.description = (
            "Analyzes particle distribution and moves particles from high-density "
            "to low-density regions to achieve uniform spatial distribution."
        )
        
        # === Density Analysis Parameters ===
        self.register_param(ParamSpec(
            name='grid_size',
            label='Grid Size',
            param_type=int,
            default=8,
            min_value=2,
            max_value=20,
            step=1,
            unit='cells',
            category='Density Analysis',
            description='Number of grid cells per dimension for density mapping',
            format_str='%d'
        ))
        
        self.register_param(ParamSpec(
            name='density_smoothing_sigma',
            label='Smoothing Sigma',
            param_type=float,
            default=1.5,
            min_value=0.0,
            max_value=5.0,
            step=0.1,
            unit='cells',
            category='Density Analysis',
            description='Gaussian smoothing for density map',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='high_density_threshold',
            label='High Density %',
            param_type=float,
            default=75.0,
            min_value=50.0,
            max_value=95.0,
            step=5.0,
            unit='%',
            category='Density Analysis',
            description='Percentile threshold for high-density regions',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='low_density_threshold',
            label='Low Density %',
            param_type=float,
            default=25.0,
            min_value=5.0,
            max_value=50.0,
            step=5.0,
            unit='%',
            category='Density Analysis',
            description='Percentile threshold for low-density regions',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='analysis_interval',
            label='Analysis Interval',
            param_type=int,
            default=5,
            min_value=1,
            max_value=50,
            step=1,
            unit='cycles',
            category='Density Analysis',
            description='How often to re-analyze density distribution',
            format_str='%d'
        ))
        
        # === Movement Parameters ===
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
            name='max_movement_distance',
            label='Max Distance',
            param_type=float,
            default=300.0,
            min_value=50.0,
            max_value=1000.0,
            step=10.0,
            unit='px',
            category='Movement',
            description='Maximum distance to move a particle',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='max_movement_speed',
            label='Max Speed',
            param_type=float,
            default=100.0,
            min_value=10.0,
            max_value=500.0,
            step=5.0,
            unit='px/s',
            category='Movement',
            description='Maximum particle movement speed (limits how fast particles can be moved)',
            format_str='%.1f'
        ))
        
        self.register_param(ParamSpec(
            name='delay_between_moves',
            label='Delay',
            param_type=float,
            default=2.0,
            min_value=0.0,
            max_value=60.0,
            step=0.5,
            unit='s',
            category='Movement',
            description='Wait time between consecutive movements',
            format_str='%.1f'
        ))
        
        # === Spatial Constraints ===
        self.register_param(ParamSpec(
            name='min_particle_separation',
            label='Min Separation',
            param_type=float,
            default=40.0,
            min_value=10.0,
            max_value=200.0,
            step=5.0,
            unit='px',
            category='Constraints',
            description='Minimum distance between particles at target',
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
        
        self.register_param(ParamSpec(
            name='target_zone_radius',
            label='Target Zone',
            param_type=float,
            default=100.0,
            min_value=20.0,
            max_value=500.0,
            step=10.0,
            unit='px',
            category='Constraints',
            description='Radius around low-density center to place particles',
            format_str='%.1f'
        ))
        
        # === SLM Parameters ===
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
        
        # === Performance Parameters ===
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
        
        # === Logging Parameters ===
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
        
        # === Experiment Control ===
        self.register_param(ParamSpec(
            name='max_cycles',
            label='Max Cycles',
            param_type=int,
            default=100,
            min_value=1,
            max_value=1000,
            step=10,
            unit='cycles',
            category='Control',
            description='Maximum redistribution cycles (0 = infinite)',
            format_str='%d'
        ))
        
        # Image dimensions
        self.image_width = 0.0
        self.image_height = 0.0
        
        # Density analysis data
        self.density_map: Optional[np.ndarray] = None
        self.high_density_regions: List[Tuple[float, float, float]] = []  # (density, x, y)
        self.low_density_regions: List[Tuple[float, float]] = []
        self.last_analysis_cycle = -1
        
        # Current movement tracking
        self.current_movement_duration = 0.0
        self.current_movement_speed = 0.0
        self.selected_source_particle: Optional[Tuple[float, float, float]] = None
        self.target_position: Optional[Tuple[float, float]] = None
        self.trap_positions: List = []
        
        # CSV logging
        self.csv_file: Optional[Path] = None
        self.csv_writer: Optional[csv.DictWriter] = None
        self.csv_file_handle = None
        
        # Action tracking for CSV
        self.action_start_frame = 0
        self.action_initial_x = 0.0
        self.action_initial_y = 0.0
        self.action_target_x = 0.0
        self.action_target_y = 0.0
        self.action_start_time = 0.0
        self.action_source_density = 0.0
        self.action_target_density = 0.0
        
        # State machine
        self.state = ExperimentState.ANALYZING_DENSITY
        self.state_start_time = 0.0
        self.frames_in_state = 0
        self.cycles_completed = 0
    
    def setup(self, ctx: ExperimentContext) -> bool:
        """Initialize the experiment"""
        ctx.log(f"=== {self.name} Starting ===", "INFO")
        ctx.log(f"Parameters:", "INFO")
        
        # Log all registered parameters
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
        
        self.state = ExperimentState.ANALYZING_DENSITY
        self.state_start_time = time.time()
        self.cycles_completed = 0
        self.last_analysis_cycle = -1
        
        # Setup CSV logging
        self._setup_csv_logging(ctx)
        
        return True
    
    def on_frame(self, ctx: ExperimentContext) -> bool:
        """Process each frame based on current state"""
        self.frames_in_state += 1
        
        # Check if max cycles reached
        if self.max_cycles > 0 and self.cycles_completed >= self.max_cycles:
            ctx.log(f"Maximum cycles ({self.max_cycles}) completed. Stopping experiment.", "INFO")
            return False
        
        # State machine
        if self.state == ExperimentState.ANALYZING_DENSITY:
            return self._state_analyzing_density(ctx)
        
        elif self.state == ExperimentState.SELECTING_SOURCE:
            return self._state_selecting_source(ctx)
        
        elif self.state == ExperimentState.SELECTING_TARGET:
            return self._state_selecting_target(ctx)
        
        elif self.state == ExperimentState.MOVING_PARTICLE:
            return self._state_moving_particle(ctx)
        
        elif self.state == ExperimentState.WAITING:
            return self._state_waiting(ctx)
        
        return True
    
    def _state_analyzing_density(self, ctx: ExperimentContext) -> bool:
        """Analyze particle density distribution"""
        
        # Check if we need to analyze (first time or periodic re-analysis)
        if self.last_analysis_cycle >= 0 and (self.cycles_completed - self.last_analysis_cycle) < self.analysis_interval:
            # Use existing density analysis
            self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
            return True
        
        # Check if we have particles
        if not ctx.tracked_positions or len(ctx.tracked_positions) < 3:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log(f"Waiting for sufficient particles (need 3+, have {len(ctx.tracked_positions) if ctx.tracked_positions else 0})", "INFO")
            return True
        
        # Update image dimensions if available
        if ctx.current_image is not None:
            self.image_height, self.image_width = ctx.current_image.shape[:2]
        
        ctx.log(f"Analyzing density distribution for {len(ctx.tracked_positions)} particles...", "INFO")
        
        # Extract particle positions
        positions = np.array([(px, py) for px, py, _ in ctx.tracked_positions])
        
        # Create density map using grid-based counting
        grid_size = self.grid_size
        x_edges = np.linspace(0, self.image_width, grid_size + 1)
        y_edges = np.linspace(0, self.image_height, grid_size + 1)
        
        # Count particles in each grid cell
        density_map = np.zeros((grid_size, grid_size))
        for px, py in positions:
            x_idx = np.searchsorted(x_edges[:-1], px, side='right') - 1
            y_idx = np.searchsorted(y_edges[:-1], py, side='right') - 1
            x_idx = max(0, min(grid_size - 1, x_idx))
            y_idx = max(0, min(grid_size - 1, y_idx))
            density_map[y_idx, x_idx] += 1
        
        # Apply Gaussian smoothing
        if self.density_smoothing_sigma > 0:
            density_map = gaussian_filter(density_map, sigma=self.density_smoothing_sigma)
        
        self.density_map = density_map
        
        # Identify high and low density regions
        density_flat = density_map.flatten()
        high_threshold = np.percentile(density_flat, self.high_density_threshold)
        low_threshold = np.percentile(density_flat, self.low_density_threshold)
        
        ctx.log(f"Density range: {density_flat.min():.2f} - {density_flat.max():.2f}", "INFO")
        ctx.log(f"High density threshold (>{self.high_density_threshold}%): {high_threshold:.2f}", "INFO")
        ctx.log(f"Low density threshold (<{self.low_density_threshold}%): {low_threshold:.2f}", "INFO")
        
        # Find grid cell centers for high and low density regions
        self.high_density_regions = []
        self.low_density_regions = []
        
        for i in range(grid_size):
            for j in range(grid_size):
                cell_density = density_map[i, j]
                center_x = (x_edges[j] + x_edges[j + 1]) / 2
                center_y = (y_edges[i] + y_edges[i + 1]) / 2
                
                if cell_density >= high_threshold:
                    # Store density value along with coordinates for sorting
                    self.high_density_regions.append((cell_density, center_x, center_y))
                elif cell_density <= low_threshold:
                    self.low_density_regions.append((center_x, center_y))
        
        # Sort high-density regions by density (highest first)
        self.high_density_regions.sort(key=lambda x: x[0], reverse=True)
        
        ctx.log(f"Found {len(self.high_density_regions)} high-density regions", "INFO")
        ctx.log(f"Found {len(self.low_density_regions)} low-density regions", "INFO")
        if self.high_density_regions:
            ctx.log(f"Highest density region: {self.high_density_regions[0][0]:.2f} at ({self.high_density_regions[0][1]:.1f}, {self.high_density_regions[0][2]:.1f})", "INFO")
        
        self.last_analysis_cycle = self.cycles_completed
        
        # Move to source selection
        self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
        
        return True
    
    def _state_selecting_source(self, ctx: ExperimentContext) -> bool:
        """Select a particle from a high-density region to move"""
        
        # Check if we have particles and density regions
        if not ctx.tracked_positions:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log("Waiting for particles to be tracked...", "INFO")
            return True
        
        if not self.high_density_regions:
            ctx.log("No high-density regions found. Re-analyzing...", "INFO")
            self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            return True
        
        # Select the highest density region (first element after sorting in descending order)
        region_density, region_x, region_y = self.high_density_regions[0]
        ctx.log(f"Targeting highest density region: {region_density:.2f} at ({region_x:.1f}, {region_y:.1f})", "INFO")
        
        # Find particles near this region center
        candidates = []
        for idx, (px, py, mass) in enumerate(ctx.tracked_positions):
            distance = np.sqrt((px - region_x)**2 + (py - region_y)**2)
            
            # Check edge constraints
            if (px < self.edge_margin or px > self.image_width - self.edge_margin or
                py < self.edge_margin or py > self.image_height - self.edge_margin):
                continue
            
            # Prefer particles closer to the high-density region center
            candidates.append((distance, idx, px, py, mass))
        
        if not candidates:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log("No valid particles in high-density regions. Waiting...", "INFO")
            return True
        
        # Sort by distance and select closest particle
        candidates.sort(key=lambda x: x[0])
        _, _, px, py, mass = candidates[0]
        
        self.selected_source_particle = (px, py, mass)
        ctx.log(f"Selected source particle at ({px:.1f}, {py:.1f}) from high-density region", "INFO")
        
        # Get density at source location
        self.action_source_density = self._get_density_at_position(px, py)
        
        # Move to target selection
        self._transition_to_state(ExperimentState.SELECTING_TARGET, ctx)
        
        return True
    
    def _state_selecting_target(self, ctx: ExperimentContext) -> bool:
        """Select a target position in a low-density region"""
        
        if not self.selected_source_particle:
            ctx.log("ERROR: No source particle selected", "ERROR")
            self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
            return True
        
        if not self.low_density_regions:
            ctx.log("No low-density regions found. Re-analyzing...", "INFO")
            self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            return True
        
        px, py, mass = self.selected_source_particle
        
        # Find suitable low-density regions (not too far from source)
        valid_targets = []
        for region_x, region_y in self.low_density_regions:
            distance = np.sqrt((region_x - px)**2 + (region_y - py)**2)
            if distance <= self.max_movement_distance:
                valid_targets.append((distance, region_x, region_y))
        
        if not valid_targets:
            ctx.log(f"No low-density regions within {self.max_movement_distance} px. Selecting new source...", "INFO")
            self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
            return True
        
        # Select target region (prefer closer regions, but with some randomness)
        valid_targets.sort(key=lambda x: x[0])
        # Pick from top 50% closest regions randomly
        top_half = max(1, len(valid_targets) // 2)
        _, region_x, region_y = valid_targets[np.random.randint(0, top_half)]
        
        # Generate random target position within target zone radius around region center
        angle = np.random.uniform(0, 2 * np.pi)
        radius = np.random.uniform(0, self.target_zone_radius)
        tx = region_x + radius * np.cos(angle)
        ty = region_y + radius * np.sin(angle)
        
        # Ensure target is within bounds
        tx = max(self.edge_margin, min(self.image_width - self.edge_margin, tx))
        ty = max(self.edge_margin, min(self.image_height - self.edge_margin, ty))
        
        # Check minimum separation from other particles
        if ctx.tracked_positions:
            for other_px, other_py, _ in ctx.tracked_positions:
                if (abs(other_px - px) < 1 and abs(other_py - py) < 1):  # Skip the source particle itself
                    continue
                dist = np.sqrt((tx - other_px)**2 + (ty - other_py)**2)
                if dist < self.min_particle_separation:
                    # Too close to another particle, try again
                    ctx.log(f"Target too close to existing particle. Retrying...", "DEBUG")
                    return True
        
        self.target_position = (tx, ty)
        distance = np.sqrt((tx - px)**2 + (ty - py)**2)
        
        ctx.log(f"Target position: ({tx:.1f}, {ty:.1f}) - distance: {distance:.1f} px", "INFO")
        
        # Get density at target location
        self.action_target_density = self._get_density_at_position(tx, ty)
        ctx.log(f"Moving from density {self.action_source_density:.2f} to {self.action_target_density:.2f}", "INFO")
        
        # Calculate movement duration and speed with speed limit
        # Start with randomized duration
        desired_duration = np.random.uniform(self.movement_duration_min, self.movement_duration_max)
        
        # Calculate speed required for this duration
        expected_frames = desired_duration * self.assumed_fps
        required_speed_px_per_frame = distance / expected_frames if expected_frames > 0 else distance
        
        # Convert max speed from px/s to px/frame
        max_speed_px_per_frame = self.max_movement_speed / self.assumed_fps if self.assumed_fps > 0 else self.max_movement_speed / 30.0
        
        # If required speed exceeds max speed, extend the duration
        if required_speed_px_per_frame > max_speed_px_per_frame:
            self.current_movement_speed = max_speed_px_per_frame
            # Recalculate duration based on speed limit
            self.current_movement_duration = distance / self.max_movement_speed if self.max_movement_speed > 0 else desired_duration
            ctx.log(f"Speed limited to {self.max_movement_speed:.1f} px/s (extended duration to {self.current_movement_duration:.2f}s)", "DEBUG")
        else:
            self.current_movement_speed = required_speed_px_per_frame
            self.current_movement_duration = desired_duration
        
        # Calculate actual speed in px/s for logging
        actual_speed_px_per_s = self.current_movement_speed * self.assumed_fps
        ctx.log(f"Movement: {self.current_movement_duration:.2f}s at {actual_speed_px_per_s:.1f} px/s ({self.current_movement_speed:.2f} px/frame)", "INFO")
        
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
        
        if not self.selected_source_particle or not self.target_position:
            ctx.log("ERROR: Missing particle or target in MOVING state", "ERROR")
            self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            return True
        
        # Get current trap position
        if not self.trap_positions:
            ctx.log("ERROR: No trap positions in MOVING state", "ERROR")
            self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
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
            
            if distance < self.step_close_enough_threshold:
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
        """Wait for delay period between movements"""
        
        # Clear SLM traps at the start of waiting period (first frame only)
        if self.frames_in_state == 0:
            ctx.set_slm_points([])
            self.trap_positions = []
            ctx.log("Released SLM trap - cleared all points", "INFO")
        
        elapsed = time.time() - self.state_start_time
        
        # Log countdown occasionally
        if self.frames_in_state % self.log_status_interval == 0:
            remaining = self.delay_between_moves - elapsed
            ctx.log(f"Waiting between movements... {remaining:.1f}s remaining", "INFO")
        
        # Check if delay period is over
        if elapsed >= self.delay_between_moves:
            self.cycles_completed += 1
            ctx.log(f"Wait complete. Starting next cycle. Cycles completed: {self.cycles_completed}", "INFO")
            
            # Check if we need to re-analyze density
            if (self.cycles_completed - self.last_analysis_cycle) >= self.analysis_interval:
                self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            else:
                self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
        
        return True
    
    def _transition_to_state(self, new_state: ExperimentState, ctx: ExperimentContext) -> None:
        """Transition to a new state"""
        ctx.log(f"State transition: {self.state.name} -> {new_state.name}", "DEBUG")
        self.state = new_state
        self.state_start_time = time.time()
        self.frames_in_state = 0
    
    def _get_density_at_position(self, x: float, y: float) -> float:
        """Get density value at a given position from the density map"""
        if self.density_map is None:
            return 0.0
        
        # Convert position to grid coordinates
        grid_size = self.density_map.shape[0]
        grid_x = int(x / self.image_width * grid_size)
        grid_y = int(y / self.image_height * grid_size)
        
        # Clamp to valid range
        grid_x = max(0, min(grid_size - 1, grid_x))
        grid_y = max(0, min(grid_size - 1, grid_y))
        
        return self.density_map[grid_y, grid_x]
    
    def _setup_csv_logging(self, ctx: ExperimentContext) -> None:
        """Setup CSV file for logging experiment actions."""
        try:
            # Create logs directory if it doesn't exist
            logs_dir = Path(__file__).resolve().parents[2] / "logs" / "experiment_logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            
            # Create CSV filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.csv_file = logs_dir / f"density_redistribution_{timestamp}.csv"
            
            # Open CSV file and create writer
            self.csv_file_handle = open(self.csv_file, 'w', newline='')
            fieldnames = [
                'cycle',
                'start_frame',
                'end_frame',
                'start_time',
                'duration_seconds',
                'initial_x',
                'initial_y',
                'target_x',
                'target_y',
                'displacement_distance',
                'source_density',
                'target_density',
                'density_improvement',
                'movement_duration_setting',
                'actual_frames',
                'average_speed_px_per_frame'
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
            end_time = time.time()
            duration = end_time - self.action_start_time
            end_frame = ctx.frame_number
            actual_frames = end_frame - self.action_start_frame
            
            displacement = np.sqrt(
                (self.action_target_x - self.action_initial_x)**2 +
                (self.action_target_y - self.action_initial_y)**2
            )
            
            avg_speed = displacement / actual_frames if actual_frames > 0 else 0.0
            density_improvement = self.action_target_density - self.action_source_density
            
            row = {
                'cycle': self.cycles_completed,
                'start_frame': self.action_start_frame,
                'end_frame': end_frame,
                'start_time': datetime.fromtimestamp(self.action_start_time).isoformat(),
                'duration_seconds': f"{duration:.3f}",
                'initial_x': f"{self.action_initial_x:.2f}",
                'initial_y': f"{self.action_initial_y:.2f}",
                'target_x': f"{self.action_target_x:.2f}",
                'target_y': f"{self.action_target_y:.2f}",
                'displacement_distance': f"{displacement:.2f}",
                'source_density': f"{self.action_source_density:.3f}",
                'target_density': f"{self.action_target_density:.3f}",
                'density_improvement': f"{density_improvement:.3f}",
                'movement_duration_setting': f"{self.current_movement_duration:.2f}",
                'actual_frames': actual_frames,
                'average_speed_px_per_frame': f"{avg_speed:.3f}"
            }
            
            self.csv_writer.writerow(row)
            self.csv_file_handle.flush()
            
        except Exception as e:
            ctx.log(f"Failed to log action to CSV: {e}", "ERROR")
    
    def _close_csv_logging(self, ctx: ExperimentContext) -> None:
        """Close CSV logging file."""
        if self.csv_file_handle is not None:
            try:
                self.csv_file_handle.close()
                ctx.log(f"CSV logging closed: {self.csv_file}", "INFO")
            except Exception as e:
                ctx.log(f"Error closing CSV file: {e}", "ERROR")
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
        
        # Try to recover by re-analyzing density
        if self.error_count < 5:
            ctx.log("Attempting recovery by re-analyzing density...", "WARNING")
            self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            return True
        else:
            ctx.log("Too many errors. Stopping experiment.", "ERROR")
            return False


# Export the script class
__all__ = ['DensityRedistribution']
