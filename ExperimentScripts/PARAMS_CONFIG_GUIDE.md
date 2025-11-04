# Experiment Parameters Configuration Guide

This guide explains how to save and load experiment script parameters in the Dashboard GUI.

## Overview

The experiment parameters configuration system allows you to:
- Save current parameter values for each experiment script
- Load previously saved parameter configurations
- Set default parameter configurations that auto-load when selecting a script
- Manage multiple parameter presets for different experimental conditions

## Location

Parameter configurations are saved in the ExperimentScripts folder structure:
```
ExperimentScripts/
├── <ScriptName>/
│   └── params/
│       ├── config1.yaml
│       ├── config2.yaml
│       └── ...
└── params_defaults.yaml
```

Each script gets its own `params` subdirectory for storing parameter presets as YAML files.

## Using the Dashboard GUI

### Saving Parameters

1. Select your experiment script from the dropdown
2. Adjust the script parameters to your desired values
3. Open the "Parameters Config" collapsible section
4. Click the **Save** button
5. Enter a configuration name (e.g., "High Power", "Test Run", "Experiment 1")
6. Optionally add a description
7. Click **Save** to confirm

### Loading Parameters

1. Select your experiment script from the dropdown
2. Open the "Parameters Config" collapsible section
3. Select a saved configuration from the dropdown
4. Click the **Load** button

The parameters will be applied to the UI and to the running script (if active).

### Setting a Default Configuration

To automatically load a parameter configuration when selecting a script:

1. Select the configuration from the dropdown
2. Click **Set Default**

Now, whenever you select this script, the default parameters will load automatically.

### Deleting Configurations

1. Select the configuration from the dropdown
2. Click **Delete**
3. Confirm the deletion

**Warning:** This action cannot be undone!

## Parameter Configuration File Format

Configurations are stored as YAML files with the following structure:

```yaml
script_name: "Random Particle Displacement (Auto-Config)"
description: "High power settings for large displacements"
created_at: "2025-11-03T10:30:00.000000+00:00"
updated_at: "2025-11-03T15:45:00.000000+00:00"
params:
  movement_duration_min: 0.5
  movement_duration_max: 4.0
  move_distance_min: 20.0
  move_distance_max: 32.0
  wait_time_min: 1.0
  wait_time_max: 3.0
  max_moves: 10
  # ... other parameters
```

## Programmatic Access

You can also access parameter configurations programmatically:

```python
from ExperimentScripts.experiment_params_manager import ExperimentParamsManager
from pathlib import Path

# Initialize manager
scripts_dir = Path("ExperimentScripts")
manager = ExperimentParamsManager(scripts_dir)

# Save configuration
params = {
    'movement_duration_min': 0.5,
    'movement_duration_max': 4.0,
    # ... other params
}
manager.save_config(
    script_name="My Script",
    config_name="config1",
    params=params,
    description="Test configuration"
)

# Load configuration
config = manager.load_config("My Script", "config1")
if config:
    print(config.params)

# List all configurations for a script
configs = manager.list_configs("My Script")

# Set/get default
manager.set_default_config("My Script", "config1")
default = manager.get_default_config("My Script")
```

## Best Practices

1. **Use Descriptive Names**: Give configurations meaningful names that describe their purpose
   - Good: "High_Power_Large_Displacement", "Test_Small_Moves"
   - Bad: "config1", "test", "new"

2. **Add Descriptions**: Use the description field to document:
   - What experimental conditions this is for
   - Why these specific values were chosen
   - Any special considerations

3. **Version Control**: Consider committing parameter configurations to git:
   - They're human-readable YAML files
   - Easy to track changes over time
   - Can be shared with collaborators

4. **Organize by Experiment**: Create separate configurations for different experimental phases:
   - "Calibration"
   - "Experiment_1_Trial_A"
   - "Final_Run"

5. **Set Defaults**: Always set a default configuration for frequently used scripts
   - Saves time when starting new sessions
   - Ensures consistent starting parameters

## Troubleshooting

### Configuration not appearing in dropdown
- Ensure the script name matches exactly (case-sensitive)
- Check that the YAML file is valid
- Try clicking "Reload Scripts" in the experiment control panel

### Parameters not loading
- Verify the parameter names in the YAML match the script's parameter specs
- Check the experiment log for error messages
- Ensure the script supports auto-configuration (has `ParamSpec` definitions)

### Can't save configuration
- Ensure you have write permissions to the ExperimentScripts folder
- Check that the configuration name doesn't contain invalid characters
- Look for error messages in the application log

## Integration with Scripts

For a script to support parameter configurations, it must use the auto-configuration system:

```python
class MyExperiment(ExperimentScript):
    def __init__(self):
        super().__init__()
        self.name = "My Experiment"
        
        # Register parameters with ParamSpec
        self.register_param(ParamSpec(
            name='my_param',
            label='My Parameter',
            param_type=float,
            default=1.0,
            min_value=0.0,
            max_value=10.0,
            description='Controls something important'
        ))
```

See `AUTO_CONFIG_GUIDE.md` for more details on creating auto-configured scripts.
