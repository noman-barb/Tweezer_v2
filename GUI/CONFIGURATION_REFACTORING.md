# Dashboard GUI Configuration Refactoring

## Overview
The dashboard GUI has been refactored to move all hardcoded configuration values to YAML files, making it easier to customize and manage UI preferences.

## Changes Made

### 1. Configuration Files

#### `services_config.yaml` - Updated with Dashboard Section
Added a comprehensive `dashboard` section with:
- **Viewport defaults**: Default window size (2560x1400)
- **Layout proportions**: Column width ratios for responsive layout
- **Image display defaults**: SLM dimensions, point intensity, selection radius
- **SLM display defaults**: Circle color, radius, thickness, debounce timing
- **Monitoring defaults**: History limits, moving average window, interval
- **UI dimensions**: Plot heights, window heights for different panels
- **Color scheme**: RGBA values for hardware, image, SLM sections, status colors
- **Affine fields**: List of affine transformation parameter names

Added to `global` section:
- `pin_config_path`: Path to Arduino pin configuration
- `experiment_scripts_dir`: Path to experiment scripts
- `monitoring_logs_dir`: Path for monitoring logs
- `experiment_logs_dir`: Path for experiment logs

#### `dashboard_config.yaml` - New File
Created comprehensive UI configuration system with:
- **Active configuration tracking**: Stores which config is currently active
- **Multiple configuration profiles**: Save/load different UI layouts
- **Viewport settings**: Window size and title
- **Window layout**: Position, size, and collapsed state for all 12 windows
- **Image display settings**: Display mode, zoom, tile grid, colormap settings
- **SLM visualization**: Circle color, size, thickness
- **Hardware monitoring**: History limits, channel visibility
- **Image metrics**: History limits, metric visibility
- **Storage settings**: Auto-save options, HDF5, target FPS
- **Monitoring settings**: Interval, auto-start
- **Experiment settings**: All experiment script parameters
- **Theme colors**: Customizable color scheme

### 2. Code Changes

#### New Classes
- **`DashboardConfig`**: Dataclass for UI configuration with serialization
- **`DashboardConfigManager`**: Manages loading/saving/deleting UI configs
  - Auto-creates default configuration if file doesn't exist
  - Supports multiple named configurations
  - Tracks active configuration
  - Prevents deletion of default configuration

#### Updated `AggregateUI` Dataclass
Added fields for:
- UI config management controls (combo, buttons)
- Window tags for all 12 windows (for layout management)

#### Updated `AggregateControllerStreaming`
Added methods:
- `save_ui_config(name, description)`: Captures current UI state
- `load_ui_config(name)`: Restores saved UI state
- `set_default_ui_config(name)`: Sets default configuration
- `delete_ui_config(name)`: Deletes a configuration
- `list_ui_configs()`: Lists all configurations
- `get_current_ui_config_name()`: Returns active config name
- Helper methods for experiment params and theme colors

Added `dashboard_config_manager` parameter to `__init__`
Added `saved_window_layout` for storing loaded window positions

#### New UI Controls
Added "UI CONFIGURATION" window with:
- Dropdown to select configurations
- **Save** button: Captures current window positions, sizes, all settings
- **Load** button: Restores saved configuration
- **Set Default** button: Makes configuration auto-load on startup
- **Delete** button: Removes configuration (except default)
- Help text explaining functionality

#### Callback Functions
Added callbacks matching SLM/tracking config pattern:
- `_on_ui_config_save()`: Opens save dialog
- `_confirm_ui_config_save()`: Handles save confirmation
- `_on_ui_config_load()`: Loads selected config
- `_on_ui_config_set_default()`: Sets default
- `_on_ui_config_delete()`: Opens delete confirmation
- `_confirm_ui_config_delete()`: Handles delete confirmation

#### Configuration Loading
Updated constants to load from `services_config.yaml`:
- All color values (HARDWARE_COLOR, IMAGE_COLOR, etc.)
- All dimension values (PLOT_HEIGHT, etc.)
- SLM defaults (width, height, intensity, etc.)
- Monitoring defaults (history, moving average window)
- Affine field list

Added `_load_dashboard_defaults()` function to load config with fallbacks

#### Main Function Updates
- Loads `services_config.yaml` to get paths
- Uses `pin_config_path` from global config
- Creates `DashboardConfigManager` instance
- Passes it to controller initialization

### 3. Layout Management

Updated viewport resize callback to include new UI config window:
- Adjusted spacing for 4 windows in left column
- UI Config window positioned between Connections and Monitoring
- Maintains responsive layout on window resize

## Usage

### For End Users

1. **Save Current Layout**:
   - Arrange windows as desired
   - Set all preferences (colors, sizes, display modes, etc.)
   - Click "Save" in UI Configuration window
   - Enter a name (e.g., "my_layout")
   - Configuration is saved to `dashboard_config.yaml`

2. **Load Saved Layout**:
   - Select configuration from dropdown
   - Click "Load"
   - All window positions, sizes, and settings are restored

3. **Set Default**:
   - Select preferred configuration
   - Click "Set Default"
   - This configuration will auto-load on next startup

4. **Delete Configuration**:
   - Select configuration to delete
   - Click "Delete"
   - Confirm deletion (cannot delete "default")

### For Developers

1. **Modify Default Values**:
   - Edit `services_config.yaml` → `dashboard` section
   - Changes apply to all new instances
   - Existing configs unaffected

2. **Add New Settings**:
   - Add to `DashboardConfig` dataclass
   - Update `save_ui_config()` to capture value
   - Update `load_ui_config()` to apply value
   - Add to default config in `DashboardConfigManager._create_default_config()`

3. **Change Paths**:
   - Edit `services_config.yaml` → `global` section
   - Update paths for pin_config, logs, scripts, etc.

## Configuration Files Structure

```
GUI/
├── services_config.yaml       # Service endpoints + dashboard defaults
├── dashboard_config.yaml      # UI layout configurations (auto-created)
├── slm_config/               # SLM affine configurations
├── slm_feature_config/       # SLM feature configurations
└── dashboard_gui.py          # Main dashboard code
```

## Benefits

1. **No More Hardcoded Values**: All constants in YAML files
2. **Easy Customization**: Edit YAML without touching code
3. **Multiple Profiles**: Save different layouts for different tasks
4. **Persistent Settings**: Window positions and all preferences saved
5. **Default Configuration**: Always have a working baseline
6. **Portable Configs**: Share configurations between setups
7. **Version Control Friendly**: Config changes tracked in Git

## Backward Compatibility

- If config files don't exist, they are auto-created with defaults
- If loading fails, hardcoded fallbacks are used
- Existing functionality unchanged, just made configurable
- Old pin_config.json path still works if not overridden

## Future Enhancements

Possible additions:
- Export/import configurations as separate files
- Configuration templates for different experiment types
- Keyboard shortcuts for switching configurations
- Configuration comparison tool
- Auto-backup of configurations
