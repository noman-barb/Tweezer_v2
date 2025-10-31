# Tweezer Services Configuration

This directory contains scripts and configuration for managing all Tweezer services.

## Overview

The service management system provides:
- **Centralized Configuration**: All service parameters in one YAML file
- **GUI Manager**: Modern graphical interface to control all services (RECOMMENDED)
- **Individual Service Scripts**: Start services independently via shell scripts
- **Master Launcher**: Start all services at once in separate terminals (Linux only)
- **Auto-restart**: Services automatically restart on failure
- **CPU Affinity**: Services can be pinned to specific CPU cores

## Files

- `services_config.yaml` - Main configuration file for all services
- `services_manager_gui.py` - **GUI application for managing services (RECOMMENDED)**
- `requirements.txt` - Python dependencies for GUI manager
- `start_all_services.sh` - Launch all enabled services in separate terminals (Linux)
- `Hardware/main/arduino.sh` - Arduino/Hardware telemetry service
- `Imaging/Acquisition/start_image_server.sh` - Image acquisition and tracking service
- `Imaging/Compression/start_save_compressed.sh` - Image compression service
- `SLM/start_generator_service.sh` - SLM hologram generator service

## Quick Start (GUI Manager - RECOMMENDED)

### 1. Install Python Dependencies

```bash
# From the services directory
pip install -r requirements.txt
```

Or install individually:
```bash
pip install dearpygui pyyaml psutil
```

### 2. Configure Services

Edit `services_config.yaml` and update:
- `save_compressed.args.input` - Path to TIFF input directory
- `save_compressed.args.output` - Path to compressed output directory
- `arduino_grpc.args.serial_port` - Arduino serial port
- CPU affinity settings for your system

### 3. Launch GUI Manager

```bash
python services_manager_gui.py
```

Or on Windows:
```powershell
python services_manager_gui.py
```

### 4. Using the GUI

The GUI provides:
- **Service List**: View all configured services with status
- **Start/Stop/Restart**: Control individual services with buttons
- **Enable/Disable**: Toggle services on/off via checkboxes
- **Edit Parameters**: Click "Edit" to modify service configuration
- **View Logs**: Click "Logs" to see service output
- **Real-time Monitoring**: CPU and memory usage for running services
- **Bulk Operations**: Menu options to start/stop all services

#### Editing Service Parameters

1. Click "Edit" button for any service
2. Modify parameters in the edit window
3. Click "Save & Apply"
4. If service is running, you'll be prompted to restart it

## Requirements

### Python Dependencies (GUI Manager)

Install via `requirements.txt`:
```bash
pip install -r requirements.txt
```

Or individually:
- **dearpygui** - Modern GUI framework
- **pyyaml** - YAML configuration parsing
- **psutil** - Process and system monitoring

### System Dependencies (Shell Scripts)

1. **yq** - YAML parser for bash scripts (Linux only)
   ```bash
   # Ubuntu/Debian
   sudo apt-get install yq
   
   # Or using pip
   pip install yq
   
   # Or using snap
   sudo snap install yq
   ```

2. **Terminal Emulator** (for `start_all_services.sh` on Linux)
   - gnome-terminal (recommended)
   - xterm
   - konsole
   - xfce4-terminal

3. **taskset** - CPU affinity control (Linux - usually pre-installed)

## Configuration

Edit `services_config.yaml` to configure all services. The file contains:

### Global Settings
```yaml
global:
  python_bin: python3           # Python interpreter to use
  repo_root: "../../"           # Path to repository root
  log_base_dir: "../../logs/service_logs"  # Base directory for logs
```

### Service Definitions

Each service has the following structure:
```yaml
services:
  service_name:
    enabled: true               # Enable/disable the service
    name: "Display Name"        # Human-readable name
    script_path: "path/to/script.sh"  # Relative to services directory (for shell scripts)
    python_script: "path/to/script.py"  # Relative to repo root
    cpu_list: "0,1"             # CPU cores to pin to
    restart_on_exit: true       # Auto-restart on failure
    restart_delay: 5            # Seconds to wait before restart
    log_subdir: "service_logs"  # Log subdirectory
    args:                       # Python script arguments
      host: "0.0.0.0"
      port: 50052
      # ... other arguments
```

### Important Notes

1. **Update Required Paths**: Before running services, update these paths in `services_config.yaml`:
   - `save_compressed.args.input` - Input directory for TIFF images
   - `save_compressed.args.output` - Output directory for compressed images

2. **CPU Affinity**: Adjust CPU core assignments based on your system:
   - Arduino: `cpu_list: "0,1"`
   - Image Server: `process_cpu_list: "2-37"`, `tracker_cpu_list: "2-33"`
   - Save Compressed: `cpu_list: "38-53"`
   - SLM Generator: `cpu_list: "54"`

3. **Serial Port**: Update `arduino_grpc.args.serial_port` if your Arduino is on a different port

4. **Network Addresses**: Update network addresses for SLM services if needed:
   - `slm_generator.args.bind` - Address for clients to connect
   - `slm_generator.args.slm_target` - SLM hardware service address

