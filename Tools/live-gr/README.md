# Live g(r) Pair Correlation Visualizer

Real-time visualization of particle tracking data with pair correlation function g(r) analysis.

## Features

- **Real-time particle positions** with density-based coloring
- **Live g(r) calculation** with temporal averaging and error bands
- **Statistics panel** showing particle count, FPS, and uptime
- **Non-blocking updates** using matplotlib animations
- **Beautiful dark theme** with customizable styles

## Installation

Ensure you have the required dependencies:

```bash
pip install grpcio protobuf matplotlib numpy pandas trackpy scipy
```

## Usage

### Basic Usage

Connect to the default image server (localhost:50052):

```bash
python live-gr.py
```

### Advanced Options

```bash
python live-gr.py --server 10.0.63.153:50052 --history 30 --interval 50
```

### Command-Line Arguments

- `--server`: Image server address (default: `localhost:50052`)
- `--history`: Number of frames to average for g(r) (default: `20`)
- `--interval`: Update interval in milliseconds (default: `100`)
- `--pixel-to-sigma`: Conversion from pixels to particle diameter σ (default: `32.0`)
- `--style`: Matplotlib style (`dark_background`, `seaborn`, `ggplot`, `bmh`)

## Display Layout

The visualization consists of:

1. **Top Left**: Real-time particle positions (0-1456 × 0-1090 pixels)
   - Particles colored by local density (viridis colormap)
   - Shows instantaneous snapshot of tracked particles

2. **Top Right & Middle Right**: Pair correlation function g(r)
   - Averaged over specified history window
   - Shaded region shows ±1 standard deviation
   - Horizontal line at g(r) = 1 for reference

3. **Middle Left**: Statistics panel
   - Total particle count
   - Frame counter
   - Processing FPS
   - Connection uptime

4. **Bottom**: Connection and configuration info

## How It Works

1. Connects to ImageServer gRPC service via `GetLatestTracks` RPC
2. Fetches particle coordinates (x, y) from tracking detections
3. Calculates pair correlation using trackpy's `pair_correlation_2d`
4. Maintains rolling buffer of g(r) for temporal smoothing
5. Updates visualization at specified interval (non-blocking)

## Improvements Over Old Version

- ✅ Uses gRPC instead of WebSockets (more robust)
- ✅ Proper error handling and reconnection logic
- ✅ Efficient buffering with deque
- ✅ Statistical error bands on g(r)
- ✅ Density-based particle coloring
- ✅ Clean object-oriented design
- ✅ Comprehensive statistics display
- ✅ Command-line configuration
- ✅ Professional styling with ASCII art borders

## Troubleshooting

### Connection Failed

- Ensure ImageServer is running on the specified address
- Check firewall settings if connecting to remote host
- Verify port number matches server configuration

### No Particles Displayed

- Check that image acquisition is active
- Verify tracking parameters in ImageServer
- Ensure sufficient particle mass/quality for detection

### Low FPS

- Increase `--interval` to reduce update frequency
- Reduce `--history` window size
- Check network latency to remote server

## Configuration

The visualizer automatically adapts to your tracking setup. Key parameters:

- **pixel_to_sigma**: Calibrate this based on your particle diameter in pixels
  - Default assumes 32 pixels = 1 particle diameter
  - Measure actual particle diameter and adjust accordingly

- **history**: Balance between smoothness and responsiveness
  - Higher values = smoother g(r) but slower response to changes
  - Lower values = more noise but faster updates

## Example Output

```
╔═══════════════════════════════════════════════╗
║   Live g(r) Pair Correlation Visualizer      ║
╚═══════════════════════════════════════════════╝

✓ Connected to image server at localhost:50052

╔═══════════════════════════╗
║    TRACKING STATISTICS    ║
╠═══════════════════════════╣
║ Particles:           247  ║
║ Frames:             1432  ║
║ FPS:               25.43  ║
║ Uptime:            56.3 s ║
║ Status:       Connected   ║
╚═══════════════════════════╝
```

## Advanced Usage

### Data Collection and Export

Use `example_usage.py` to collect data for analysis:

```bash
# Collect 120 seconds of data
python example_usage.py collect --duration 120 --name experiment_01

# Collect from remote server
python example_usage.py collect --server 10.0.63.153:50052 --duration 300 --name cooling_phase
```

This will create a directory with:
- `particles.csv`: Final particle positions
- `gr_averaged.csv`: Time-averaged g(r) with metadata
- `gr_history.json`: Complete g(r) time series
- `structure_metrics.json`: Calculated structural parameters
- `session_metadata.json`: Session information

### Comparing Multiple Sessions

Compare g(r) from different experiments:

```bash
python example_usage.py compare data/experiment_01 data/experiment_02 data/experiment_03 --output comparison.png
```

### Programmatic Access

```python
from live_gr import LiveGrVisualizer, GrCalculator
from utils import calculate_structure_metrics, export_session_data

# Create visualizer
viz = LiveGrVisualizer(server_address="localhost:50052")
viz.connect()

# Collect data
for _ in range(100):
    data = viz.fetch_latest_tracks()
    if data:
        viz.process_tracking_data(data)
    time.sleep(0.1)

# Analyze
avg_r, avg_gr = viz.buffer.get_averaged_gr()
metrics = calculate_structure_metrics(avg_r, avg_gr)
print(f"Coordination number: {metrics['coordination_number']:.2f}")

# Export
export_session_data(viz.buffer, Path("./my_experiment"))
viz.disconnect()
```

## Files

- `live-gr.py`: Main visualization script
- `utils.py`: Data export and analysis utilities
- `example_usage.py`: Examples for data collection and comparison
- `config.yaml`: Configuration file template
- `run_live_gr.bat`: Windows launcher script
- `README.md`: This file

## Structure Metrics

The analysis calculates:

- **First Peak Position**: Nearest neighbor distance in σ units
- **First Peak Height**: Maximum value of g(r) at first peak
- **Coordination Number**: Integral of first shell (2πr ∫ g(r) dr)
- **Long-Range Value**: Average g(r) at large distances (should → 1)
- **Phase Transition Detection**: Monitors changes in peak position/height

## License

Part of the Tweezer project.
