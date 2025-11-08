# SLM Auto Fine-Tuning System

## Overview

The Auto Fine-Tuning system automatically refines the SLM (Spatial Light Modulator) calibration by sampling random positions within the camera view, pinning optical tweezers at those locations, and comparing the intended positions with the actual tracked particle positions. This iterative process generates an optimized affine transformation that significantly improves the alignment between camera coordinates and SLM coordinates.

## Features

### 🎯 Intelligent Sampling
- **Random Sampling**: Automatically generates random sample points within a configurable margin from image edges
- **Particle Tracking**: Uses the existing particle tracking system to detect trapped particles
- **Error Measurement**: Calculates precise positional errors between intended and actual trap locations

### 📊 Visual Feedback
- **Real-time Visualization**: Beautiful animated overlays showing:
  - **Sampling Area**: Blue rectangle indicating the valid sampling region
  - **Completed Samples**: Color-coded markers (green/yellow/red based on error magnitude)
  - **Current Target**: Pulsing cyan crosshair at the current pinning location
  - **Matched Particle**: Pulsing green circle at the detected particle position
  - **Error Vectors**: Lines connecting targets to matched particles
  - **Progress Indicator**: Sample counter (e.g., "Sample 5/12")

### 🔧 Calibration Refinement
- **Affine Transformation**: Uses least-squares optimization to compute the best-fit affine matrix
- **Legacy Format**: Automatically converts to the 3-point calibration format used by the existing system
- **Error Metrics**: Provides comprehensive before/after statistics:
  - RMS (Root Mean Square) error
  - Maximum error
  - Mean error
  - Improvement percentage

### 💾 Configuration Management
- **Automatic Naming**: Fine-tuned configs are saved with datetime stamps (e.g., `finetuning_20250105_143022`)
- **Separate Storage**: Stored in `/finetuning` subfolder, keeping them distinct from manual configs
- **Metadata Display**: Shows key information:
  - Creation timestamp
  - Base configuration name
  - Number of samples
  - Error reduction statistics
  - Improvement percentage

## Usage

### Starting a Fine-Tuning Session

1. **Prerequisites**:
   - Ensure the Image Server is connected and tracking particles
   - Ensure the SLM is connected
   - Load a base calibration configuration (your current best guess)
   - Have particles in the sample (preferably distributed across the field of view)

2. **Configure Parameters**:
   - **Sample Count**: Number of random positions to sample (default: 12, range: 3-100)
     - More samples = better accuracy but longer runtime
     - Minimum 3 samples required for affine transformation
     - Recommended: 10-20 for good balance
   - **Edge Margin**: Distance from image edges to avoid (default: 50px, range: 10-200px)
     - Prevents sampling near edges where particle detection may be unreliable
     - Adjust based on your field of view and trap stability

3. **Control Buttons**:
   - **Start**: Begin the fine-tuning process
   - **Pause**: Temporarily halt sampling (can be resumed)
   - **Stop**: Abort the process (no config will be saved)

4. **Visualization Toggle**:
   - **Show Visualization**: Enable/disable the animated overlay
   - Keep enabled to see what the system is doing in real-time

### Loading a Fine-Tuned Configuration

1. Select a configuration from the **Fine-Tuned Config** dropdown
   - Configs are listed in reverse chronological order (newest first)
   - Names include the datetime: `finetuning_YYYYMMDD_HHMMSS`

2. View the metadata:
   - Creation timestamp
   - Base configuration it was derived from
   - Number of samples collected
   - Error reduction statistics
   - Overall improvement percentage

3. Click **Load Selected** to apply the configuration
   - The affine parameters will be updated immediately
   - SLM will be marked as dirty and automatically re-sent

4. Click **Delete** to remove unwanted configurations
   - A confirmation dialog will appear
   - Cannot be undone

## How It Works

### 1. Sampling Phase
For each sample:
1. Generate a random (x, y) position within the sampling area
2. Clear existing SLM points
3. Pin a single trap at the target position
4. Send command to SLM
5. Wait 1.5 seconds for trap stabilization and particle capture
6. Query the particle tracker for the nearest detected particle
7. Calculate the error (Euclidean distance) between target and actual position
8. Store the sample data

