# SLM Configuration System

This directory contains the SLM (Spatial Light Modulator) configuration management system for the Tweezer project.

## Overview

The SLM configuration system manages affine transformation parameters that map camera coordinates to SLM coordinates. These parameters are used for calibrating the hologram generation process.

## Files

- `slm_config_manager.py` - Main configuration manager class
- `default.json` - Default configuration with zero parameters
- `configs.json` - Metadata file (auto-generated)
- `*.json` - Individual configuration files

## Configuration Structure

Each configuration file contains:

### Legacy Parameters (currently used)
- `cam_x0`, `cam_y0`, `slm_x0`, `slm_y0` - First calibration point
- `cam_x1`, `cam_y1`, `slm_x1`, `slm_y1` - Second calibration point  
- `cam_x2`, `cam_y2`, `slm_x2`, `slm_y2` - Third calibration point

### Extended Parameters (for future use)
- `translate_x`, `translate_y`, `translate_z` - Translation parameters
- `rotate_x_deg`, `rotate_y_deg`, `rotate_z_deg` - Rotation parameters (degrees)
- `scale_x`, `scale_y`, `scale_z` - Scale parameters
- `shear_xy`, `shear_yz`, `shear_xz` - Shear parameters

### Metadata
- `name` - Configuration name
- `description` - Human-readable description

## Usage in Dashboard

The dashboard GUI provides controls for:

1. **Loading configurations** - Select from dropdown and click "Load"
2. **Saving configurations** - Click "Save" and provide name/description
3. **Setting as default** - Click "Set Default" to make the current config load on startup
4. **Resetting** - Click "Reset" to load the default (all zeros) configuration
5. **Deleting** - Click "Delete" to remove a configuration (cannot delete default)

## Configuration Files Location

The location is specified in `services_config.yaml` under:
```yaml
global:
  slm_config_dir: slm_config
```

This path is relative to the services directory.

## Python API

```python
from slm_config.slm_config_manager import SlmConfigManager, SlmConfig

# Initialize manager
manager = SlmConfigManager(Path("slm_config"))

# Create new configuration
config = manager.create_config("my_config", "My custom calibration")

# Load configuration
config = manager.get_config("my_config")
params = config.get_legacy_params()

# Set as current/default
manager.set_current_config("my_config")

# List all configurations
configs = manager.list_configs()
```

## Default Configuration

The system always maintains a "default" configuration with all parameters set to zero. This configuration cannot be deleted and serves as the fallback when resetting or when no other configuration is available.