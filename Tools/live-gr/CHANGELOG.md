# Changelog

## Version 2.0 - Complete Rewrite (2025-11-08)

### Major Changes
- Complete rewrite from WebSocket-based to gRPC-based architecture
- Object-oriented design with clean separation of concerns
- Professional visualization with multiple display panels

### New Features
- ✨ Real-time particle position display with density-based coloring
- ✨ Time-averaged g(r) with statistical error bands
- ✨ Live statistics panel (particle count, FPS, uptime)
- ✨ Data export utilities for offline analysis
- ✨ Structure metrics calculation (coordination number, peak positions)
- ✨ Phase transition detection
- ✨ Multi-session comparison tools
- ✨ Command-line configuration
- ✨ YAML configuration file support
- ✨ Cross-platform launcher scripts

### Architecture Improvements
- 🏗️ Modular design: GrCalculator, TrackingDataBuffer, LiveGrVisualizer
- 🏗️ Proper error handling and graceful degradation
- 🏗️ Type hints throughout for maintainability
- 🏗️ Efficient data structures (deque for O(1) operations)
- 🏗️ Non-blocking matplotlib animations

### Visualization Enhancements
- 🎨 Professional dark theme with customizable styles
- 🎨 3-panel layout (particles, g(r), statistics)
- 🎨 Density-based particle coloring (viridis colormap)
- 🎨 Statistical error bands on g(r) plot
- 🎨 Reference line at g(r) = 1
- 🎨 ASCII art borders and formatting
- 🎨 Auto-scaling axes with proper limits

### Performance
- ⚡ Efficient gRPC polling (native to system)
- ⚡ Configurable update rates (10-100 Hz)
- ⚡ Optimized data structures for minimal overhead
- ⚡ Batch processing of tracking data
- ⚡ Lazy imports for faster startup

### Documentation
- 📚 Comprehensive README.md
- 📚 Quick start guide (QUICK_START.md)
- 📚 Example usage scripts
- 📚 Configuration template (config.yaml)
- 📚 Complete docstrings
- 📚 This changelog

### New Files
- `live-gr.py` - Main visualizer (replaces old-live-gr.py)
- `utils.py` - Data export and analysis utilities
- `example_usage.py` - Example scripts for data collection
- `config.yaml` - Configuration template
- `run_live_gr.bat` - Windows launcher
- `run_live_gr.sh` - Unix/Linux launcher
- `README.md` - Full documentation
- `QUICK_START.md` - Quick reference
- `SUMMARY.md` - Overview of changes
- `CHANGELOG.md` - This file

### Breaking Changes
- ❌ No longer uses WebSocket connection (now gRPC)
- ❌ Configuration moved from hardcoded to CLI/YAML
- ❌ Different import path (from Camera/image_exchange_pb2_grpc)

### Migration Guide
Old way:
```python
# Connected to WebSocket at ws://10.0.63.153:4012/ws
# Hardcoded parameters in script
```

New way:
```bash
# Use gRPC connection to ImageServer
python live-gr.py --server 10.0.63.153:50052

# All parameters configurable
python live-gr.py --history 30 --interval 100 --pixel-to-sigma 32
```

### Dependencies
- grpcio
- protobuf
- matplotlib
- numpy
- pandas
- trackpy
- scipy (for density calculation)

### Known Issues
- Type checking shows false positives for matplotlib/trackpy (harmless)
- First few frames may show NaN until buffer fills
- Very high particle counts (>1000) may slow updates

### Future Enhancements
- [ ] Real-time plot of structure metrics over time
- [ ] Save/load visualization state
- [ ] Custom colormap editor
- [ ] Integration with experiment automation
- [ ] WebGL-based 3D visualization
- [ ] Multi-server monitoring
- [ ] Alert system for phase transitions
- [ ] Export to publication-ready formats

---

## Version 1.0 - Original (old-live-gr.py)

### Features
- Basic particle position plotting
- Simple g(r) calculation
- WebSocket connection
- 2-panel matplotlib display

### Limitations
- WebSocket protocol (not native to system)
- Hardcoded parameters
- No error handling
- No data export
- Basic visualization
- Procedural code structure