### 2. Calibration Refinement
After collecting all samples:
1. Extract source points (particle positions) and target points (SLM positions)
2. Build a least-squares system to solve for the affine transformation:
   ```
   [x', y'] = [a, b, c] @ [x, y, 1]
              [d, e, f]
   ```
3. Compute the affine matrix that minimizes the RMS error
4. Transform the source points using the refined matrix
5. Calculate new error metrics

### 3. Legacy Format Conversion
The affine matrix is converted to the 3-point calibration format:
- Point 0: (0, 0) → transformed
- Point 1: (100, 0) → transformed  
- Point 2: (0, 100) → transformed

This ensures compatibility with the existing calibration system.

### 4. Saving Results
A `FineTuningResult` object is created containing:
- All sample data (target positions, particle positions, errors)
- Before/after error statistics
- Refined calibration parameters
- Metadata (timestamp, base config, sample count, margins)

The result is saved as a JSON file in `SLM/slm_config/finetuning/`.

## Visual Indicators

### Color Coding
- **Blue Rectangle**: Sampling area boundary
- **Cyan Pulsing**: Current target position
- **Green Pulsing**: Currently matched particle
- **Green Markers**: Low error samples (< 5px)
- **Yellow Markers**: Medium error samples (5-15px)
- **Red Markers**: High error samples (> 15px)
- **Yellow Lines**: Error vectors connecting targets to particles

### Animation
The visualization includes smooth pulsing animations that:
- Draw attention to the current sampling operation
- Indicate system activity
- Make the process visually engaging

## File Structure

```
SLM/
  slm_config/
    slm_config_manager.py          # Existing manual config manager
    slm_finetuning_manager.py      # New fine-tuning manager
    finetuning/                    # Fine-tuned configs directory
      finetuning_20250105_143022.json
      finetuning_20250105_145130.json
      ...
    default.json                   # Manual configs
    slm1.json
    tweezer_area.json
    ...
```

## Tips for Best Results

### Sample Placement
- Ensure particles are distributed across the field of view
- Avoid clustering all particles in one region
- The more uniform the distribution, the better the calibration

### Sample Count
- Start with 10-15 samples for initial testing
- Use 20-30 samples for production calibration
- More samples provide diminishing returns after ~30

### Edge Margins
- Use larger margins (100-150px) if:
  - Particles near edges are hard to detect
  - Trap stability is reduced near edges
- Use smaller margins (30-50px) if:
  - You need to calibrate close to edges
  - Particle detection is reliable throughout

### Iterative Refinement
- Load your best manual calibration first
- Run fine-tuning with ~12 samples
- Load the resulting config
- Run fine-tuning again with ~20 samples
- Each iteration should show improvement

### Quality Indicators
- **Good**: >50% error reduction, RMS error < 3px
- **Acceptable**: 20-50% error reduction, RMS error 3-8px
- **Needs Work**: <20% improvement or RMS error > 8px
  - Try adjusting your base calibration
  - Check particle tracking parameters
  - Verify SLM is properly aligned

## Technical Details

### Affine Transformation Math
The system solves for a 2D affine transformation matrix:
```
[x']   [a  b  c] [x]
[y'] = [d  e  f] [y]
[1 ]   [0  0  1] [1]
```

Where:
- (x, y) are particle (camera) coordinates
- (x', y') are target (SLM) coordinates
- Parameters a-f are solved using least-squares

The transformation captures:
- **Translation**: c, f
- **Rotation**: a, b, d, e (combined with scale)
- **Scale**: Magnitude of transformation
- **Shear**: Off-diagonal terms

### Error Metrics
- **RMS Error**: √(Σ(errors²) / n) - Most important metric
- **Max Error**: Worst case misalignment
- **Mean Error**: Average misalignment

