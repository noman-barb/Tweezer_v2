# Live g(r) Visualizer - Documentation Index

Welcome to the Live g(r) Pair Correlation Function Visualizer!

## 📚 Documentation

Choose your starting point based on your needs:

### 🚀 Getting Started
- **[QUICK_START.md](QUICK_START.md)** - Start here! Quick reference for first-time users
  - Installation steps
  - Launch instructions
  - Common tasks
  - Troubleshooting

### 📖 Complete Guide
- **[README.md](README.md)** - Full documentation
  - Features overview
  - Detailed usage instructions
  - Configuration options
  - Advanced usage examples
  - Structure metrics explanation

### 🎯 Quick Reference
- **[SUMMARY.md](SUMMARY.md)** - What's new and improved
  - Overview of improvements
  - Architecture comparison
  - File structure
  - Technical highlights

### 📝 Version History
- **[CHANGELOG.md](CHANGELOG.md)** - Version history and changes
  - What's new in v2.0
  - Breaking changes
  - Migration guide
  - Future roadmap

## 📁 Files

### Core Scripts
- **`live-gr.py`** - Main visualizer application
  - Run this to start the live visualization
  - Connects to ImageServer via gRPC
  - Displays particles and g(r) in real-time

- **`utils.py`** - Utility functions
  - Data export to CSV/JSON
  - Structure metrics calculation
  - Phase transition detection
  - Multi-session comparison

- **`example_usage.py`** - Example workflows
  - Data collection scripts
  - Session comparison
  - Programmatic usage examples

### Configuration
- **`config.yaml`** - Configuration template
  - Server settings
  - Display parameters
  - g(r) calculation options
  - Visualization style

### Launchers
- **`run_live_gr.bat`** - Windows launcher
- **`run_live_gr.sh`** - Unix/Linux launcher

### Reference
- **`old-live-gr.py`** - Original version (for comparison)

## 🎓 Learning Path

### Beginner
1. Read [QUICK_START.md](QUICK_START.md)
2. Run `python live-gr.py` or `run_live_gr.bat`
3. Observe the default visualization
4. Try changing `--history` and `--interval`

### Intermediate
1. Read [README.md](README.md) sections on configuration
2. Experiment with command-line arguments
3. Try data collection: `python example_usage.py collect --duration 60`
4. Explore exported CSV files

### Advanced
1. Read [SUMMARY.md](SUMMARY.md) for architecture details
2. Study `utils.py` for analysis functions
3. Write custom analysis scripts using the API
4. Integrate with experiment automation

## 🔧 Common Tasks

### Just want to visualize?
```bash
python live-gr.py
```

### Connect to remote server?
```bash
python live-gr.py --server 10.0.63.153:50052
```

### Collect data for analysis?
```bash
python example_usage.py collect --duration 120 --name my_experiment
```

### Compare multiple runs?
```bash
python example_usage.py compare data/run1 data/run2 --output comparison.png
```

### Need help?
```bash
python live-gr.py --help
python example_usage.py --help
```

## 📊 What You'll See

The visualizer displays:
- **Top Left**: Particle positions (colored by local density)
- **Top/Middle Right**: g(r) with error bands
- **Middle Left**: Statistics (count, FPS, uptime)
- **Bottom**: Connection info and settings

## 🎨 Customization

### Quick style change
```bash
python live-gr.py --style seaborn
```

### Adjust averaging
```bash
python live-gr.py --history 30  # Smoother, slower response
python live-gr.py --history 10  # Noisier, faster response
```

### Change update rate
```bash
python live-gr.py --interval 50   # Faster updates (20 Hz)
python live-gr.py --interval 200  # Slower updates (5 Hz)
```

### Calibrate distance scale
```bash
python live-gr.py --pixel-to-sigma 28.5  # Adjust to your particles
```

## 🐛 Troubleshooting

See [QUICK_START.md](QUICK_START.md) section 6 for common issues:
- Connection problems
- No particles displayed
- Slow/laggy updates
- Noisy g(r)

## 📞 Support

1. Check [QUICK_START.md](QUICK_START.md) troubleshooting section
2. Review [README.md](README.md) for detailed explanations
3. Examine [SUMMARY.md](SUMMARY.md) for technical details
4. Check [CHANGELOG.md](CHANGELOG.md) for known issues

## 🎯 Best Practices

1. **Start simple**: Use defaults first
2. **Calibrate**: Measure your particle diameter in pixels
3. **Collect baseline**: Save data from known good states
4. **Compare**: Use comparison tools to track changes
5. **Document**: Save session metadata with experiments

## 📦 Dependencies

Required packages:
```
grpcio
protobuf
matplotlib
numpy
pandas
trackpy
scipy
```

Install via:
```bash
pip install grpcio protobuf matplotlib numpy pandas trackpy scipy
```

Or use your conda environment:
```bash
conda activate tweezer
```

## 🚀 Next Steps

1. Run the visualizer with defaults
2. Let it collect data for ~30 seconds
3. Adjust parameters to your preference
4. Try data collection for offline analysis
5. Compare different experimental conditions

---

**Happy visualizing! 🎉**

For questions or issues, check the documentation files listed above.
