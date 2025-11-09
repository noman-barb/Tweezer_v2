# Quick Start Guide - Density Redistribution Experiment

## 5-Minute Start

### Step 1: Load the Experiment
1. Open the Tweezer Dashboard
2. Navigate to "Experiment Scripts" section
3. Select "Particle Density Redistribution" from dropdown
4. Click "Reload Scripts" if not visible

### Step 2: Load a Preset
1. In "Experiment Parameters" section
2. Click "Load" button
3. Select `quick_test.yaml` for first run
4. Click "Apply Parameters"

### Step 3: Verify Setup
Ensure these services are connected:
- ✓ Camera (tracking particles)
- ✓ SLM (optical traps)
- ✓ At least 5-10 particles tracked

### Step 4: Start Experiment
1. Click "Start Experiment" button
2. Watch the log for:
   - "Analyzing density distribution..."
   - "Found X high-density regions"
   - "Found Y low-density regions"
   - "Selected source particle..."

### Step 5: Monitor Progress
Watch for:
- Movement cycles completing
- Density values in logs
- Particles moving from crowded to sparse areas
- CSV file being generated in `logs/experiment_logs/`

## What to Expect

### First 30 seconds
- Initial density analysis
- First high-density region identified
- First particle selected and moved

### After 1-2 minutes (quick_test)
- 5-10 redistribution cycles completed
- Visible reduction in clustering
- CSV log with movement data

### After 5 minutes (default preset)
- 20-30 cycles completed
- More uniform particle distribution
- Convergence toward equilibrium

## Parameter Presets Explained

### Quick Test (20 cycles, ~2-5 minutes)
**Best for**: First-time testing, verification
- Fast movements (0.3-1.5s)
- Short delays (1s)
- Coarse analysis (6×6 grid)

### Default (100 cycles, ~10-20 minutes)
**Best for**: Standard use, general experiments
- Moderate movements (0.5-4.0s)
- Standard delays (2s)
- Medium analysis (8×8 grid)

### Gentle Mode (50 cycles, ~15-30 minutes)
**Best for**: Delicate particles, high precision
- Slow movements (2.0-8.0s)
- Long delays (5s)
- Fine analysis (10×10 grid)

### Aggressive Mode (150 cycles, ~15-30 minutes)
**Best for**: Fast redistribution, robust particles
- Fast movements (0.3-2.0s)
- Short delays (1.5s)
- Frequent re-analysis

## Troubleshooting

### "Waiting for sufficient particles"
- Ensure camera is connected and streaming
- Check tracking is enabled
- Need at least 3 particles for density analysis

### "No high-density regions found"
- Particles may already be well-distributed
- Try lowering `high_density_threshold` (e.g., 65%)
- Or increase `grid_size` for finer analysis

### "No valid particles in high-density regions"
- Check `edge_margin` isn't too large
- Verify particles aren't all near edges
- Try different preset

### Particles not moving
- Verify SLM is connected
- Check `trap_intensity` is sufficient (>0.7)
- Ensure `max_movement_distance` isn't too restrictive

### Movements too slow/fast
- Adjust `movement_duration_min/max`
- Change `delay_between_moves`
- Try different preset

## Key Log Messages

### Normal Operation
```
Analyzing density distribution for 25 particles...
Found 8 high-density regions
Found 12 low-density regions
Selected source particle at (450.2, 320.5) from high-density region
Target position: (780.3, 450.1) - distance: 185.3 px
Moving from density 3.45 to 0.82
Movement duration: 2.15s at 2.58 px/frame
Reached target position (780.3, 450.1)
Action complete - cleared SLM hologram
Wait complete. Starting next cycle. Cycles completed: 15
```

### Warning Signs
```
WARNING: No image available
WARNING: No particles currently tracked
No valid particles in high-density regions
```

## Viewing Results

### Real-Time Monitoring
- Watch log for density values
- Look for "density improvement" messages
- Monitor cycles completed counter

### CSV Analysis
Location: `logs/experiment_logs/density_redistribution_YYYYMMDD_HHMMSS.csv`

Columns:
- `source_density` / `target_density`: Density at source/target
- `density_improvement`: Target - source (negative = improvement)
- `displacement_distance`: How far particle moved
- `duration_seconds`: Actual movement time
- `average_speed_px_per_frame`: Achieved speed

### Success Indicators
- Decreasing average `source_density` over time
- Increasing `density_improvement` values
- More uniform particle distribution visually

## Advanced Tips

### Fine-Tuning Density Detection
- Increase `grid_size` (10-12) for fine-grained analysis
- Adjust `density_smoothing_sigma` (1.0-2.5) for smoothness
- Modify thresholds based on actual density distribution

### Optimizing Movement
- Match `assumed_fps` to actual camera frame rate
- Adjust `slm_max_refresh_rate` to SLM capabilities
- Tune `target_reached_threshold` for precision vs speed

### Experiment Duration
- Set `max_cycles` to 0 for infinite running
- Use `analysis_interval` to balance adaptiveness vs stability
- Monitor convergence and stop manually when satisfied

### Saving Custom Configs
1. Adjust parameters in dashboard
2. Click "Save" in experiment parameters
3. Enter descriptive name (e.g., "my_particles_optimized")
4. Reuse in future sessions

## Next Steps

After successful quick test:
1. Try `default.yaml` for longer run
2. Analyze CSV data to understand dynamics
3. Adjust parameters based on your particle system
4. Save optimized configuration
5. Run production experiments with custom settings

## Common Workflows

### Workflow 1: Rapid Testing
```
quick_test.yaml → observe → adjust parameters → test again
```

### Workflow 2: Optimization
```
default.yaml → analyze CSV → identify bottlenecks → create custom preset
```

### Workflow 3: Production
```
Load custom preset → verify with quick_test → run full experiment → save data
```

## Support

If issues persist:
1. Check all services are connected
2. Verify tracking is working (see particles in camera view)
3. Review logs for error messages
4. Try `quick_test.yaml` preset
5. Check README.md for detailed documentation

## Performance Expectations

### Quick Test (quick_test.yaml)
- Duration: 2-5 minutes
- Cycles: 20
- Movements per minute: ~4-6
- Good for: Verification, testing changes

### Default (default.yaml)
- Duration: 10-20 minutes
- Cycles: 100
- Movements per minute: ~3-5
- Good for: Standard experiments, data collection

### Gentle (gentle_mode.yaml)
- Duration: 15-30 minutes
- Cycles: 50
- Movements per minute: ~1-2
- Good for: Delicate particles, precision work

### Aggressive (aggressive_mode.yaml)
- Duration: 15-30 minutes
- Cycles: 150
- Movements per minute: ~5-8
- Good for: Fast equilibration, robust particles
