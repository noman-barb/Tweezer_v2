# Density Redistribution Experiment - Implementation Summary

## Created Files

### Main Experiment Script
- **Location**: `ExperimentScripts/experiment1/density_redistribution.py`
- **Class**: `DensityRedistribution`
- **Lines of Code**: ~900 lines

### Parameter Configurations
Directory: `ExperimentScripts/Density_Redistribution_(Auto-Config)/params/`

1. **default.yaml** - Balanced settings for general use
2. **quick_test.yaml** - Fast testing configuration (20 cycles)
3. **gentle_mode.yaml** - Slow, careful movements for delicate particles
4. **aggressive_mode.yaml** - Fast, thorough redistribution (150 cycles)

### Documentation
- **README.md** - Comprehensive guide to the experiment

## Key Features

### 1. Density Analysis System
- Grid-based particle density mapping (configurable resolution)
- Gaussian smoothing for continuous density fields
- Percentile-based threshold identification
- Periodic re-analysis to track distribution changes

### 2. Intelligent Particle Selection
- **Source Selection**: Targets particles in high-density regions (>75th percentile)
- **Target Selection**: Places particles in low-density regions (<25th percentile)
- **Distance Optimization**: Prefers closer low-density targets
- **Constraint Validation**: Edge margins and separation requirements

### 3. Movement Control
- Smooth trap trajectories with incremental steps
- Adaptive speed based on SLM refresh rate
- Multiple updates per frame for high refresh rates
- Configurable movement durations and distances

### 4. State Machine Architecture
```
ANALYZING_DENSITY → SELECTING_SOURCE → SELECTING_TARGET → MOVING_PARTICLE → WAITING
         ↑                                                                        ↓
         └────────────────────────────────────────────────────────────────────────┘
```

### 5. Comprehensive Logging
CSV output includes:
- Cycle numbers and frame ranges
- Source/target positions and densities
- Movement metrics (distance, duration, speed)
- Density improvement calculations

## Configuration Parameters

### Density Analysis (5 parameters)
- `grid_size`: Resolution of density map
- `density_smoothing_sigma`: Smoothing strength
- `high_density_threshold`: Definition of "crowded"
- `low_density_threshold`: Definition of "sparse"
- `analysis_interval`: How often to re-analyze

### Movement (4 parameters)
- `movement_duration_min/max`: Movement time ranges
- `max_movement_distance`: Maximum displacement
- `delay_between_moves`: Settling time

### Spatial Constraints (3 parameters)
- `min_particle_separation`: Collision avoidance
- `edge_margin`: Boundary safety
- `target_zone_radius`: Target position randomization

### SLM Configuration (3 parameters)
- `slm_max_refresh_rate`: Update frequency
- `trap_intensity`: Trap power
- `trap_z_offset`: Axial positioning

### Performance (3 parameters)
- `assumed_fps`: Frame rate for calculations
- `target_reached_threshold`: Positioning precision
- `step_close_enough_threshold`: Multi-step precision

### Logging (2 parameters)
- `log_progress_interval`: Movement progress frequency
- `log_status_interval`: Status message frequency

### Control (1 parameter)
- `max_cycles`: Experiment duration limit

**Total: 29 configurable parameters**

## Comparison with Random Displacement

| Feature | Random Displacement | Density Redistribution |
|---------|-------------------|----------------------|
| Movement Strategy | Random selection | Density-based targeting |
| Target Selection | Random direction/distance | Low-density regions |
| Analysis | None | Periodic density mapping |
| Goal | General mixing | Uniform distribution |
| States | 3 | 5 |
| Complexity | Low | Medium-High |
| Parameters | 20 | 29 |

## Algorithm Highlights

### Density Map Generation
```python
1. Create NxN grid over image
2. Count particles per cell
3. Apply Gaussian smoothing
4. Calculate percentile thresholds
5. Identify high/low regions
```

### Movement Decision Logic
```python
1. Select random high-density region
2. Find nearest particle to region center
3. Find low-density regions within max distance
4. Select from closest 50% randomly
5. Generate position in target zone
6. Validate separation constraints
7. Execute movement
```

## Integration with Dashboard

The experiment automatically integrates with the dashboard GUI:
- All parameters appear in dashboard controls
- Parameters grouped by category
- Type-appropriate UI widgets (sliders, spin boxes)
- Real-time parameter updates
- Save/load configuration presets
- CSV logging for analysis

## Usage Workflow

1. Load experiment in dashboard
2. Select parameter preset (or use default)
3. Adjust parameters if needed
4. Start experiment
5. Monitor density convergence
6. Save successful configurations
7. Analyze CSV logs

## Testing Recommendations

### Phase 1: Quick Validation
- Use `quick_test.yaml` preset
- Verify state transitions
- Check movement mechanics
- Monitor 20 cycles

### Phase 2: Parameter Tuning
- Adjust density thresholds
- Tune movement speeds
- Optimize analysis interval
- Test edge cases

### Phase 3: Long-Duration Testing
- Use `default.yaml` or `aggressive_mode.yaml`
- Run 100+ cycles
- Monitor convergence metrics
- Analyze CSV data

## Future Enhancements

Possible extensions:
1. **Voronoi-based density** - Use Voronoi tessellation instead of grid
2. **Multi-particle moves** - Move multiple particles simultaneously
3. **Density visualization** - Real-time heatmap overlay on image
4. **Convergence metrics** - Track distribution uniformity over time
5. **Adaptive thresholds** - Dynamically adjust based on progress
6. **Pattern formation** - Create specific density patterns (e.g., rings)

## Dependencies

Required Python packages:
- `numpy` - Array operations
- `scipy` - Spatial algorithms (KDTree, Voronoi, gaussian_filter)
- `csv` - Data logging
- `time`, `datetime` - Timing and logging

## File Structure
```
ExperimentScripts/
├── experiment1/
│   ├── density_redistribution.py      # Main script
│   └── random_displacement_v2.py      # Reference script
├── Density_Redistribution_(Auto-Config)/
│   ├── params/
│   │   ├── default.yaml
│   │   ├── quick_test.yaml
│   │   ├── gentle_mode.yaml
│   │   └── aggressive_mode.yaml
│   └── README.md
└── base_script.py                      # Framework
```

## Notes

- Script follows the same pattern as `random_displacement_v2.py`
- Uses auto-configuration system for dashboard integration
- Fully compatible with existing experiment framework
- CSV logging matches format of other experiments
- Ready to use without modifications to dashboard code
