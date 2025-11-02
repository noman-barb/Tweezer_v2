# Tweezer Experiment Scripts

This directory contains automated experiment scripts for the Tweezer Dashboard GUI.

## Overview

Experiment scripts provide a flexible framework for automating particle manipulation experiments, similar to backtesting frameworks in algorithmic trading. Scripts have access to:

- **Real-time image data** - Raw camera frames
- **Particle tracking** - Positions and properties of tracked particles
- **SLM control** - Create and update holographic traps
- **DAC/Arduino control** - Voltage outputs and analog inputs
- **Configuration** - SLM, tracking, and feature settings

## Quick Start

1. **Create a new experiment** by subclassing `ExperimentScript`
2. **Implement required methods**: `setup()`, `on_frame()`, `teardown()`
3. **Save the file** in this directory or a subdirectory
4. **Launch the Dashboard GUI** - your script will be automatically discovered
5. **Select and run** your experiment from the GUI

## Example: Minimal Experiment

```python
from base_script import ExperimentScript, ExperimentContext

class MyExperiment(ExperimentScript):
    def __init__(self):
        super().__init__()
        self.name = "My First Experiment"
        self.description = "A simple test experiment"
    
    def setup(self, ctx: ExperimentContext) -> bool:
        ctx.log("Starting experiment")
        return True  # Return False to abort
    
    def on_frame(self, ctx: ExperimentContext) -> bool:
        # Your experiment logic here
        # Called on each new camera frame
        
        # Example: Create a trap at the first particle
        if ctx.tracked_positions:
            x, y, mass = ctx.tracked_positions[0]
            ctx.set_slm_points([(x, y, 0.0, 0.9)])
        
        return True  # Return False to stop
    
    def teardown(self, ctx: ExperimentContext) -> None:
        ctx.log("Experiment complete")
        ctx.set_slm_points([])  # Clear traps
```

## Available Experiments

### Experiment 1: Random Particle Displacement
**File**: `experiment1/random_displacement.py`

Demonstrates automated particle manipulation:
- Randomly selects a tracked particle
- Chooses a random displacement (50-200 pixels)
- Creates SLM trap and moves particle to target
- Waits 4 seconds
- Repeats

This showcases:
- Particle detection and selection
- SLM trap control with smooth movement
- State machine implementation
- Time-based sequencing

## ExperimentContext API

The `ExperimentContext` object provides access to all dashboard functionality:

### Reading Data

```python
# Current camera frame (numpy array)
image = ctx.current_image

# Tracked particle positions: [(x, y, mass), ...]
particles = ctx.tracked_positions

# Frame number and timestamp
frame_num = ctx.frame_number
timestamp = ctx.timestamp

# Current SLM trap positions
traps = ctx.slm_points  # [(x, y, z, intensity), ...]

# DAC channel states
voltage = ctx.dac_states['vdc_1']
```

### Controlling Hardware

```python
# Set SLM trap positions
ctx.set_slm_points([
    (x1, y1, z1, intensity1),  # Trap 1
    (x2, y2, z2, intensity2),  # Trap 2
    # ...
])

# Set DAC voltage
ctx.set_dac_value('vdc_1', 5.0)

# Find particle near coordinates
particle = ctx.get_particle_at(x=500, y=500, radius=30)
if particle:
    print(f"Found particle: {particle['mass']}")
```

### Logging

```python
ctx.log("This is an info message", "INFO")
ctx.log("Warning message", "WARNING")
ctx.log("Error message", "ERROR")
ctx.log("Debug message", "DEBUG")
```

## Script Lifecycle

1. **Discovery**: Scripts are scanned when Dashboard starts or when "Reload Scripts" is clicked
2. **Setup**: `setup(ctx)` is called once when script starts
3. **Execution**: `on_frame(ctx)` is called for each new camera frame
4. **Teardown**: `teardown(ctx)` is called when script stops

### Optional Lifecycle Methods

```python
def on_pause(self, ctx: ExperimentContext) -> None:
    """Called when script is paused"""
    pass

def on_resume(self, ctx: ExperimentContext) -> None:
    """Called when script resumes"""
    pass

def on_error(self, ctx: ExperimentContext, error: Exception) -> bool:
    """Called on error. Return True to continue, False to stop"""
    return False
```

## Best Practices

### 1. State Management
Use state machines for complex experiments:

```python
from enum import Enum

class State(Enum):
    INIT = 1
    RUNNING = 2
    CLEANUP = 3

def on_frame(self, ctx):
    if self.state == State.INIT:
        # Initialization logic
        self.state = State.RUNNING
    elif self.state == State.RUNNING:
        # Main logic
        pass
```

