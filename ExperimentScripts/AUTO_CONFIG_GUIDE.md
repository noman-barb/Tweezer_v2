# Auto-Configuration System for Experiment Scripts

## Problem

Previously, to make an experiment parameter configurable in the dashboard, you had to:
1. Define it in the experiment script
2. Add a UI control in `dashboard_gui.py`
3. Add it to the `AggregateUI` dataclass
4. Update `_get_current_experiment_params()`
5. Update `_apply_experiment_params()`
6. Update `_on_experiment_params_apply()` callback
7. Pass it through the UI constructor

This was **error-prone, tedious, and hard to maintain**.

## Solution: Parameter Specifications (`ParamSpec`)

Now you define parameters **once** in your experiment script with rich metadata, and the dashboard automatically:
- Generates appropriate UI controls
- Handles saving/loading
- Validates values
- Organizes parameters by category

## How to Use

### 1. Define Parameters in Your Experiment Script

```python
from base_script import ExperimentScript, ExperimentContext, ParamSpec

class MyExperiment(ExperimentScript):
    def __init__(self):
        super().__init__()
        self.name = "My Experiment"
        
        # Register configurable parameters
        self.register_param(ParamSpec(
            name='movement_speed',           # Attribute name on self
            label='Movement Speed',          # Display name in UI
            param_type=float,                # Python type
            default=10.0,                    # Default value
            min_value=1.0,                   # Minimum (optional)
            max_value=100.0,                 # Maximum (optional)
            step=1.0,                        # Step size (optional)
            unit='px/s',                     # Unit label (optional)
            category='Movement',             # Group in UI (optional)
            description='Speed of particle movement',  # Tooltip (optional)
            format_str='%.1f'                # Display format (optional)
        ))
        
        self.register_param(ParamSpec(
            name='enable_logging',
            label='Enable Logging',
            param_type=bool,
            default=True,
            category='Logging',
            description='Enable CSV logging of actions'
        ))
```

### 2. Use Parameters in Your Code

```python
def on_frame(self, ctx: ExperimentContext) -> bool:
    # Just use them like normal attributes!
    speed = self.movement_speed
    
    if self.enable_logging:
        self._log_action()
    
    return True
```

### 3. That's It!

The dashboard will:
- ✅ Automatically generate UI controls with appropriate input types
- ✅ Group parameters by category in collapsible sections
- ✅ Show units and tooltips
- ✅ Validate min/max constraints
- ✅ Save/load values with UI configurations
- ✅ Apply changes to running experiments

## `ParamSpec` Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | str | ✅ | Attribute name on the script object |
| `label` | str | ✅ | Display name in UI |
| `param_type` | type | ✅ | Python type: `float`, `int`, `bool`, `str`, or `tuple` |
| `default` | Any | ✅ | Default value (must match type) |
| `min_value` | float/int | ❌ | Minimum value (numeric types only) |
| `max_value` | float/int | ❌ | Maximum value (numeric types only) |
| `step` | float/int | ❌ | Step increment for sliders/spinners |
| `unit` | str | ❌ | Unit label (e.g., 'px', 's', 'Hz') |
| `category` | str | ❌ | Group name for organizing parameters (default: "General") |
| `description` | str | ❌ | Tooltip/help text |
| `format_str` | str | ❌ | Printf-style format string (default: "%.2f") |

## Parameter Types

### Float
```python
self.register_param(ParamSpec(
    name='threshold',
    label='Detection Threshold',
    param_type=float,
    default=5.0,
    min_value=0.1,
    max_value=50.0,
    step=0.5,
    format_str='%.1f'
))
```
**Generates:** Float input with spinner, clamped to min/max

### Integer
```python
self.register_param(ParamSpec(
    name='frame_interval',
    label='Frame Interval',
    param_type=int,
    default=30,
    min_value=1,
    max_value=300,
    step=5
))
```
**Generates:** Integer input with spinner

### Boolean
```python
self.register_param(ParamSpec(
    name='use_filtering',
    label='Enable Filtering',
    param_type=bool,
    default=True,
    description='Apply Gaussian filter to images'
))
```
**Generates:** Checkbox

