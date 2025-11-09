# Quick Start Guide

## 1. First Time Setup

Make sure you have the dependencies:
```bash
conda activate tweezer
pip install grpcio protobuf matplotlib numpy pandas trackpy scipy
```

## 2. Launch the Visualizer

### Windows (Easy Way)
Double-click `run_live_gr.bat`

### Command Line
```bash
python live-gr.py
```

### Connect to Remote Server
```bash
python live-gr.py --server 10.0.63.153:50052
```

## 3. Understanding the Display

```
┌─────────────────────────┬─────────────────────────┐
│                         │                         │
│  Particle Positions     │   g(r) Correlation      │
│  (colored by density)   │   (averaged)            │
│                         │                         │
├─────────────────────────┤                         │
│                         │                         │
│  Statistics Panel       │                         │
│  - Particle count       │                         │
│  - FPS                  │                         │
│  - Uptime               │                         │
│                         │                         │
├─────────────────────────┴─────────────────────────┤
│            Connection Info & Settings              │
└───────────────────────────────────────────────────┘
```

## 4. Common Tasks

### Adjust Averaging Window
```bash
python live-gr.py --history 30
```
- Larger = smoother but slower response
- Smaller = more noise but faster updates

### Change Update Rate
```bash
python live-gr.py --interval 50
```
- Lower interval = faster updates (higher CPU)
- Higher interval = slower updates (lower CPU)

### Calibrate Pixel-to-Sigma Conversion
```bash
python live-gr.py --pixel-to-sigma 28.5
```
- Measure your particle diameter in pixels
- Default is 32.0 pixels = 1σ

### Collect Data Without GUI
```bash
python example_usage.py collect --duration 120 --name test_run
```

## 5. Interpreting g(r)

### What to Look For:

**Liquid State:**
- First peak at ~1.0-1.2 σ
- Peak height: 2-3
- Oscillations decay quickly
- Long-range g(r) → 1.0

**Crystalline State:**
- Sharp peaks at regular intervals
- High first peak (>5)
- Long-range order visible
- Slow decay of oscillations

**Gas State:**
- First peak at larger distance
- Low peak height (~1.2)
- Flat g(r) everywhere else
- g(r) ≈ 1 for most distances

**Glass State:**
- Split first peak
- High coordination
- No long-range order
- Intermediate peak heights

## 6. Troubleshooting

### "Failed to connect"
- Check ImageServer is running: `ps aux | grep ImageServer`
- Verify port: default is 50052
- Test connection: `telnet localhost 50052`

### "No particles displayed"
- Check camera is acquiring images
- Verify tracking parameters in ImageServer
- Adjust mass/quality thresholds

### Slow updates / Low FPS
- Increase `--interval` (e.g., 200ms)
- Reduce `--history` (e.g., 10 frames)
- Check network latency if remote

### g(r) looks noisy
- Increase `--history` for more averaging
- Check particle count (need >50 for good statistics)
- Verify tracking quality

## 7. Tips & Tricks

### Best Practices:
1. Start with default settings
2. Let it run for ~30 seconds to build history
3. Adjust parameters based on your system
4. Save data periodically for offline analysis

### Performance Optimization:
- Use local server when possible (lower latency)
- Typical settings: `--interval 100 --history 20`
- For slow systems: `--interval 200 --history 10`
- For analysis: `--history 50` for smooth curves

### Data Quality:
- Need >100 particles for good g(r)
- More frames averaged = better statistics
- Save data regularly for later analysis

## 8. Next Steps

- Read full README.md for detailed documentation
- Try example_usage.py for data collection
- Adjust config.yaml for persistent settings
- Export data for publication-quality plots

## Need Help?

Check the main README.md or examine the code - it's well-documented!
