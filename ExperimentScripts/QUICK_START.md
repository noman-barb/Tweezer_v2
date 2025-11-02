# Quick Start: Creating Your First Experiment

## 1. Create the Script File

Create a new file: `ExperimentScripts/my_first_experiment.py`

```python
from base_script import ExperimentScript, ExperimentContext
import time

class MyFirstExperiment(ExperimentScript):
    """My first experiment - creates a trap at each particle"""
    
    def __init__(self):
        super().__init__()
        self.name = "My First Experiment"
        self.description = "Creates an SLM trap at each tracked particle"
        self.last_update = 0.0
        
    def setup(self, ctx: ExperimentContext) -> bool:
        """Initialize the experiment"""
        ctx.log("Starting my first experiment!")
        self.last_update = time.time()
        return True
    
    def on_frame(self, ctx: ExperimentContext) -> bool:
        """Process each frame"""
        # Update every 100ms to avoid overwhelming the system
        if time.time() - self.last_update < 0.1:
            return True
        self.last_update = time.time()
        
        # Check if we have any particles
        if not ctx.tracked_positions:
            ctx.log("Waiting for particles...", "DEBUG")
            return True
        
        # Create a trap at each particle location
        traps = []
        for x, y, mass in ctx.tracked_positions:
            traps.append((x, y, 0.0, 0.9))  # x, y, z=0, intensity=0.9
        
        ctx.set_slm_points(traps)
        ctx.log(f"Created {len(traps)} traps", "DEBUG")
        
        return True  # Keep running
    
    def teardown(self, ctx: ExperimentContext) -> None:
        """Clean up when stopped"""
        ctx.log("Experiment stopped!")
        ctx.set_slm_points([])  # Clear all traps
```

## 2. Run Your Experiment

1. Open the Dashboard GUI
2. Find the "EXPERIMENT SCRIPTS" window (left side)
3. Click "Reload Scripts" button
4. Select "My First Experiment" from dropdown
5. Click "Start"
6. Watch as traps appear at each particle!
7. Click "Stop" when done

## Common Patterns

### Pattern 1: Run for N Frames

```python
def setup(self, ctx):
    self.frames_to_run = 100
    return True

def on_frame(self, ctx):
    self.frames_to_run -= 1
    # Your logic here
    if self.frames_to_run <= 0:
        ctx.log("Finished!")
        return False  # Stop
    return True  # Continue
```

### Pattern 2: Wait for a Duration

```python
import time

def setup(self, ctx):
    self.start_time = time.time()
    self.duration = 10.0  # seconds
    return True

def on_frame(self, ctx):
    elapsed = time.time() - self.start_time
    # Your logic here
    if elapsed >= self.duration:
        ctx.log("Timeout reached!")
        return False  # Stop
    return True  # Continue
```

### Pattern 3: State Machine

```python
from enum import Enum

class State(Enum):
    INIT = 1
    RUNNING = 2
    CLEANUP = 3

def setup(self, ctx):
    self.state = State.INIT
    return True

def on_frame(self, ctx):
    if self.state == State.INIT:
        # Initialization logic
        self.state = State.RUNNING
    elif self.state == State.RUNNING:
        # Main logic
        if some_condition:
            self.state = State.CLEANUP
    elif self.state == State.CLEANUP:
        # Cleanup logic
        return False  # Stop
    return True  # Continue
```

### Pattern 4: Find Specific Particle

```python
def on_frame(self, ctx):
    # Find particle near center of image
    particle = ctx.get_particle_at(x=960, y=540, radius=50)
    
    if particle:
        ctx.log(f"Found particle with mass {particle['mass']:.1f}")
        # Do something with it
        ctx.set_slm_points([(particle['x'], particle['y'], 0.0, 0.9)])
    else:
        ctx.log("No particle found near center", "DEBUG")
    
    return True
```

### Pattern 5: Move Trap Smoothly

```python
def setup(self, ctx):
    self.target_x = 1000
    self.target_y = 500
    self.current_x = 500
    self.current_y = 500
    self.speed = 5.0  # pixels per frame
    return True

def on_frame(self, ctx):
    import math
    
    # Calculate distance to target
    dx = self.target_x - self.current_x
    dy = self.target_y - self.current_y
    distance = math.sqrt(dx**2 + dy**2)
    
    if distance < 1.0:
        ctx.log("Reached target!")
        return False  # Stop
    
    # Move toward target
    step = min(self.speed, distance)
    self.current_x += (dx / distance) * step
    self.current_y += (dy / distance) * step
    
    # Update trap position
    ctx.set_slm_points([(self.current_x, self.current_y, 0.0, 0.9)])
    
    return True
```

### Pattern 6: Control DAC Voltage

```python
def on_frame(self, ctx):
    # Turn on voltage when particles detected
    if len(ctx.tracked_positions) > 5:
        ctx.set_dac_value('vdc_1', 5.0)  # 5V
        ctx.log("High particle count - DAC ON")
    else:
        ctx.set_dac_value('vdc_1', 0.0)  # 0V
        ctx.log("Low particle count - DAC OFF")
    
    return True
```

### Pattern 7: Save Data

```python
import csv

def setup(self, ctx):
    self.data_file = open('experiment_data.csv', 'w', newline='')
    self.writer = csv.writer(self.data_file)
    self.writer.writerow(['frame', 'time', 'num_particles', 'avg_mass'])
    return True

def on_frame(self, ctx):
    if ctx.tracked_positions:
        avg_mass = sum(p[2] for p in ctx.tracked_positions) / len(ctx.tracked_positions)
    else:
        avg_mass = 0.0
    
    self.writer.writerow([
        ctx.frame_number,
        ctx.timestamp,
        len(ctx.tracked_positions),
        avg_mass
    ])
    
    return True

def teardown(self, ctx):
    self.data_file.close()
    ctx.log(f"Data saved to experiment_data.csv")
```

## Tips

1. **Always call `super().__init__()`** in your `__init__` method
2. **Return `True`** from `on_frame()` to continue, `False` to stop
3. **Clear traps** in `teardown()`: `ctx.set_slm_points([])`
4. **Use logging** liberally: `ctx.log("message", "INFO")`
5. **Check for particles** before using them: `if ctx.tracked_positions:`
6. **Handle errors** with try/except blocks
7. **Test incrementally** - start simple, add complexity gradually

## Debugging

If your script doesn't appear:
- Check filename doesn't start with `_`
- Ensure class inherits from `ExperimentScript`
- Click "Reload Scripts" in GUI
- Check for syntax errors

If script fails to start:
- Check `setup()` returns `True`
- Look at error messages in dashboard log
- Add logging to see what's happening

If script behaves oddly:
- Add `ctx.log()` calls to track state
- Check you're returning `True` from `on_frame()`
- Verify trap coordinates are in valid range (0-1920, 0-1152 for standard SLM)

## Next Steps

1. Try the demo experiment: "Random Particle Displacement"
2. Modify it to change behavior (wait time, distance, etc.)
3. Create your own experiment using the patterns above
4. Check `README.md` for detailed API documentation
5. Look at `experiment1/random_displacement.py` for a complete example

Happy experimenting!