### Tuple (Range Parameters)
```python
self.register_param(ParamSpec(
    name='distance_range',
    label='Distance Range',
    param_type=tuple,
    default=(20.0, 50.0),
    min_value=0.0,
    max_value=200.0,
    unit='px'
))
```
**Generates:** Two inputs labeled "Min" and "Max"

## Categories

Parameters are automatically grouped by their `category` field in collapsible sections:

```python
# Movement category
self.register_param(ParamSpec(
    name='speed',
    label='Speed',
    param_type=float,
    default=10.0,
    category='Movement'
))

self.register_param(ParamSpec(
    name='acceleration',
    label='Acceleration',
    param_type=float,
    default=2.0,
    category='Movement'
))

# Logging category
self.register_param(ParamSpec(
    name='log_interval',
    label='Log Interval',
    param_type=int,
    default=100,
    category='Logging'
))
```

**UI Result:**
```
┌─ Movement ────────────────┐
│ Speed:         [10.0]      │
│ Acceleration:  [2.0]       │
└────────────────────────────┘

┌─ Logging ─────────────────┐
│ Log Interval:  [100]       │
└────────────────────────────┘
```

## Advanced: Programmatic Access

```python
# Get all parameter specs
specs = script.get_param_specs()

# Get single value
value = script.get_param_value('movement_speed')

# Set single value
script.set_param_value('movement_speed', 25.0)

# Get all values as dict
all_values = script.get_all_param_values()
# Returns: {'movement_speed': 25.0, 'enable_logging': True, ...}

# Set multiple values
script.set_params_from_dict({
    'movement_speed': 30.0,
    'enable_logging': False
})
```

## Example: Complete Script

See `random_displacement_v2.py` for a complete example that demonstrates:
- 13 different configurable parameters
- Multiple categories (Movement, Timing, Constraints, SLM, Performance, Logging)
- Mix of float, int types
- Range parameters (tuples)
- All parameters accessible via dashboard UI
- Automatic saving/loading with UI configurations

## Migration Guide

### Old Way (Manual):
```python
# In experiment script:
self.movement_speed = 10.0

# In dashboard_gui.py (multiple files):
# 1. Add to AggregateUI dataclass
experiment_movement_speed: Optional[int] = None

# 2. Create UI control
experiment_movement_speed = dpg.add_input_float(...)

# 3. Add to _get_current_experiment_params()
if self.ui.experiment_movement_speed:
    params['movement_speed'] = dpg.get_value(...)

# 4. Add to _apply_experiment_params()
if 'movement_speed' in params:
    dpg.set_value(...)

# 5. Add to _on_experiment_params_apply()
if hasattr(script, 'movement_speed'):
    script.movement_speed = dpg.get_value(...)
```

### New Way (Auto-Config):
```python
# In experiment script only:
self.register_param(ParamSpec(
    name='movement_speed',
    label='Movement Speed',
    param_type=float,
    default=10.0,
    min_value=1.0,
    max_value=100.0,
    unit='px/s'
))
```

**Done!** Everything else is automatic.

## Benefits

✅ **Single Source of Truth** - Parameters defined once  
✅ **Type Safety** - Explicit type declarations  
✅ **Automatic Validation** - Min/max constraints enforced  
✅ **Self-Documenting** - Rich metadata (units, descriptions, categories)  
✅ **UI Consistency** - Automatic, uniform controls  
✅ **Save/Load** - Works with configuration system  
✅ **Maintainable** - No dashboard code changes needed  
✅ **Discoverable** - Dashboard can introspect parameters  

## Future Enhancements

Potential additions:
- **Enum parameters** - Dropdown selections
- **Color parameters** - Color pickers
- **File path parameters** - File dialogs
- **Array parameters** - Multi-value inputs
- **Dynamic parameters** - Change available params at runtime
- **Parameter dependencies** - Show/hide based on other values
- **Validation callbacks** - Custom validation logic
- **Parameter templates** - Reusable parameter sets

## Notes

- Lint errors about unknown attributes are false positives (type checker limitation)
- Parameters are set automatically when `register_param()` is called
- All registered parameters are accessible as instance attributes
- Changes via dashboard apply immediately to running scripts
- Old manual parameter system still works (backwards compatible)
