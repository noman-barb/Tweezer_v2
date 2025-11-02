# Dashboard Window Layout Persistence

## Overview

The dashboard GUI now supports **complete window layout persistence**, allowing you to manually arrange your workspace and save/load configurations that restore everything exactly as you left it.

## What Gets Saved and Restored

### ✅ Currently Implemented (Comprehensive)

1. **Window Positions** - X and Y coordinates of each window
2. **Window Sizes** - Width and height of each window
3. **Window Visibility** - Whether windows are shown or hidden
4. **Window Collapsed State** - Whether windows are collapsed or expanded
5. **Scroll Positions** - Vertical and horizontal scroll offsets for scrollable windows
6. **Docking Layout** - Complete DearPyGui docking structure including:
   - Which windows are docked together
   - Tab groups and their active tabs
   - Split ratios between docked windows
   - Parent-child docking relationships
   - Dock node hierarchy
7. **Viewport Settings** - Window dimensions and title

### Additional Settings Saved

Beyond layout, the configuration also saves:

- **Image Display Settings**: Display mode, zoom, tile grid, colormap preferences
- **SLM Visualization**: Circle color, size, thickness for point markers
- **Hardware Monitoring**: History limits, visible channels
- **Image Metrics**: History limits, visible plots
- **Storage Settings**: Auto-save preferences, FPS targets
- **Monitoring**: Monitoring interval configuration
- **Experiment Parameters**: All experiment script parameters
- **Theme Colors**: UI color scheme preferences

## How to Use

### Saving Your Layout

1. **Manually arrange your windows** exactly how you want them:
   - Move windows to desired positions
   - Resize them to your preferred dimensions
   - Dock windows together by dragging tabs
   - Set up splits and tab groups
   - Scroll to desired positions in scrollable windows
   - Collapse/expand windows as needed

2. **Save the configuration**:
   - Click the "Save Config" button in the UI Configuration window
   - Enter a descriptive name (e.g., "My Layout", "Experiment Mode", "Monitoring Setup")
   - Optionally add a description
   - Click "Confirm"

### Loading Your Layout

1. **Select your saved configuration** from the dropdown in UI Configuration window
2. **Click "Load Config"**
3. All windows will automatically restore to their saved:
   - Positions
   - Sizes
   - Docking arrangements
   - Scroll positions
   - Visibility and collapsed states

### Setting a Default Layout

1. **Load or create** your preferred layout
2. **Click "Set as Default"** in the UI Configuration window
3. This layout will be automatically loaded when you start the dashboard

## Technical Implementation

### DearPyGui Docking State

The system uses DearPyGui's built-in `dpg.save_init_file()` and `dpg.configure_app(init_file=...)` functions to capture and restore the complete docking layout. This is stored as an INI file format string within the YAML configuration.

### Window-Specific Properties

Individual window properties are captured using:
```python
dpg.get_item_pos(tag)           # Position
dpg.get_item_rect_size(tag)     # Size
dpg.get_item_configuration(tag) # Collapsed and visibility states
dpg.get_x_scroll(tag)           # Horizontal scroll position
dpg.get_y_scroll(tag)           # Vertical scroll position
```

And restored using:
```python
dpg.set_item_pos(tag, [x, y])
dpg.configure_item(tag, width=w, height=h, collapsed=state, show=visibility)
dpg.set_x_scroll(tag, x)
dpg.set_y_scroll(tag, y)
```

### Configuration Storage

Configurations are stored in YAML format at:
```
GUI/dashboard_config.yaml
```

Docking layouts are temporarily written to:
```
GUI/.layout_{config_name}.ini
```

## Windows Tracked

The following windows have their layout saved:

1. System Connections
2. UI Configuration
3. Metrics Monitor
4. Experiment Scripts
5. Image Viewer
6. Image Display Controls
7. Image Saving & Capture
8. Environment & DAC Control
9. Hardware Monitoring
10. Image Server Metrics
11. SLM Server Metrics
12. Tracking Parameters
13. SLM Point Control

## Example Use Cases

### Use Case 1: Experiment Mode
- Image viewer maximized on left
- SLM control and tracking parameters docked together on right
- Hardware monitoring collapsed at bottom
- **Save as**: "Experiment Mode"

### Use Case 2: Monitoring Mode
- All metric windows expanded and visible
- Multiple plots side-by-side
- Image viewer minimized
- **Save as**: "Monitoring Mode"

### Use Case 3: Setup Mode
- All configuration windows accessible
- Compact layout for easy navigation
- Image viewer at moderate size
- **Save as**: "Setup Mode"

## Tips

1. **Name your configurations descriptively** - Use names that indicate their purpose
2. **Save multiple layouts** - Create different layouts for different workflows
3. **Set a default** - Your most common layout should be set as default
4. **Experiment freely** - You can always reload a saved configuration
5. **Update existing configs** - Save with the same name to overwrite

## Troubleshooting

### Layout doesn't restore perfectly
- Ensure you saved the layout after making all changes
- Try resizing the viewport to trigger the layout update callback
- Check that all windows were visible when you saved

### Docking not restored
- The docking layout is stored in the configuration
- If DearPyGui's internal state is incompatible, fall back to position/size restoration
- Check logs for any warnings about docking restoration

### Scroll positions not restored
- Scroll positions only work for scrollable windows
- Some windows may reset scroll on content changes
- Scroll restoration happens after position/size restoration

## Future Enhancements

Potential improvements for future versions:
- Auto-save current layout on exit
- Layout profiles for different screen resolutions
- Layout import/export between installations
- Keyboard shortcuts for switching layouts
- Layout preview thumbnails

## Related Files

- **Configuration Manager**: `dashboard_gui.py` (class `DashboardConfigManager`)
- **Save Function**: `AggregateControllerStreaming.save_ui_config()`
- **Load Function**: `AggregateControllerStreaming.load_ui_config()`
- **UI Dataclass**: `DashboardConfig` (includes `docking_layout` field)
- **Storage Location**: `GUI/dashboard_config.yaml`

## Version History

- **v1.0** - Initial implementation with complete layout persistence including:
  - Window positions and sizes
  - Docking state
  - Scroll positions
  - Visibility and collapsed states
  - Full DearPyGui ini file integration