## Usage with GUI Manager (RECOMMENDED)

The GUI manager provides the easiest way to control services:

```bash
# Start the GUI
python services_manager_gui.py

# Or specify custom config file
python services_manager_gui.py /path/to/config.yaml
```

### GUI Features

- **Service Control**: Start, stop, restart any service with a single click
- **Live Status**: See real-time CPU and memory usage
- **Parameter Editing**: Modify service configuration without editing YAML
- **Log Viewer**: View recent log entries for each service
- **Enable/Disable**: Toggle services without removing configuration
- **Bulk Operations**: Start/stop all services from the menu
- **Auto-refresh**: Status updates automatically every second
- **Cross-platform**: Works on Windows, Linux, and macOS

## Usage with Shell Scripts (Linux Only)

### Starting All Services

Launch all enabled services in separate terminals:
```bash
cd services
./start_all_services.sh
```

Each service will run in its own terminal window with:
- Color-coded output
- Auto-restart on failure
- Logs saved to configured directories

### Starting Individual Services

Run a single service:
```bash
# Arduino/Hardware service
./Hardware/main/arduino.sh

# Image acquisition service
./Imaging/Acquisition/start_image_server.sh

# Image compression service
./Imaging/Compression/start_save_compressed.sh

# SLM hologram generator
./SLM/start_generator_service.sh
```

### Stopping Services

- To stop a service: Press `Ctrl+C` in its terminal window
- To stop all services: Close all terminal windows or press `Ctrl+C` in each

### Overriding Configuration

You can override settings via environment variables:
```bash
# Use a different config file
CONFIG_FILE=/path/to/custom_config.yaml ./start_all_services.sh

# Override CPU affinity for a specific service
CPU_LIST="4-7" ./Hardware/main/arduino.sh

# Override Python interpreter
PYTHON_BIN=/path/to/python3 ./start_all_services.sh
```

### Passing Additional Arguments

All scripts accept additional command-line arguments:
```bash
# Override port for Arduino service
./Hardware/main/arduino.sh --port 50053

# Override tracking parameters for image server
./Imaging/Acquisition/start_image_server.sh --track-diameter 25 --track-separation 20

# Add verbose logging to compression service
./Imaging/Compression/start_save_compressed.sh --verbose
```

## Logs

Logs are stored in subdirectories under the configured `log_base_dir`:
```
logs/service_logs/
├── arduino_grpc_server/
│   └── arduino_grpc_server_20251026_143022.log
├── imaging/
│   ├── acquisition/
│   │   └── image_server_with_track_20251026_143023.log
│   └── save_compressed/
│       └── save_compressed_server.log
└── slm/
    └── slm_generator_service_20251026_143024.log
```

## Disabling Services

To disable a service without removing its configuration:
```yaml
services:
  service_name:
    enabled: false  # Service will be skipped
    # ... rest of config
```

## Troubleshooting

### Service Won't Start

1. Check if the service is enabled in `services_config.yaml`
2. Verify paths in the config file are correct
3. Check log files for error messages
4. Ensure required dependencies are installed

### yq Not Found

Install yq using one of the methods in Requirements section.

### Permission Denied

Make scripts executable:
```bash
chmod +x start_all_services.sh
chmod +x Hardware/main/arduino.sh
chmod +x Imaging/Acquisition/start_image_server.sh
chmod +x Imaging/Compression/start_save_compressed.sh
chmod +x SLM/start_generator_service.sh
```

### Terminal Emulator Not Found

Install a supported terminal emulator:
```bash
# Ubuntu/Debian
sudo apt-get install gnome-terminal
```

### CPU Affinity Errors

- Verify CPU core numbers match your system: `lscpu`
- Adjust CPU ranges in `services_config.yaml`
- Ensure taskset is available: `which taskset`

## Development

### Adding a New Service

1. Create the service script in the appropriate subdirectory
2. Add service configuration to `services_config.yaml`:
   ```yaml
   services:
     new_service:
       enabled: true
       name: "New Service"
       script_path: "path/to/new_service.sh"
       python_script: "path/to/script.py"
       cpu_list: "0"
       restart_on_exit: true
       restart_delay: 5
       log_subdir: "new_service"
       args:
         # Service-specific arguments
   ```
3. The service will be automatically picked up by `start_all_services.sh`

### Modifying Service Parameters

1. Edit `services_config.yaml`
2. Restart affected services
3. No need to modify shell scripts unless adding new parameter types

## Examples

### Start only Arduino and Image services
```bash
# In services_config.yaml, set:
# save_compressed.enabled: false
# slm_generator.enabled: false

./start_all_services.sh
```

### Run Image Server on different port
```bash
# Edit services_config.yaml:
# image_server.args.port: 50055

# Or override on command line:
./Imaging/Acquisition/start_image_server.sh --port 50055
```

### Custom CPU affinity for compression
```bash
CPU_LIST="40-50" ./Imaging/Compression/start_save_compressed.sh
```
