# Experiment Script System - Implementation Summary

## Overview

A complete experiment automation framework has been implemented for the Tweezer Dashboard GUI, providing a flexible plugin system for creating automated particle manipulation experiments.

## Architecture

### Core Components

1. **`ExperimentScripts/base_script.py`**
   - `ExperimentContext`: Provides full access to system state and control
   - `ExperimentScript`: Abstract base class for experiments
   - Lifecycle methods: `setup()`, `on_frame()`, `teardown()`
   - Optional callbacks: `on_pause()`, `on_resume()`, `on_error()`

2. **`ExperimentScripts/script_manager.py`**
   - `ScriptManager`: Handles script discovery, loading, and execution
   - Automatic scanning of Python files for ExperimentScript subclasses
   - Lifecycle management (start, stop, pause, resume)
   - Error handling and recovery

3. **`ExperimentScripts/experiment1/random_displacement.py`**
   - Demo experiment showcasing the framework
   - State machine implementation
   - Particle selection and manipulation
   - Time-based sequencing

### Dashboard Integration

The experiment system is fully integrated into `dashboard_gui.py`:

1. **Context Building** (`_build_experiment_context`):
   - Gathers current image data
   - Extracts tracked particle positions
   - Provides SLM and DAC control handles
   - Exposes configuration objects

2. **Callback System**:
   - `_experiment_set_slm_points`: Scripts can update SLM traps instantly
   - `_experiment_set_dac_value`: Scripts can control DAC voltages
   - `_experiment_log`: Scripts can log to dashboard
   
3. **Frame Processing**:
   - Scripts are automatically called on each new image frame
   - Integrated into main update loop
   - No performance impact when no script is running

4. **UI Controls**:
   - Script selector dropdown
   - Start/Stop/Pause buttons
   - Reload scripts button
   - Real-time status display
   - Log message display

## Key Features

### 1. **Automatic Discovery**
- Scripts are automatically found in `ExperimentScripts/` directory
- No manual registration needed
- Hot-reload support via "Reload Scripts" button

### 2. **Full System Access**
Scripts can:
- Read current camera images
- Access tracked particle positions
- Set SLM trap positions (updates immediately on screen and hardware)
- Control DAC voltages
- Read analog inputs
- Access all configuration objects

### 3. **SLM Integration**
- Scripts call `ctx.set_slm_points([(x, y, z, intensity), ...])`
- Updates are immediate and automatic
- GUI displays updated trap positions in real-time
- Uses current SLM configuration (affine transform, apodization, etc.)
- Respects feature settings (z-focus, default point Z, etc.)

### 4. **Flexible Control**
- Scripts control their own execution via return values
- Can run indefinitely or for specific durations
- Support for pause/resume
- Error handling with recovery options

### 5. **State Machine Support**
- Demo script shows clean state machine implementation
- Easy to implement complex multi-step experiments

## Demo Experiment: Random Particle Displacement

**Location**: `ExperimentScripts/experiment1/random_displacement.py`

**Behavior**:
1. Randomly selects a tracked particle
2. Decides on random displacement (50-200 pixels)
3. Creates SLM trap at particle location
4. Smoothly moves trap to target position (10 pixels/frame)
5. Waits 4 seconds
6. Repeats

**Features Demonstrated**:
- Particle detection and selection
- State machine (SELECTING → MOVING → WAITING)
- Smooth SLM trap movement
- Time-based delays
- Logging and status updates
- Error recovery

## Usage

### Running from Dashboard GUI

1. Launch dashboard: `python dashboard_gui.py`
2. Look for "EXPERIMENT SCRIPTS" window (left column)
3. Select "Random Particle Displacement" from dropdown
4. Click "Start" button
5. Script will automatically:
   - Wait for particles to be tracked
   - Select random particles and move them
   - Display status and log messages
6. Click "Stop" to end experiment
7. Click "Pause" to temporarily suspend (maintains state)

### Creating New Experiments

1. Create a new file in `ExperimentScripts/` (e.g., `my_experiment.py`)
2. Import base classes:
   ```python
   from base_script import ExperimentScript, ExperimentContext
   ```
3. Define your experiment class:
   ```python
   class MyExperiment(ExperimentScript):
       def __init__(self):
           super().__init__()
           self.name = "My Experiment"
           self.description = "Does something cool"
       
       def setup(self, ctx: ExperimentContext) -> bool:
           # Initialize
           return True
       
       def on_frame(self, ctx: ExperimentContext) -> bool:
           # Your logic here
           return True  # Continue running
       
       def teardown(self, ctx: ExperimentContext) -> None:
           # Cleanup
           pass
   ```
