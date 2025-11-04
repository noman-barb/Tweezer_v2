# Quick Reference: Experiment Parameter Configurations

## Save Parameters
1. Select script from dropdown
2. Adjust parameters in "Script Parameters" section
3. Click **Parameters Config** to expand
4. Click **Save** button
5. Enter name and description
6. Click **Save** in dialog

## Load Parameters
1. Select script from dropdown
2. Expand **Parameters Config** section
3. Choose config from dropdown
4. Click **Load** button

## Set Default
1. Select config from dropdown
2. Click **Set Default** button
3. Config will auto-load when selecting script

## Delete Configuration
1. Select config from dropdown
2. Click **Delete** button
3. Confirm deletion

## Keyboard Shortcuts
- Enter in save dialog → Save
- Escape in any modal → Cancel

## Tips
- **Descriptive Names**: Use names like "High_Power_Run1" not "test"
- **Use Descriptions**: Document why these parameters were chosen
- **Set Defaults**: Save time by setting frequently-used configs as defaults
- **Version Control**: Commit `.yaml` files to track parameter changes

## File Location
```
ExperimentScripts/<ScriptName>/params/<config_name>.yaml
```

## Troubleshooting
| Problem | Solution |
|---------|----------|
| Config not showing | Click "Reload Scripts" |
| Can't save | Check folder permissions |
| Wrong values loaded | Verify parameter names match script |
| Default not loading | Check `params_defaults.yaml` file |

## See Also
- `PARAMS_CONFIG_GUIDE.md` - Full documentation
- `PARAMS_CONFIG_IMPLEMENTATION.md` - Technical details
- `AUTO_CONFIG_GUIDE.md` - Creating configurable scripts
