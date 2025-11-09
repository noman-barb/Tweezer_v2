# Particle Density Redistribution Experiment

## Overview

This experiment analyzes the spatial distribution of particles in the optical tweezer system and redistributes them to achieve a more uniform density distribution. It identifies regions with high particle concentration and moves particles to sparse regions using SLM traps.

## How It Works

### State Machine

The experiment operates through a 5-state cycle:

1. **ANALYZING_DENSITY**: Creates a density map of particle distribution
   - Divides the image into a grid
   - Counts particles in each cell
   - Applies Gaussian smoothing
   - Identifies high-density and low-density regions

2. **SELECTING_SOURCE**: Chooses a particle from a high-density region
   - Randomly selects a high-density region
   - Finds particles near the region center
   - Selects the closest valid particle

3. **SELECTING_TARGET**: Determines where to move the particle
   - Randomly selects a low-density region
   - Ensures target is within maximum movement distance
   - Generates a position within the target zone
   - Checks separation constraints

4. **MOVING_PARTICLE**: Gradually moves the SLM trap to target
   - Creates trap at particle position
   - Incrementally moves trap toward target
   - Adapts to SLM refresh rate

5. **WAITING**: Delays between movements
   - Releases the trap
   - Waits for particles to settle
   - Checks if density re-analysis is needed

### Density Analysis

The experiment uses a grid-based density analysis:
- Divides the image into a configurable grid (e.g., 8×8 cells)
- Counts particles in each cell
- Applies Gaussian smoothing to create continuous density map
- Uses percentile thresholds to identify high/low density regions

### Movement Strategy

- **Source Selection**: Prioritizes particles in high-density regions (>75th percentile)
- **Target Selection**: Places particles in low-density regions (<25th percentile)
- **Distance Constraints**: Ensures movements stay within reasonable distances
- **Collision Avoidance**: Maintains minimum separation between particles

## Configuration Parameters

### Density Analysis
- `grid_size`: Grid resolution (default: 8×8)
- `density_smoothing_sigma`: Gaussian smoothing strength (default: 1.5)
- `high_density_threshold`: Percentile for crowded regions (default: 75%)
- `low_density_threshold`: Percentile for sparse regions (default: 25%)
- `analysis_interval`: Re-analyze every N cycles (default: 5)

### Movement
- `movement_duration_min/max`: Time range for movements (default: 0.5-4.0s)
- `max_movement_distance`: Maximum particle displacement (default: 300px)
- `delay_between_moves`: Settling time (default: 2.0s)

### Spatial Constraints
- `min_particle_separation`: Minimum distance at target (default: 40px)
- `edge_margin`: Safety margin from edges (default: 64px)
- `target_zone_radius`: Randomization radius at target (default: 100px)

### SLM
- `slm_max_refresh_rate`: Update frequency (default: 30Hz)
- `trap_intensity`: Trap power (default: 0.9)
- `trap_z_offset`: Axial trap position (default: 0.0)

### Control
- `max_cycles`: Maximum redistribution cycles (default: 100, 0=infinite)

## Preset Configurations

### Default (`default.yaml`)
Balanced parameters for typical use cases.

### Quick Test (`quick_test.yaml`)
- Faster movements (0.3-1.5s)
- Shorter delays (1.0s)
- Coarser grid (6×6)
- Only 20 cycles
- Good for testing and verification

### Gentle Mode (`gentle_mode.yaml`)
- Slow, careful movements (2.0-8.0s)
- Long settling delays (5.0s)
- Fine grid analysis (10×10)
- Higher trap intensity (0.95)
- Best for delicate particles

### Aggressive Mode (`aggressive_mode.yaml`)
- Fast movements (0.3-2.0s)
- Short delays (1.5s)
- Lower density thresholds (more redistribution)
- Longer distances (400px)
- 150 cycles for thorough redistribution

## Usage

1. **Load in Dashboard**: Select "Particle Density Redistribution" from experiment scripts
2. **Choose Preset**: Load a parameter configuration (default/quick_test/gentle/aggressive)
3. **Adjust Parameters**: Fine-tune in the dashboard as needed
4. **Start Experiment**: Click "Start" to begin redistribution
5. **Monitor Progress**: Watch density map and movement log
6. **Save Configuration**: Save custom parameter sets for future use

## Output Data

The experiment logs detailed CSV data:
- Cycle number and frame ranges
- Source and target positions
- Density values at source/target
- Movement distances and durations
- Actual speeds achieved

CSV files are saved to: `logs/experiment_logs/density_redistribution_YYYYMMDD_HHMMSS.csv`

## Tips

- **Start with Quick Test**: Verify behavior before longer runs
- **Monitor Density Map**: Watch for convergence toward uniform distribution
- **Adjust Thresholds**: If few movements occur, adjust density thresholds
- **Check Edge Margin**: Ensure sufficient margin for your trap configuration
- **Re-analysis Frequency**: Balance between responsive adaptation and stability

## Algorithm Details

### Density Map Construction
1. Create grid over image dimensions
2. Bin particles into grid cells
3. Apply Gaussian filter for smoothing
4. Identify regions above/below percentile thresholds

### Source Selection Priority
1. Find all high-density regions (>threshold)
2. For each region, find nearby particles
3. Select particle closest to region center
4. Apply edge and constraint filters

### Target Selection
1. Find low-density regions within movement distance
2. Sort by distance to source
3. Randomly select from closest 50%
4. Generate random position within target zone
5. Verify minimum separation from other particles

### Movement Control
- Smooth trap trajectory with incremental steps
- Adaptive step size based on SLM refresh rate
- Multiple updates per frame if refresh rate allows
- Early termination when within threshold

## Troubleshooting

**No movements happening:**
- Check density thresholds (may be too extreme)
- Verify sufficient particles are tracked
- Check max_movement_distance isn't too restrictive

**Particles not reaching targets:**
- Increase movement_duration_max
- Decrease assumed_fps if actual frame rate is lower
- Increase target_reached_threshold

**System becomes less uniform:**
- Increase analysis_interval for more frequent re-analysis
- Adjust high/low density thresholds
- Check min_particle_separation isn't too restrictive

**Movements too aggressive:**
- Switch to gentle_mode preset
- Increase movement durations
- Increase delay_between_moves

## Related Experiments

- **Random Particle Displacement**: Random movements without density consideration
- **Structured Pattern Formation**: Creates specific geometric patterns