### Data Storage Format
Each fine-tuning result is saved as JSON:
```json
{
  "name": "finetuning_20250105_143022",
  "timestamp": "2025-01-05T14:30:22+00:00",
  "base_config_name": "default",
  "sample_count": 12,
  "margin_pixels": 50.0,
  "sample_area_min_x": 50.0,
  "sample_area_min_y": 50.0,
  "sample_area_max_x": 1870.0,
  "sample_area_max_y": 1102.0,
  "samples": [
    {
      "slm_x": 234.5,
      "slm_y": 678.2,
      "particle_x": 236.1,
      "particle_y": 679.8,
      "error": 2.3
    },
    ...
  ],
  "rms_error_before": 8.45,
  "rms_error_after": 2.13,
  "max_error_before": 15.2,
  "max_error_after": 4.8,
  "mean_error_before": 7.1,
  "mean_error_after": 1.9,
  "refined_cam_x0": 0.0,
  "refined_cam_y0": 0.0,
  "refined_slm_x0": 12.3,
  "refined_slm_y0": -4.5,
  ... (other calibration parameters)
}
```

## Troubleshooting

### "No particle found" warnings
- **Cause**: Particle tracking not detecting particles near trap
- **Solutions**:
  - Check tracking parameters (threshold, min_mass, etc.)
  - Increase trap intensity
  - Ensure particles are present in sample
  - Reduce sample count to avoid sparse regions

### Poor improvement percentage
- **Cause**: Base calibration is very far off
- **Solutions**:
  - Manually adjust base calibration closer to correct values
  - Use 3-point manual calibration first
  - Check SLM alignment/focus
  - Verify camera-SLM coordinate systems match

### High variation in errors
- **Cause**: Inhomogeneous calibration (good in center, bad at edges)
- **Solutions**:
  - Increase sample count
  - Reduce edge margins to sample more uniformly
  - Check for optical distortions
  - Consider non-linear calibration methods

### Fine-tuning gets stuck
- **Cause**: Trap not capturing particles
- **Solutions**:
  - Check SLM connection
  - Verify trap intensity
  - Ensure base calibration is reasonable
  - Stop and adjust parameters

## Future Enhancements

Potential improvements for future versions:
- **Adaptive sampling**: Focus samples in high-error regions
- **Non-linear calibration**: Support for optical distortions
- **Multi-plane calibration**: Z-axis fine-tuning
- **Automatic retry**: Re-sample failed points
- **Confidence metrics**: Statistical significance tests
- **Comparison mode**: Side-by-side before/after visualization

## API Reference

### Controller Methods

```python
# Start fine-tuning
controller.start_finetuning(sample_count: int, margin_pixels: float) -> None

# Stop/pause fine-tuning
controller.stop_finetuning() -> None
controller.pause_finetuning() -> None

# Load/delete configs
controller.load_finetuning_config(name: str) -> bool
controller.delete_finetuning_config(name: str) -> bool

# Query configs
controller.list_finetuning_configs() -> List[str]
controller.get_finetuning_metadata(name: str) -> Optional[Dict[str, Any]]
```

### Manager Methods

```python
# Create manager
manager = FineTuningManager(base_config_dir: Path)

# Save/load results
manager.save_result(result: FineTuningResult) -> bool
manager.load_result(name: str) -> Optional[FineTuningResult]
manager.delete_result(name: str) -> bool

# List and query
manager.list_results() -> List[str]
manager.get_result_metadata(name: str) -> Optional[Dict[str, Any]]

# Generate name
name = manager.generate_name()  # Returns datetime-based name
```

### Data Classes

```python
@dataclass
class FineTuningSample:
    slm_x: float
    slm_y: float
    particle_x: float
    particle_y: float
    error: float

@dataclass
class FineTuningResult:
    name: str
    timestamp: str
    base_config_name: str
    sample_count: int
    margin_pixels: float
    samples: List[Dict[str, float]]
    rms_error_before: float
    rms_error_after: float
    # ... (other fields)
    
    def get_improvement_percentage(self) -> float
```

## Contributing

When contributing to the fine-tuning system:
1. Maintain backward compatibility with existing calibration format
2. Add unit tests for mathematical transformations
3. Update this documentation for new features
4. Follow the existing code style and patterns

## License

This feature is part of the Tweezer control system and follows the same license as the parent project.