### 2. Time-Based Actions
Track time for delays and timeouts:

```python
def setup(self, ctx):
    self.start_time = time.time()
    return True

def on_frame(self, ctx):
    elapsed = time.time() - self.start_time
    if elapsed > 10.0:  # 10 second timeout
        return False  # Stop
```

### 3. Particle Tracking
Use particle IDs or spatial tracking:

```python
def select_particle(self, ctx):
    # Find brightest particle
    if ctx.tracked_positions:
        brightest = max(ctx.tracked_positions, key=lambda p: p[2])
        return brightest
    return None
```

### 4. Error Handling
Implement robust error handling:

```python
def on_error(self, ctx, error):
    ctx.log(f"Error: {error}", "ERROR")
    
    if self.error_count < 3:
        return True  # Try to continue
    else:
        ctx.log("Too many errors, stopping", "ERROR")
        return False
```

### 5. Configuration
Make experiments configurable:

```python
def __init__(self):
    super().__init__()
    self.name = "Configurable Experiment"
    
    # Experiment parameters
    self.trap_intensity = 0.9
    self.wait_time = 5.0
    self.max_cycles = 100
```

## GUI Integration

Scripts are automatically discovered and can be controlled from the Dashboard GUI:

1. **Script Selector** - Dropdown showing all available experiments
2. **Start** - Begin experiment execution
3. **Stop** - Stop current experiment (calls `teardown()`)
4. **Pause/Resume** - Pause without stopping (optional callbacks)
5. **Reload Scripts** - Rescan directory for new/updated scripts
6. **Status Display** - Shows current script state and statistics

## SLM Updates

SLM updates made via `ctx.set_slm_points()` are:
- **Immediately sent** to the SLM hardware
- **Automatically displayed** in the GUI
- **Respect current SLM configuration** (affine transform, apodization, z-focus)
- **Use feature settings** from the GUI (default point Z, etc.)

The GUI remains the authoritative source for SLM parameters - scripts just specify trap positions.

## File Organization

```
ExperimentScripts/
├── __init__.py                 # Package init
├── base_script.py              # Base classes (ExperimentScript, ExperimentContext)
├── script_manager.py           # Script discovery and execution
├── README.md                   # This file
├── experiment1/                # Experiment 1
│   ├── __init__.py
│   └── random_displacement.py
├── experiment2/                # Your next experiment
│   ├── __init__.py
│   └── your_script.py
└── ...
```

## Advanced Features

### Multi-Particle Manipulation

```python
def on_frame(self, ctx):
    # Create trap for each particle
    traps = []
    for x, y, mass in ctx.tracked_positions:
        traps.append((x + 10, y + 10, 0.0, 0.9))  # Offset trap
    ctx.set_slm_points(traps)
```

### Conditional Control

```python
def on_frame(self, ctx):
    # Activate DAC based on particle count
    if len(ctx.tracked_positions) > 10:
        ctx.set_dac_value('vdc_1', 5.0)
    else:
        ctx.set_dac_value('vdc_1', 0.0)
```

### Data Logging

```python
def setup(self, ctx):
    self.data_file = open('experiment_data.csv', 'w')
    self.data_file.write('frame,particles,trap_x,trap_y\n')
    return True

def on_frame(self, ctx):
    # Log data
    if self.trap_position:
        self.data_file.write(
            f"{ctx.frame_number},{len(ctx.tracked_positions)},"
            f"{self.trap_position[0]},{self.trap_position[1]}\n"
        )

def teardown(self, ctx):
    self.data_file.close()
```

## Troubleshooting

### Script Not Appearing in GUI
- Check file doesn't start with underscore (`_`)
- Ensure class inherits from `ExperimentScript`
- Click "Reload Scripts" in GUI
- Check for syntax errors in script file

### Script Fails to Start
- Check `setup()` returns `True`
- Look for errors in GUI log window
- Verify imports are correct

### Unexpected Behavior
- Check `on_frame()` returns `True` to continue
- Verify state variables are initialized in `setup()`
- Add logging: `ctx.log("Debug info", "DEBUG")`

## Contributing

When creating new experiments:
1. Use descriptive names and docstrings
2. Add error handling with `on_error()`
3. Clean up in `teardown()` (clear traps, close files, etc.)
4. Test with and without particles present
5. Document parameters and behavior

## Examples Gallery

More example experiments coming soon:
- Grid scanning
- Particle sorting
- Multi-trap coordination
- Force measurement
- Feedback control

## Resources

- Base Script Source: `base_script.py`
- Script Manager Source: `script_manager.py`
- Dashboard Integration: `../GUI/dashboard_gui.py`
