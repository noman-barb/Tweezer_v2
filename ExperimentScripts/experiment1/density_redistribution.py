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
import math
import heapq
from dataclasses import dataclass
from scipy.spatial import KDTree, Voronoi
from scipy.ndimage import gaussian_filter
from scipy.optimize import linear_sum_assignment

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


@dataclass
class PlannedMove:
    """Represents a planner-scheduled redistribution action."""
    source_cell: Tuple[int, int]
    target_cell: Tuple[int, int]
    source_center: Tuple[float, float]
    target_center: Tuple[float, float]
    distance: float
    expected_gain: float
    source_density: float
    target_density: float
    priority: float
    plan_cycle: int


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
        
        self.register_param(ParamSpec(
            name='min_particle_mass',
            label='Min Particle Mass',
            param_type=float,
            default=0.0,
            min_value=0.0,
            max_value=100000.0,
            step=10.0,
            unit='',
            category='Constraints',
            description='Minimum particle mass to consider for tracking and manipulation',
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
            default=1.0,
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
        self.raw_density_map: Optional[np.ndarray] = None
        self.high_density_regions: List[Tuple[float, float, float]] = []  # (density, x, y)
        self.low_density_regions: List[Tuple[float, float]] = []
        self.last_analysis_cycle = -1
        self.grid_size = 0
        self.grid_x_edges: Optional[np.ndarray] = None
        self.grid_y_edges: Optional[np.ndarray] = None
        self.cell_width = 0.0
        self.cell_height = 0.0
        self.cell_particle_indices: List[List[List[int]]] = []
        self.plan_surplus_threshold = 0.6
        self.plan_max_moves = 200
        self.plan_density_weight = 0.5
        self.plan_target_sampling_attempts = 25
        self.move_plan: List[Tuple[float, int, PlannedMove]] = []
        self.plan_sequence = 0
        self.current_plan_step: Optional[PlannedMove] = None
        self.plan_generation_cycle = -1
        self.selected_particle_index: Optional[int] = None
        
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
        """Analyze particle density distribution and build a long-term move plan."""

        # Skip re-analysis unless the plan is exhausted or interval elapsed
        if self.last_analysis_cycle >= 0 and (self.cycles_completed - self.last_analysis_cycle) < self.analysis_interval:
            self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
            return True

        # Require a minimum number of particles to build a meaningful plan
        tracked_count = len(ctx.tracked_positions) if ctx.tracked_positions else 0
        if tracked_count < 3:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log(f"Waiting for sufficient particles (need 3+, have {tracked_count})", "INFO")
            return True

        # Refresh image metadata if we have a frame
        if ctx.current_image is not None:
            self.image_height, self.image_width = ctx.current_image.shape[:2]

        ctx.log(f"Analyzing density distribution for {tracked_count} particles...", "INFO")

        # Configure grid geometry from parameters
        grid_size = int(self.get_param_value('grid_size'))
        self.grid_size = max(2, grid_size)
        self.grid_x_edges = np.linspace(0, self.image_width, self.grid_size + 1)
        self.grid_y_edges = np.linspace(0, self.image_height, self.grid_size + 1)
        self.cell_width = self.image_width / self.grid_size if self.grid_size > 0 else self.image_width
        self.cell_height = self.image_height / self.grid_size if self.grid_size > 0 else self.image_height

        # Build raw density map and particle index map per cell
        raw_density_map = np.zeros((self.grid_size, self.grid_size), dtype=int)
        cell_particle_indices: List[List[List[int]]] = [[[] for _ in range(self.grid_size)] for _ in range(self.grid_size)]

        min_mass = self.get_param_value('min_particle_mass')
        filtered_count = 0
        for idx, (px, py, mass) in enumerate(ctx.tracked_positions):
            # Filter by minimum mass
            if mass < min_mass:
                filtered_count += 1
                continue
                
            x_idx = min(max(int(np.searchsorted(self.grid_x_edges, px, side='right') - 1), 0), self.grid_size - 1)
            y_idx = min(max(int(np.searchsorted(self.grid_y_edges, py, side='right') - 1), 0), self.grid_size - 1)
            raw_density_map[y_idx, x_idx] += 1
            cell_particle_indices[y_idx][x_idx].append(idx)
        
        if filtered_count > 0:
            ctx.log(f"Filtered out {filtered_count} particles below mass threshold {min_mass:.1f}", "DEBUG")

        # Smooth density for thresholding while preserving raw counts for planning
        density_map = raw_density_map.astype(float)
        if self.density_smoothing_sigma > 0:
            density_map = gaussian_filter(density_map, sigma=self.density_smoothing_sigma)

        self.density_map = density_map
        self.raw_density_map = raw_density_map
        self.cell_particle_indices = cell_particle_indices

        density_flat = density_map.flatten()
        high_threshold = float(np.percentile(density_flat, self.high_density_threshold))
        low_threshold = float(np.percentile(density_flat, self.low_density_threshold))

        ctx.log(f"Density range: {density_flat.min():.2f} - {density_flat.max():.2f}", "INFO")
        ctx.log(f"High density threshold (>{self.high_density_threshold}%): {high_threshold:.2f}", "INFO")
        ctx.log(f"Low density threshold (<{self.low_density_threshold}%): {low_threshold:.2f}", "INFO")

        # Track grid regions for reactive fallback logic
        self.high_density_regions = []
        self.low_density_regions = []
        for row in range(self.grid_size):
            for col in range(self.grid_size):
                center_x = (self.grid_x_edges[col] + self.grid_x_edges[col + 1]) / 2
                center_y = (self.grid_y_edges[row] + self.grid_y_edges[row + 1]) / 2
                cell_density = density_map[row, col]
                if cell_density >= high_threshold:
                    self.high_density_regions.append((cell_density, center_x, center_y))
                elif cell_density <= low_threshold:
                    self.low_density_regions.append((center_x, center_y))

        self.high_density_regions.sort(key=lambda entry: entry[0], reverse=True)
        ctx.log(f"Found {len(self.high_density_regions)} high-density regions", "INFO")
        ctx.log(f"Found {len(self.low_density_regions)} low-density regions", "INFO")
        if self.high_density_regions:
            ctx.log(
                f"Highest density region: {self.high_density_regions[0][0]:.2f} at ({self.high_density_regions[0][1]:.1f}, {self.high_density_regions[0][2]:.1f})",
                "INFO",
            )

        # Generate a prioritized redistribution plan that minimizes travel while maximizing density improvement
        self.plan_sequence = 0
        self.move_plan = self._generate_move_plan(raw_density_map, density_map, ctx)
        self.current_plan_step = None
        self.plan_generation_cycle = self.cycles_completed

        if self.move_plan:
            top_priority = self.move_plan[0][0]
            ctx.log(
                f"Planner scheduled {len(self.move_plan)} candidate moves (best priority {top_priority:.2f}).",
                "INFO",
            )
        else:
            ctx.log("Planner found no advantageous moves this cycle; falling back to reactive control.", "INFO")

        self.last_analysis_cycle = self.cycles_completed
        self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
        return True
    
    def _state_selecting_source(self, ctx: ExperimentContext) -> bool:
        """Select a particle from a high-density region to move"""
        
        # Check if we have particles and density regions
        if not ctx.tracked_positions:
            if self.frames_in_state % self.log_status_interval == 0:
                ctx.log("Waiting for particles to be tracked...", "INFO")
            return True

        # Attempt to follow the queued long-term plan first
        if self.current_plan_step is None and self.move_plan:
            self.current_plan_step = self._pop_next_plan_step(ctx)

        while self.current_plan_step is not None:
            candidate = self._select_particle_for_plan(ctx, self.current_plan_step)
            if candidate is None:
                ctx.log("Planned move invalidated (no suitable particle near planned source). Retrying...", "WARNING")
                self.current_plan_step = self._pop_next_plan_step(ctx)
                continue

            idx, px, py, mass = candidate
            self.selected_particle_index = idx
            self.selected_source_particle = (px, py, mass)
            self.action_source_density = self._get_density_at_position(px, py)
            ctx.log(
                "Executing planned move from cell "
                f"{self.current_plan_step.source_cell} -> {self.current_plan_step.target_cell} "
                f"(priority {self.current_plan_step.priority:.2f})",
                "INFO",
            )
            self._transition_to_state(ExperimentState.SELECTING_TARGET, ctx)
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
        min_mass = self.get_param_value('min_particle_mass')
        for idx, (px, py, mass) in enumerate(ctx.tracked_positions):
            # Filter by minimum mass
            if mass < min_mass:
                continue
                
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
        self.selected_particle_index = None
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
        
        px, py, mass = self.selected_source_particle
        source_position = (px, py)
        planned_move_used = False
        target_candidate: Optional[Tuple[float, float]] = None

        # Prefer the planner's recommended target when available
        if self.current_plan_step is not None:
            target_candidate = self._sample_target_position(
                ctx,
                self.current_plan_step.target_center,
                source_position,
                self.selected_particle_index,
            )
            if target_candidate is not None:
                planned_distance = math.hypot(target_candidate[0] - px, target_candidate[1] - py)
                if planned_distance <= self.max_movement_distance:
                    planned_move_used = True
                else:
                    ctx.log(
                        f"Planned target exceeds max distance ({planned_distance:.1f} px > {self.max_movement_distance:.1f} px).",
                        "WARNING",
                    )
                    target_candidate = None
            else:
                ctx.log("Planner target invalid (violates constraints). Falling back to reactive target.", "WARNING")

            if target_candidate is None:
                # Drop the plan step so we can choose a reactive destination instead
                self.current_plan_step = None

        if target_candidate is None:
            if not self.low_density_regions:
                ctx.log("No low-density regions found. Re-analyzing...", "INFO")
                self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
                return True
            
            valid_regions: List[Tuple[float, float, float]] = []
            for region_x, region_y in self.low_density_regions:
                distance = math.hypot(region_x - px, region_y - py)
                if distance <= self.max_movement_distance:
                    valid_regions.append((distance, region_x, region_y))

            if not valid_regions:
                ctx.log(f"No low-density regions within {self.max_movement_distance} px. Selecting new source...", "INFO")
                self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
                return True

            valid_regions.sort(key=lambda entry: entry[0])
            target_candidate = None
            for _, region_x, region_y in valid_regions:
                sampled = self._sample_target_position(
                    ctx,
                    (region_x, region_y),
                    source_position,
                    self.selected_particle_index,
                )
                if sampled is not None:
                    target_candidate = sampled
                    break

            if target_candidate is None:
                ctx.log("Failed to find a valid target within low-density regions. Re-analyzing...", "WARNING")
                self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
                return True
        
        tx, ty = target_candidate
        distance = math.hypot(tx - px, ty - py)

        if planned_move_used and self.current_plan_step is not None:
            ctx.log(
                f"Planner target position: ({tx:.1f}, {ty:.1f}) - distance {distance:.1f} px",
                "INFO",
            )
        else:
            ctx.log(f"Target position: ({tx:.1f}, {ty:.1f}) - distance: {distance:.1f} px", "INFO")

        self.target_position = (tx, ty)
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

            if self.current_plan_step is not None:
                completed_move = self.current_plan_step
                ctx.log(
                    (
                        f"Planner move {completed_move.source_cell} -> {completed_move.target_cell} "
                        f"completed (gain {completed_move.expected_gain:.2f})"
                    ),
                    "INFO",
                )
                self.current_plan_step = None
            self.selected_particle_index = None
            
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
            self.selected_particle_index = None
        
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
            if not self.move_plan:
                ctx.log("Planner queue empty. Triggering fresh density analysis.", "INFO")
                self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            elif (self.cycles_completed - self.last_analysis_cycle) >= self.analysis_interval:
                self._transition_to_state(ExperimentState.ANALYZING_DENSITY, ctx)
            else:
                self._transition_to_state(ExperimentState.SELECTING_SOURCE, ctx)
        
        return True

    def _generate_move_plan(
        self,
        raw_density_map: np.ndarray,
        smoothed_density_map: np.ndarray,
        ctx: ExperimentContext,
    ) -> List[Tuple[float, int, PlannedMove]]:
        """Create a prioritized list of redistribution moves using a cost-minimizing assignment."""

        plan_heap: List[Tuple[float, int, PlannedMove]] = []
        if self.grid_size <= 0 or self.grid_x_edges is None or self.grid_y_edges is None:
            return plan_heap

        total_particles = int(raw_density_map.sum())
        if total_particles == 0:
            return plan_heap

        total_cells = self.grid_size * self.grid_size
        base_target, remainder = divmod(total_particles, total_cells)
        target_counts = np.full_like(raw_density_map, base_target, dtype=int)

        cell_order: List[Tuple[int, int, int]] = []
        for row in range(self.grid_size):
            for col in range(self.grid_size):
                cell_order.append((int(raw_density_map[row, col]), row, col))
        cell_order.sort(key=lambda entry: entry[0])
        for idx in range(remainder):
            _, row, col = cell_order[idx]
            target_counts[row, col] += 1

        surplus_cells: List[Tuple[int, int]] = []
        deficit_cells: List[Tuple[int, int]] = []
        for row in range(self.grid_size):
            for col in range(self.grid_size):
                count = int(raw_density_map[row, col])
                target = int(target_counts[row, col])
                if count > target:
                    surplus_cells.extend([(row, col)] * (count - target))
                elif count < target:
                    deficit_cells.extend([(row, col)] * (target - count))

        if not surplus_cells or not deficit_cells:
            return plan_heap

        surplus_cells.sort(key=lambda rc: smoothed_density_map[rc[0], rc[1]], reverse=True)
        deficit_cells.sort(key=lambda rc: smoothed_density_map[rc[0], rc[1]])

        plan_limit = min(self.plan_max_moves, len(surplus_cells), len(deficit_cells))
        if plan_limit == 0:
            return plan_heap

        surplus_cells = surplus_cells[:plan_limit]
        deficit_cells = deficit_cells[:plan_limit]

        cost_matrix = np.zeros((len(surplus_cells), len(deficit_cells)))
        center_cache: Dict[Tuple[int, int], Tuple[float, float]] = {}

        def cell_center(row: int, col: int) -> Tuple[float, float]:
            key = (row, col)
            if key not in center_cache:
                cx = (self.grid_x_edges[col] + self.grid_x_edges[col + 1]) / 2
                cy = (self.grid_y_edges[row] + self.grid_y_edges[row + 1]) / 2
                center_cache[key] = (cx, cy)
            return center_cache[key]

        for s_idx, (s_row, s_col) in enumerate(surplus_cells):
            sx, sy = cell_center(s_row, s_col)
            for d_idx, (d_row, d_col) in enumerate(deficit_cells):
                tx, ty = cell_center(d_row, d_col)
                distance = math.hypot(sx - tx, sy - ty)
                density_gap = max(0.1, smoothed_density_map[s_row, s_col] - smoothed_density_map[d_row, d_col])
                cost_matrix[s_idx, d_idx] = distance / (1.0 + self.plan_density_weight * density_gap)

        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        skipped_due_to_distance = 0
        for s_idx, d_idx in zip(row_ind, col_ind):
            s_row, s_col = surplus_cells[s_idx]
            d_row, d_col = deficit_cells[d_idx]
            source_center = cell_center(s_row, s_col)
            target_center = cell_center(d_row, d_col)
            distance = math.hypot(source_center[0] - target_center[0], source_center[1] - target_center[1])
            if distance > self.max_movement_distance:
                skipped_due_to_distance += 1
                continue

            density_gain = smoothed_density_map[s_row, s_col] - smoothed_density_map[d_row, d_col]
            priority = float(cost_matrix[s_idx, d_idx])

            move = PlannedMove(
                source_cell=(s_row, s_col),
                target_cell=(d_row, d_col),
                source_center=source_center,
                target_center=target_center,
                distance=float(distance),
                expected_gain=float(density_gain),
                source_density=float(smoothed_density_map[s_row, s_col]),
                target_density=float(smoothed_density_map[d_row, d_col]),
                priority=priority,
                plan_cycle=self.cycles_completed,
            )
            self.plan_sequence += 1
            heapq.heappush(plan_heap, (priority, self.plan_sequence, move))

        if skipped_due_to_distance > 0 and plan_heap:
            ctx.log(f"Planner skipped {skipped_due_to_distance} over-distance assignments", "DEBUG")

        if not plan_heap and skipped_due_to_distance > 0:
            ctx.log("Planner assignments all exceeded distance limits; reactive logic will handle redistribution.", "DEBUG")

        return plan_heap

    def _pop_next_plan_step(self, ctx: ExperimentContext) -> Optional[PlannedMove]:
        """Retrieve the next viable move from the planner queue."""

        while self.move_plan:
            _, _, move = heapq.heappop(self.move_plan)
            if move.distance <= self.max_movement_distance:
                return move
            ctx.log(
                f"Discarded planned move ({move.distance:.1f} px) beyond max distance {self.max_movement_distance:.1f} px",
                "DEBUG",
            )
        return None

    def _select_particle_for_plan(
        self,
        ctx: ExperimentContext,
        move: PlannedMove,
    ) -> Optional[Tuple[int, float, float, float]]:
        """Find a tracked particle near the planner's source location."""

        if not ctx.tracked_positions:
            return None

        min_mass = self.get_param_value('min_particle_mass')
        base_radius = max(self.cell_width, self.cell_height) * 0.75
        best_candidate: Optional[Tuple[float, int, float, float, float]] = None

        def consider(radius: float, current_best: Optional[Tuple[float, int, float, float, float]]) -> Optional[Tuple[float, int, float, float, float]]:
            best = current_best
            for idx, (px, py, mass) in enumerate(ctx.tracked_positions):
                # Filter by minimum mass
                if mass < min_mass:
                    continue
                if not self._position_within_bounds(px, py):
                    continue
                distance = math.hypot(px - move.source_center[0], py - move.source_center[1])
                if distance > radius:
                    continue
                if best is None or distance < best[0]:
                    best = (distance, idx, float(px), float(py), float(mass))
            return best

        best_candidate = consider(base_radius, best_candidate)
        if best_candidate is None:
            best_candidate = consider(base_radius * 1.8, best_candidate)

        if best_candidate is None:
            return None

        _, idx, px, py, mass = best_candidate
        return int(idx), px, py, mass

    def _sample_target_position(
        self,
        ctx: ExperimentContext,
        desired_center: Tuple[float, float],
        source_position: Tuple[float, float],
        source_index: Optional[int] = None,
    ) -> Optional[Tuple[float, float]]:
        """Sample a feasible target near the desired center while respecting constraints."""

        radius_limit = max(self.target_zone_radius, max(self.cell_width, self.cell_height))
        for attempt in range(self.plan_target_sampling_attempts):
            radius = np.random.uniform(0.0, radius_limit)
            angle = np.random.uniform(0.0, 2.0 * math.pi)
            tx = desired_center[0] + radius * math.cos(angle)
            ty = desired_center[1] + radius * math.sin(angle)
            tx = min(max(tx, self.edge_margin), self.image_width - self.edge_margin)
            ty = min(max(ty, self.edge_margin), self.image_height - self.edge_margin)
            if self._is_position_valid_target(ctx, tx, ty, source_position, source_index):
                return (tx, ty)
            radius_limit = max(radius_limit * 0.9, max(self.cell_width, self.cell_height) * 0.25)

        fallback_x = min(max(desired_center[0], self.edge_margin), self.image_width - self.edge_margin)
        fallback_y = min(max(desired_center[1], self.edge_margin), self.image_height - self.edge_margin)
        if self._is_position_valid_target(ctx, fallback_x, fallback_y, source_position, source_index):
            return (fallback_x, fallback_y)
        return None

    def _is_position_valid_target(
        self,
        ctx: ExperimentContext,
        x: float,
        y: float,
        source_position: Tuple[float, float],
        source_index: Optional[int],
    ) -> bool:
        """Validate that a proposed target respects bounds and separation constraints."""

        if not self._position_within_bounds(x, y):
            return False

        min_mass = self.get_param_value('min_particle_mass')
        if ctx.tracked_positions:
            for idx, (other_x, other_y, mass) in enumerate(ctx.tracked_positions):
                # Only check separation with particles that meet mass threshold
                if mass < min_mass:
                    continue
                if source_index is not None and idx == source_index:
                    continue
                if abs(other_x - source_position[0]) < 1.0 and abs(other_y - source_position[1]) < 1.0:
                    continue
                if math.hypot(x - other_x, y - other_y) < self.min_particle_separation:
                    return False

        return True

    def _position_within_bounds(self, x: float, y: float) -> bool:
        """Check that a coordinate satisfies the configured edge margin."""

        return (
            self.edge_margin <= x <= self.image_width - self.edge_margin
            and self.edge_margin <= y <= self.image_height - self.edge_margin
        )
    
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
