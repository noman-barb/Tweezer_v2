# GUI Module - Unified Control Dashboard

Centralized control and monitoring interface for the distributed Tweezer system, providing real-time visualization, service lifecycle management, and comprehensive system monitoring through DearPyGUI across multiple PCs connected via 10 Gigabit Ethernet.

## 📖 Table of Contents

- [Architecture Overview](#architecture-overview)
- [Distributed System Topology](#distributed-system-topology)
- [Dashboard Interface](#dashboard-interface)
- [Service Manager](#service-manager)
- [Configuration Management](#configuration-management)
- [Usage Guide](#usage-guide)

## 🏗️ Architecture Overview

The GUI module runs on the **Main Control PC** and provides centralized management for the entire distributed Tweezer system across three PCs:

1. **Main Control PC**: Arduino control, image tracking, SLM hologram generation
2. **Camera PC**: Hamamatsu camera, RAMdisk, ImageWatcher, save_compressed_server  
3. **SLM PC**: SLM hardware driver (PCIE connection)

All PCs communicate via 10 Gigabit Ethernet for low-latency gRPC communication.

## 🖧 Distributed System Topology

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│            GUI MODULE - DISTRIBUTED SYSTEM CONTROL TOPOLOGY                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                         MAIN CONTROL PC                                    │
│  │                                                                             │
│  │  ┌─────────────────────────────────────────────────────────────────┐        │
│  │  │          services_manager_gui.py (Dashboard)                   │        │
│  │  │                                                                 │        │
│  │  │  Centralized control interface for all services:               │        │
│  │  │  - Start/Stop services across all PCs                          │        │
│  │  │  - Monitor service health and performance                      │        │
│  │  │  - View logs in real-time                                      │        │
│  │  │  - Edit service parameters                                     │        │
│  │  │  - Network monitoring (10G LAN)                                │        │
│  │  └─────────────────────────────────────────────────────────────────┘        │
│  │         │                                                                   │
│  │         ├─────────────────┬────────────────────┐                            │
│  │         │                 │                    │                            │
│  │         ▼                 ▼                    ▼                            │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                     │
│  │  │  Arduino    │    │   Image     │    │    SLM      │                     │
│  │  │   gRPC      │    │   Tracker   │    │  Generator  │                     │
│  │  │  :50051     │    │   :50052    │    │   :50053    │                     │
│  │  └──────┬──────┘    └──────▲──────┘    └──────┬──────┘                     │
│  │         │                  │                   │                            │
│  │    USB Serial              │ 10G LAN           │ 10G LAN                    │
│  │         ▼                  │                   │                            │
│  │  ┌─────────────┐           │                   │                            │
│  │  │  Arduino    │           │                   │                            │
│  │  │    Due      │           │                   │                            │
│  │  │             │           │                   │                            │
│  │  │ - Laser     │           │                   │                            │
│  │  │ - Heater    │           │                   │                            │
│  │  │ - SHT3      │           │                   │                            │
│  │  └─────────────┘           │                   │                            │
│  └────────────────────────────┼───────────────────┼────────────────────────────┤
│                                │                   │                            │
│  ══════════════════════════════╪═══════════════════╪════════════════════════════│
│                                │                   │                            │
│  ┌─────────────────────────────┼───────────────────┼────────────────────────────┤
│  │                    CAMERA PC│                   │                            │
│  │                             │                   │                            │
│  │  Services managed by GUI:   │                   │                            │
│  │                             │                   │                            │
│  │  ┌─────────────────┐        │                   │                            │
│  │  │  ImageWatcher   │────────┘                   │                            │
│  │  │       .py       │                            │                            │
│  │  │  - RAMdisk      │                            │                            │
│  │  │  - Send to Main │                            │                            │
│  │  │    PC :50052    │                            │                            │
│  │  └─────────────────┘                            │                            │
│  │                                                  │                            │
│  │  ┌──────────────────────┐                       │                            │
│  │  │ save_compressed      │                       │                            │
│  │  │      _server.py      │                       │                            │
│  │  │  - TIFF → JPEG-XL    │                       │                            │
│  │  │  - RAMdisk watch     │                       │                            │
│  │  │  - Perm storage      │                       │                            │
│  │  └──────────────────────┘                       │                            │
│  │                                                  │                            │
│  │  ┌──────────────────────┐                       │                            │
│  │  │  Hamamatsu Camera    │                       │                            │
│  │  │  → RAMdisk (TIFF)    │                       │                            │
│  │  └──────────────────────┘                       │                            │
│  └──────────────────────────────────────────────────┼────────────────────────────┤
│                                                     │                            │
│  ══════════════════════════════════════════════════╪════════════════════════════│
│                                                     │                            │
│  ┌──────────────────────────────────────────────────▼────────────────────────────┤
│  │                          SLM PC                                             │
│  │                                                                             │
│  │  Services managed by GUI:                                                   │
│  │                                                                             │
│  │  ┌─────────────────┐                                                        │
│  │  │  slm_service    │◀───────────────────────────────────────┘               │
│  │  │      .py        │                                                        │
│  │  │  - gRPC :50051  │                                                        │
│  │  │  - Recv from    │                                                        │
│  │  │    Main :50053  │                                                        │
│  │  │  - SLM hardware │                                                        │
│  │  │    (PCIE)       │                                                        │
│  │  └─────────────────┘                                                        │
│  └─────────────────────────────────────────────────────────────────────────────┘
│                                                                                 │
│  Configuration File: GUI/services_config.yaml                                  │
│  - Network IP addresses and ports for each service                             │
│  - CPU affinity for optimal performance                                        │
│  - Auto-restart policies                                                       │
│  - Log directories and settings                                                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 📋 Files

- `services_config.yaml` - Main configuration file for all services (network topology, IPs, parameters)
- `services_manager_gui.py` - **GUI application for managing services (RECOMMENDED)**
- `requirements.txt` - Python dependencies for GUI manager

## 🖥️ Dashboard Interface

The main dashboard provides real-time control and monitoring:

### Features

- **Service Management**
  - Start/stop/restart services across all PCs
  - Enable/disable services
  - Auto-restart on crash
  - CPU affinity configuration
  - Real-time status monitoring

- **Arduino Hardware Control** (via Main PC)
  - DAC output adjustment (laser power on Pin 66, heater on Pin 67)
  - ADC input monitoring (sensor readings)
  - I2C device status (SHT3 environment sensor)
  - Digital I/O controls
  - Real-time telemetry streaming

- **Camera & Tracking** (distributed Camera PC → Main PC)
  - Image reception monitoring from Camera PC
  - Particle tracking statistics
  - Performance metrics (TrackPy 32 processes)
  - RAMdisk status
  - JPEG-XL compression progress

- **SLM Control** (Main PC → SLM PC)
  - Hologram generation status (CUDA on Main PC)
  - GPU utilization monitoring
  - Pattern update rate
  - Network latency to SLM PC
  - SLM hardware status

- **System Monitoring**
  - CPU and memory usage per service
  - Network throughput (10G LAN)
  - Disk I/O (RAMdisk and permanent storage)
  - Real-time log viewing from all services
  - Process health monitoring

## 🚀 Quick Start (GUI Manager - RECOMMENDED)

### 1. Install Python Dependencies

```bash
# From the GUI directory
pip install -r requirements.txt
```

Or install individually:
```bash
pip install dearpygui pyyaml psutil
```

### 2. Configure Services

Edit `services_config.yaml` and update:

**Network Configuration:**
- `image_server.args.host` - Set bind address for Image Server on Main PC
- SLM generator target: `192.168.6.2:50051` (SLM PC)

**Camera PC Paths:**
- RAMdisk path for ImageWatcher and save_compressed_server
- Permanent storage path for JPEG-XL files

**Main PC Settings:**
- `arduino_grpc.args.serial_port` - Arduino Due USB port (e.g., `/dev/ttyACM0` or `COM3`)
- CPU affinity settings for optimal performance

### 3. Launch GUI Manager

```bash
python services_manager_gui.py
```

Or on Windows:
```powershell
python services_manager_gui.py
```



### Capabilities### 4. Using the GUI



- **Process Management**The GUI provides:

  - Start/stop/restart services- **Service List**: View all configured services with status

  - Auto-restart on crash- **Start/Stop/Restart**: Control individual services with buttons

  - Graceful shutdown handling- **Enable/Disable**: Toggle services on/off via checkboxes

  - Process health monitoring- **Edit Parameters**: Click "Edit" to modify service configuration

- **View Logs**: Click "Logs" to see service output

- **Resource Control**- **Real-time Monitoring**: CPU and memory usage for running services

  - CPU affinity assignment- **Bulk Operations**: Menu options to start/stop all services

  - Memory limit enforcement

  - Priority management#### Editing Service Parameters

  - Thread count configuration

1. Click "Edit" button for any service

- **Logging & Monitoring**2. Modify parameters in the edit window

  - Real-time log viewing3. Click "Save & Apply"

  - Performance metrics4. If service is running, you'll be prompted to restart it

  - Error tracking

  - Resource usage history## Requirements



## ⚙️ Configuration Management### Python Dependencies (GUI Manager)



### Service Configuration (`service_config.yaml`)Install via `requirements.txt`:

```bash

```yamlpip install -r requirements.txt

global:```

  python_bin: python

  conda_env: tweezerOr individually:

  repo_root: ../- **dearpygui** - Modern GUI framework

  log_base_dir: ../logs/service_logs- **pyyaml** - YAML configuration parsing

  stop_services_on_exit: true- **psutil** - Process and system monitoring



services:### System Dependencies (Shell Scripts)

  arduino_grpc:

    enabled: true1. **yq** - YAML parser for bash scripts (Linux only)

    name: Arduino gRPC Server   ```bash

    python_script: Arduino/rpc/grpc_server_streaming.py   # Ubuntu/Debian

    cpu_list: 0,1   sudo apt-get install yq

    restart_on_exit: true   

    restart_delay: 5   # Or using pip

    args:   pip install yq

      serial_port: /dev/ttyACM0  # Windows: COM3   

      host: 0.0.0.0   # Or using snap

      port: 50051   sudo snap install yq

      baud: 2000000   ```



  image_server:2. **Terminal Emulator** (for `start_all_services.sh` on Linux)

    enabled: true   - gnome-terminal (recommended)

    name: Image Server with Tracking   - xterm

    python_script: Camera/ImageServer_with_track.py   - konsole

    cpu_list: 2-37   - xfce4-terminal

    restart_on_exit: true

    args:3. **taskset** - CPU affinity control (Linux - usually pre-installed)

      host: 0.0.0.0

      port: 50052## Configuration

      tracker_processes: 32

      tile_width: 256Edit `services_config.yaml` to configure all services. The file contains:

      track_diameter: 21

### Global Settings

  slm_generator:```yaml

    enabled: trueglobal:

    name: SLM Hologram Generator  python_bin: python3           # Python interpreter to use

    python_script: SLM/slm-control-server/generator_service.py  repo_root: "../../"           # Path to repository root

    cpu_list: 38,39  log_base_dir: "../../logs/service_logs"  # Base directory for logs

    args:```

      host: 0.0.0.0

      port: 50053### Service Definitions

      gpu_index: 0

```Each service has the following structure:

```yaml

### Configuration Optionsservices:

  service_name:

| Parameter | Description | Default |    enabled: true               # Enable/disable the service

|-----------|-------------|---------|    name: "Display Name"        # Human-readable name

| `enabled` | Service auto-start | true |    script_path: "path/to/script.sh"  # Relative to services directory (for shell scripts)

| `python_script` | Script path relative to repo | Required |    python_script: "path/to/script.py"  # Relative to repo root

| `cpu_list` | CPU affinity (cores or ranges) | All cores |    cpu_list: "0,1"             # CPU cores to pin to

| `restart_on_exit` | Auto-restart on crash | false |    restart_on_exit: true       # Auto-restart on failure

| `restart_delay` | Seconds before restart | 5 |    restart_delay: 5            # Seconds to wait before restart

| `log_subdir` | Log subdirectory | service name |    log_subdir: "service_logs"  # Log subdirectory

| `args` | Service-specific arguments | {} |    args:                       # Python script arguments

      host: "0.0.0.0"

## 🔗 Integration Points      port: 50052

      # ... other arguments

### Client Integration```



The dashboard integrates with all system services via gRPC:### Important Notes



```python1. **Update Required Paths**: Before running services, update these paths in `services_config.yaml`:

# Arduino integration   - `save_compressed.args.input` - Input directory for TIFF images

from Arduino.rpc.grpc_client_streaming import DueStreamingClient   - `save_compressed.args.output` - Output directory for compressed images

arduino_client = DueStreamingClient("localhost:50051")

arduino_client.connect()2. **CPU Affinity**: Adjust CPU core assignments based on your system:

   - Arduino: `cpu_list: "0,1"`

# Camera integration   - Image Server: `process_cpu_list: "2-37"`, `tracker_cpu_list: "2-33"`

from Camera.main_gui import ImageClient   - Save Compressed: `cpu_list: "38-53"`

camera_client = ImageClient("localhost:50052")   - SLM Generator: `cpu_list: "54"`



# SLM integration3. **Serial Port**: Update `arduino_grpc.args.serial_port` if your Arduino is on a different port

import hologram_pb2_grpc

slm_stub = hologram_pb2_grpc.ControlServiceStub(channel)4. **Network Addresses**: Update network addresses for SLM services if needed:

```   - `slm_generator.args.bind` - Address for clients to connect

   - `slm_generator.args.slm_target` - SLM hardware service address

### Data Flow

## Usage with GUI Manager (RECOMMENDED)

1. **User Input** → GUI event callback

2. **Validation** → Parameter checkingThe GUI manager provides the easiest way to control services:

3. **Client Call** → gRPC request

4. **Service Processing** → Hardware command```bash

5. **Response** → Status update# Start the GUI

6. **UI Update** → Visual feedbackpython services_manager_gui.py



## 📚 Usage Guide# Or specify custom config file

python services_manager_gui.py /path/to/config.yaml

### Starting the Dashboard```



```bash### GUI Features

# Activate environment

conda activate tweezer- **Service Control**: Start, stop, restart any service with a single click

- **Live Status**: See real-time CPU and memory usage

# Launch unified dashboard- **Parameter Editing**: Modify service configuration without editing YAML

python GUI/dashboard.py- **Log Viewer**: View recent log entries for each service

- **Enable/Disable**: Toggle services without removing configuration

# With custom configuration- **Bulk Operations**: Start/stop all services from the menu

python GUI/dashboard.py --config custom_config.yaml- **Auto-refresh**: Status updates automatically every second

```- **Cross-platform**: Works on Windows, Linux, and macOS



### Launching Service Manager## Usage with Shell Scripts (Linux Only)



```bash### Starting All Services

# Start service manager

python GUI/service_manager.pyLaunch all enabled services in separate terminals:

```bash

# Auto-start all servicescd services

python GUI/service_manager.py --auto-start./start_all_services.sh

```

# Load custom configuration

python GUI/service_manager.py --config services.yamlEach service will run in its own terminal window with:

```- Color-coded output

- Auto-restart on failure

### Command Line Options- Logs saved to configured directories



**Dashboard:**### Starting Individual Services

```bash

python GUI/dashboard.py \Run a single service:

    --arduino-host localhost:50051 \```bash

    --camera-host localhost:50052 \# Arduino/Hardware service

    --slm-host localhost:50053 \./Hardware/main/arduino.sh

    --log-level DEBUG \

    --fullscreen# Image acquisition service

```./Imaging/Acquisition/start_image_server.sh



**Service Manager:**# Image compression service

```bash./Imaging/Compression/start_save_compressed.sh

python GUI/service_manager.py \

    --config service_config.yaml \# SLM hologram generator

    --log-dir ./logs \./SLM/start_generator_service.sh

    --auto-start \```

    --stop-on-exit

```### Stopping Services



### Programmatic Control- To stop a service: Press `Ctrl+C` in its terminal window

- To stop all services: Close all terminal windows or press `Ctrl+C` in each

```python

from GUI.service_manager import ServicesManager### Overriding Configuration

from pathlib import Path

You can override settings via environment variables:

# Initialize manager```bash

manager = ServicesManager(Path("GUI/service_config.yaml"))# Use a different config file

manager.load_config()CONFIG_FILE=/path/to/custom_config.yaml ./start_all_services.sh



# Start services# Override CPU affinity for a specific service

manager.start_service("arduino_grpc")CPU_LIST="4-7" ./Hardware/main/arduino.sh

manager.start_service("image_server")

# Override Python interpreter

# Monitor statusPYTHON_BIN=/path/to/python3 ./start_all_services.sh

status = manager.get_service_status("arduino_grpc")```

print(f"Status: {status['status']}, PID: {status['pid']}")

### Passing Additional Arguments

# Graceful shutdown

manager.stop_all_services()All scripts accept additional command-line arguments:

``````bash

# Override port for Arduino service

## 🎨 Customization./Hardware/main/arduino.sh --port 50053



### UI Themes# Override tracking parameters for image server

./Imaging/Acquisition/start_image_server.sh --track-diameter 25 --track-separation 20

The dashboard supports custom DearPyGUI themes:

# Add verbose logging to compression service

```python./Imaging/Compression/start_save_compressed.sh --verbose

import dearpygui.dearpygui as dpg```



with dpg.theme() as custom_theme:## Logs

    with dpg.theme_component(dpg.mvAll):

        dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (20, 20, 20))Logs are stored in subdirectories under the configured `log_base_dir`:

        dpg.add_theme_color(dpg.mvThemeCol_Button, (70, 70, 180))```

        dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)logs/service_logs/

```├── arduino_grpc_server/

│   └── arduino_grpc_server_20251026_143022.log

### Layout Configuration├── imaging/

│   ├── acquisition/

- Floating/dockable windows│   │   └── image_server_with_track_20251026_143023.log

- Multi-monitor support│   └── save_compressed/

- Saved layouts│       └── save_compressed_server.log

- Customizable panels└── slm/

    └── slm_generator_service_20251026_143024.log

## 🐛 Troubleshooting```



### Common Issues## Disabling Services



**Service Won't Start**To disable a service without removing its configuration:

```bash```yaml

# Check configurationservices:

python GUI/service_manager.py --validate  service_name:

    enabled: false  # Service will be skipped

# Test service manually    # ... rest of config

python Arduino/rpc/grpc_server_streaming.py --port 50051```



# Check logs## Troubleshooting

tail -f logs/service_logs/arduino_grpc_server/service.log

```### Service Won't Start



**GUI Not Responding**1. Check if the service is enabled in `services_config.yaml`

- Verify DearPyGUI installation: `pip show dearpygui`2. Verify paths in the config file are correct

- Check graphics driver compatibility3. Check log files for error messages

- Monitor CPU usage4. Ensure required dependencies are installed

- Review event loop

### yq Not Found

**Connection Errors**

```bashInstall yq using one of the methods in Requirements section.

# Test gRPC connectivity

grpcurl -plaintext localhost:50051 list### Permission Denied



# Check port availabilityMake scripts executable:

netstat -tuln | grep 5005```bash

chmod +x start_all_services.sh

# Verify firewall settingschmod +x Hardware/main/arduino.sh

```chmod +x Imaging/Acquisition/start_image_server.sh

chmod +x Imaging/Compression/start_save_compressed.sh

**Performance Issues**chmod +x SLM/start_generator_service.sh

- Reduce telemetry update frequency```

- Lower plot refresh rates

- Check CPU affinity settings### Terminal Emulator Not Found

- Monitor memory usage

Install a supported terminal emulator:

### Debug Mode```bash

# Ubuntu/Debian

Enable detailed logging:sudo apt-get install gnome-terminal

```

```bash

# Dashboard debug mode### CPU Affinity Errors

python GUI/dashboard.py --log-level DEBUG --verbose

- Verify CPU core numbers match your system: `lscpu`

# Service manager debug- Adjust CPU ranges in `services_config.yaml`

python GUI/service_manager.py --debug- Ensure taskset is available: `which taskset`

```

## Development

## 📊 Performance Tuning

### Adding a New Service

### Optimization Tips

1. Create the service script in the appropriate subdirectory

1. **CPU Affinity**: Assign dedicated cores to services2. Add service configuration to `services_config.yaml`:

2. **Update Rates**: Balance responsiveness vs CPU usage   ```yaml

3. **Buffer Sizes**: Optimize for your data rates   services:

4. **Thread Pools**: Match to CPU core count     new_service:

5. **Network Settings**: Adjust gRPC message sizes       enabled: true

       name: "New Service"

### Resource Allocation       script_path: "path/to/new_service.sh"

       python_script: "path/to/script.py"

```yaml       cpu_list: "0"

# High-performance configuration       restart_on_exit: true

services:       restart_delay: 5

  image_server:       log_subdir: "new_service"

    cpu_list: 2-37          # Dedicated cores for tracking       args:

    args:         # Service-specific arguments

      tracker_processes: 32  # Match available cores   ```

      max_workers: 8        # gRPC worker threads3. The service will be automatically picked up by `start_all_services.sh`

      

  slm_generator:### Modifying Service Parameters

    cpu_list: 38,39         # Dedicated cores for GPU ops

    args:1. Edit `services_config.yaml`

      gpu_index: 0          # Use dedicated GPU2. Restart affected services

```3. No need to modify shell scripts unless adding new parameter types



## 📄 File Structure## Examples



```### Start only Arduino and Image services

GUI/```bash

├── dashboard.py              # Main unified dashboard# In services_config.yaml, set:

├── service_manager.py        # Service lifecycle manager# save_compressed.enabled: false

├── service_config.yaml       # Service configuration# slm_generator.enabled: false

├── TWEEZE.sh                 # Linux launch script

└── README.md                 # This documentation./start_all_services.sh

``````



## 🔐 Security Considerations### Run Image Server on different port

```bash

- Services listen on `0.0.0.0` by default (all interfaces)# Edit services_config.yaml:

- Consider using `localhost` or specific IPs for security# image_server.args.port: 50055

- No authentication implemented (trusted local network only)

- Log files may contain sensitive operational data# Or override on command line:

- Secure file permissions for configuration files./Imaging/Acquisition/start_image_server.sh --port 50055

```

## 🚀 Best Practices

### Custom CPU affinity for compression

1. **Always use the service manager** for production deployments```bash

2. **Enable auto-restart** for critical servicesCPU_LIST="40-50" ./Imaging/Compression/start_save_compressed.sh

3. **Monitor resource usage** regularly```

4. **Keep configuration under version control**
5. **Review logs** for warnings and errors
6. **Test configuration changes** before production use
7. **Document custom modifications**
8. **Backup configurations** before updates

## 📚 Additional Resources

- [Main System Documentation](../README.md)
- [Arduino Module](../Arduino/README.md)
- [Camera Module](../Camera/README.md)
- [SLM Module](../SLM/README.md)
- [DearPyGUI Documentation](https://dearpygui.readthedocs.io/)

The GUI module provides comprehensive system control and monitoring, enabling efficient operation of the Tweezer experimental setup through an intuitive, real-time interface.