4. Save the file
5. Click "Reload Scripts" in GUI
6. Your experiment appears in the dropdown!

## API Reference

### ExperimentContext

**Data Access**:
- `ctx.current_image` - Raw image numpy array
- `ctx.tracked_positions` - List of `(x, y, mass)` tuples
- `ctx.frame_number` - Current frame index
- `ctx.timestamp` - Current time (seconds since epoch)
- `ctx.slm_points` - Current SLM trap positions
- `ctx.dac_states` - Current DAC voltages
- `ctx.analog_states` - Current analog inputs

**Control Methods**:
- `ctx.set_slm_points(points)` - Update SLM traps
  - Format: `[(x, y, z, intensity), ...]`
  - Updates immediately on screen and hardware
- `ctx.set_dac_value(channel, value)` - Set DAC voltage
- `ctx.get_particle_at(x, y, radius)` - Find particle near coordinates
- `ctx.log(message, level)` - Log message to dashboard

**Configuration**:
- `ctx.slm_config` - Current SLM configuration
- `ctx.tracking_config` - Current tracking parameters
- `ctx.feature_config` - Current feature settings

### Script Lifecycle

1. **Setup**: `setup(ctx) -> bool`
   - Called once when script starts
   - Return `True` to continue, `False` to abort
   - Initialize state, validate conditions, etc.

2. **Frame Processing**: `on_frame(ctx) -> bool`
   - Called for each new camera frame
   - Return `True` to continue, `False` to stop
   - Main experiment logic goes here

3. **Teardown**: `teardown(ctx) -> None`
   - Called once when script stops (either completed or stopped)
   - Clean up resources, save data, reset hardware, etc.

4. **Optional Callbacks**:
   - `on_pause(ctx)` - Called when paused
   - `on_resume(ctx)` - Called when resumed
   - `on_error(ctx, error) -> bool` - Handle errors

## Files Created/Modified

### New Files
1. `ExperimentScripts/__init__.py` - Package initialization
2. `ExperimentScripts/base_script.py` - Base classes (230 lines)
3. `ExperimentScripts/script_manager.py` - Manager implementation (245 lines)
4. `ExperimentScripts/experiment1/__init__.py` - Experiment 1 package
5. `ExperimentScripts/experiment1/random_displacement.py` - Demo experiment (248 lines)
6. `ExperimentScripts/README.md` - Comprehensive documentation (450 lines)

### Modified Files
1. `GUI/dashboard_gui.py` - Integrated experiment system (~150 lines added)
   - Added imports for experiment scripts
   - Added script manager initialization
   - Added context building and callbacks
   - Added UI elements for script control
   - Integrated into update loop
   - Added window positioning

## Benefits

1. **No Dashboard Modifications Needed**: Write experiments, drop them in folder, done
2. **Full Flexibility**: Complete access to all hardware and state
3. **Immediate Feedback**: SLM updates show on screen instantly
4. **Easy Debugging**: Logging and status display built-in
5. **Reusable**: Share experiment scripts as single files
6. **Safe**: Experiments can't break the dashboard (error handling)
7. **GUI Remains Authoritative**: SLM parameters from GUI control display and hardware

## Future Enhancements

Potential additions:
1. Experiment parameter configuration UI
2. Data export utilities (CSV, HDF5, etc.)
3. Plotting capabilities for real-time analysis
4. Multi-script sequencing
5. Script templates/wizard
6. Performance profiling tools

## Testing

To test the implementation:

1. Start the dashboard
2. Ensure camera and tracking are working (particles visible)
3. Select "Random Particle Displacement" script
4. Click "Start"
5. Observe:
   - Status updates in experiment window
   - Log messages appearing
   - SLM traps moving particles on screen
   - Particles being displaced

Expected behavior: Script will randomly select and move particles every ~4 seconds indefinitely until stopped.

## Notes

- Type checker warnings about `latest_features` and `frame_sequence` are expected - they're guarded by `hasattr()` checks
- Script execution happens in main GUI thread (no threading issues)
- Scripts are called only when new frames arrive (no polling overhead)
- SLM updates use existing debouncing (30 FPS max send rate)

## Summary

The experiment script system provides a powerful, flexible framework for automating particle manipulation experiments without modifying the core dashboard code. The demo experiment showcases all major features and serves as a template for creating new experiments.
