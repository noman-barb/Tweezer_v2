# Tweezer Control System

A comprehensive optical tweezer control system with real-time particle tracking, SLM hologram generation, and Arduino-based hardware control. This system provides a complete solution for optical manipulation experiments with sub-millisecond response times.

## 📖 Table of Contents

- [System Architecture](#system-architecture)
- [Quick Start](#quick-start)
- [Environment Setup](#environment-setup)
- [Module Overview](#module-overview)
- [Hardware Requirements](#hardware-requirements)
- [Performance Specifications](#performance-specifications)
- [Troubleshooting](#troubleshooting)

## 🏗️ System Architecture

The Tweezer Control System follows a distributed microservices architecture across multiple PCs connected via direct Ethernet connections for low-latency gRPC communication:

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                    TWEEZER DISTRIBUTED CONTROL SYSTEM                          │
│                     (Multi-PC Direct Connection Architecture)                  │
├────────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                         MAIN CONTROL PC                                     │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐   │
│  │  │    GUI      │    │   Image     │    │   Arduino   │    │    SLM      │   │
│  │  │ Dashboard   │    │   Tracker   │    │    gRPC     │    │  Hologram   │   │
│  │  │             │    │   Server    │    │   Server    │    │  Generator  │   │
│  │  │  ┌───────┐  │    │  ┌───────┐  │    │             │    │             │   │
│  │  │  │Service│  │    │  │TrackPy│  │    │  ┌───────┐  │    │  ┌───────┐  │   │
│  │  │  │Manager│  │    │  │Engine │  │    │  │Serial │  │    │  │CUDA   │  │   │
│  │  │  └───────┘  │    │  │:50052 │  │    │  │Bridge │  │    │  │Engine │  │   │
│  │  │             │    │  └───────┘  │    │  │:50051 │  │    │  │:50053 │  │   │
│  │  └─────────────┘    └─────────────┘    │  └───────┘  │    │  └───────┘  │   │
│  │                             ▲           └─────────────┘    └─────────────┘  │
│  │                             │                  │                   │        │
│  │                             │                  │ USB Serial        │        │
│  │                             │                  ▼                   │        │
│  │                             │           ┌─────────────┐            │        │
│  │                             │           │  Arduino    │            │        │
│  │                             │           │    Due      │            │        │
│  │                             │           │  ┌───────┐  │            │        │
│  │                             │           │  │DAC/ADC│  │            │        │
│  │                             │           │  │Laser  │  │            │        │
│  │                             │           │  │Heater │  │            │        │
│  │                             │           │  │SHT3   │  │            │        │
│  │                             │           │  └───────┘  │            │        │
│  │                             │           └─────────────┘            │        │
│  └─────────────────────────────┼──────────────────────────────────────┼────────┤
│                                │                                      │        │
│                                │ Direct Ethernet Connection           │ Direct │
│  ══════════════════════════════╪══════════════════════════════════════╪════════│
│                                │                                      │        │
│  ┌─────────────────────────────┼──────────────────────────────────────┼────────┤
│  │                    CAMERA PC (Image Acquisition)                   │        │
│  │                                │                                   │        │
│  │  ┌─────────────┐    ┌─────────▼──────┐    ┌─────────────┐          │        │
│  │  │  Hamamatsu  │    │     Image      │    │    Save     │          │        │
│  │  │   Camera    │───▶│    Watcher     │    │ Compressed  │         │        │
│  │  │             │    │                │    │   Server    │          │        │
│  │  │  ┌───────┐  │    │  ┌───────────┐ │    │  ┌───────┐  │          │        │
│  │  │  │CMOS   │  │    │  │gRPC Client│ │    │  │TIFF   │  │          │        │
│  │  │  │Sensor │  │    │  │→:50052    │ │    │  │→JPEG  │  │          │        │
│  │  │  └───────┘  │    │  │Watch TIFF │ │    │  │-XL    │  │          │        │
│  │  │             │    │  │RAMdisk    │ │    │  │Watch  │  │          │        │
│  │  └─────────────┘    │  └───────────┘ │    │  │RAMdisk│  │          │        │
│  │         │           └────────────────┘    │  └───────┘  │          │        │
│  │    Camera Software          ▲             └─────────────┘          │        │
│  │         │                   │                     │                │        │
│  │         ▼                   │                     ▼                │        │
│  │  ┌─────────────┐            │            ┌─────────────┐           │        │
│  │  │  RAMdisk    │────────────┘            │ Permanent   │           │        │
│  │  │  (TIFF)     │                         │  Storage    │           │        │
│  │  │  Temporary  │◀────────────────────────│ (JPEG-XL)   │           │        │
│  │  └─────────────┘   save when requested   └─────────────┘           │        │
│  └─────────────────────────────────────────────────────────────────────────────┤
│                                                                        │        │
│  ══════════════════════════════════════════════════════════════════════════════│
│                                                                        │        │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                        SLM PC (Hologram Display)                            │
│  │                                                                     │       │
│  │  ┌──────────────────────────────────────────────────────┐           │       │
│  │  │                   SLM Driver Service                 │           │       │
│  │  │                                                       │          │       │
│  │  │  ┌─────────────────┐    ┌────────────────────────┐   │           │       │
│  │  │  │ gRPC Server     │◀───┤  Generator (:50053)    │◀──┼──────────┘       │
│  │  │  │ Receives        │    │  on Main PC            │   │                   │
│  │  │  │ Holograms       │    └────────────────────────┘   │                   │
│  │  │  │ :50051          │                                 │                   │
│  │  │  └─────────────────┘                                 │                   │
│  │  │         │                                            │                   │
│  │  │         ▼                                            │                   │
│  │  │  ┌─────────────────┐    ┌─────────────┐              │                   │
│  │  │  │ SLM Hardware    │    │    SLM      │              │                   │
│  │  │  │ Driver          │───▶│  Display    │              │                  │
│  │  │  │ (PCIE)          │    │  Hardware   │              │                   │
│  │  │  │                 │    │  ┌───────┐  │              │                   │
│  │  │  │                 │    │  │Spatial│  │              │                   │
│  │  │  │                 │    │  │Light  │  │              │                   │
│  │  │  │                 │    │  │Mod.   │  │              │                   │
│  │  │  └─────────────────┘    │  └───────┘  │              │                   │
│  │  │                         └─────────────┘              │                   │
│  │  └──────────────────────────────────────────────────────┘                   │
│  └─────────────────────────────────────────────────────────────────────────────┘
│                                                                                │
│  Key Data Flows:                                                               │
│  1. Camera → RAMdisk (TIFF) → ImageWatcher → Main PC (Direct Connection)       │
│  2. Dashboard → Image Server → Get tracked particles                           │
│  3. Dashboard → SLM Generator → SLM Driver PC → SLM (Direct Connection)        │
│  4. Dashboard → Arduino Server → Arduino Due (USB Serial)                      │
│  5. RAMdisk (TIFF) → save_compressed_server → Permanent Storage (JPEG-XL)      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### 1. Environment Setup

Create and activate the conda environment:

```bash
# Navigate to project root
cd /path/to/Tweezer

# Create conda environment from YAML
conda env create -f env/tweezer.yaml

# Activate environment
conda activate tweezer

# Install additional pip dependencies
pip install -r env/requirements.txt
```

### 2. Hardware Connections

The system uses a **distributed three-PC architecture** connected via direct Ethernet connections:

```
Main Control PC:
  - Arduino Due → USB Serial (laser, heater, sensors)
  - Direct Ethernet → Camera PC
  - Direct Ethernet → SLM PC

Camera PC:
  - Hamamatsu Camera → Direct connection
  - Camera Software → RAMdisk (TIFF images)
  - Direct Ethernet → Main Control PC

SLM PC:
  - SLM Hardware → PCIE adapter
  - Direct Ethernet → Main Control PC

Power Connections:
  - Laser → Arduino DAC0 (Pin 66)
  - Objective Heater → Arduino DAC1 (Pin 67)
  - Environment Sensor (SHT3) → Arduino I2C
```

**Note**: PCs are connected via direct Ethernet connections (not through a network switch). See `GUI/services_config.yaml` for IP addresses and network topology.

### 3. Service Startup

Start all services using the service manager:

```bash
# Launch the service manager GUI
python GUI/service_manager.py

# Or start services individually:
python Arduino/rpc/grpc_server_streaming.py --port 50051
python Camera/ImageServer_with_track.py --port 50052
python SLM/slm-control-server/generator_service.py --port 50053
```

### 4. Launch Dashboard

```bash
python GUI/dashboard.py
```

## 🔧 Environment Setup

### Conda Environment

The system uses a carefully configured conda environment with GPU support:

```yaml
name: tweezer
channels:
  - conda-forge
  - defaults
dependencies:
  - python=3.11.14
  - cupy=13.1.0          # GPU acceleration
  - numpy=1.26.4         # Numerical computing
  - grpcio=1.75.1        # Communication
  - dearpygui=1.10.1     # GUI framework
  - opencv-python=4.11.0 # Image processing
  - trackpy=0.7          # Particle tracking
  - tifffile=2025.10.16  # Image I/O
  # ... (see env/tweezer.yaml for complete list)
```

### Key Dependencies

| Component | Purpose | Version |
|-----------|---------|---------|
| **CuPy** | GPU computing for SLM | 13.1.0 |
| **gRPC** | Inter-service communication | 1.75.1 |
| **DearPyGUI** | Real-time dashboard | 1.10.1 |
| **TrackPy** | Particle tracking algorithms | 0.7 |
| **OpenCV** | Image processing | 4.11.0 |
| **PySerial** | Arduino communication | 3.5 |
| **TiffFile** | High-performance image I/O | 2025.10.16 |

## 📋 Module Overview

### Arduino Module (`Arduino/`)
**Location**: Main Control PC (USB Serial connection)

Hardware interface for precision control of:
- DAC outputs (laser power on DAC0/Pin 66, objective heater on DAC1/Pin 67)
- ADC inputs (sensor monitoring)
- I2C devices (SHT3 environment sensor for temperature/humidity)
- Digital I/O (triggers, status)
- Serial communication with CRC error checking

**Key Features:**
- Millisecond-level response latency
- CRC-8 error detection
- Streaming gRPC interface on port 50051
- 12-bit DAC/ADC resolution
- Connected directly to Main Control PC via USB

### Camera Module (`Camera/`)
**Location**: Distributed across Camera PC and Main Control PC

- **Camera PC**: Hamamatsu camera → RAMdisk (TIFF) → ImageWatcher (sends via direct connection) + save_compressed_server (TIFF→JPEG-XL)
- **Main Control PC**: ImageServer_with_track.py receives images and performs TrackPy tracking

**Key Features:**
- Tracking latency: ~40ms for 1152x1152 images with ~3500 particles
- Tile-based parallel processing (32 AMD EPYC gen 2 cores)
- RAMdisk-based image capture on Camera PC
- Lossless JPEG-XL compression for permanent storage
- Sub-pixel tracking accuracy with TrackPy
- gRPC streaming on port 50052 from Main PC

### GUI Module (`GUI/`)
**Location**: Main Control PC

Centralized control dashboard for entire distributed system:
- Service lifecycle management across all PCs
- Real-time system monitoring
- Configuration management (services_config.yaml)
- Data visualization

**Key Features:**
- DearPyGUI-based interface
- Service health monitoring
- Live performance metrics
- Controls all services via gRPC

### SLM Module (`SLM/`)
**Location**: Distributed between Main Control PC and SLM PC

- **Main Control PC**: generator_service.py (CUDA hologram generation on port 50053)
- **SLM PC**: slm_service.py (hardware driver receiving holograms, connected via PCIE adapter)

**Key Features:**
- GPU-accelerated FFT on Main PC (A4000 GPU)
- Hologram streaming at up to 200 fps (with iteration=2 setting)
- SLM hardware maximum: 300 fps (generator limited by A4000 GPU)
- Gerchberg-Saxton algorithm for hologram generation
- SLM hardware connected via PCIE adapter on dedicated SLM PC
- Direct Ethernet connection between generator and driver PCs

## 💻 Hardware Requirements

### System Topology

The system requires **three separate PCs** connected via **direct Ethernet connections**:

#### Main Control PC
- **Purpose**: Dashboard, Image Tracker, SLM Generator, Arduino Control
- **USB**: 1x USB port for Arduino Due connection
- **Network**: Direct Ethernet connections to Camera PC and SLM PC

#### Camera PC
- **Purpose**: Hamamatsu Camera Control, Image Watcher, Save Compressed Server
- **Connection**: Hamamatsu camera connected directly
- **Storage**: 
  - RAMdisk for temporary TIFF storage
  - Permanent storage for compressed JPEG-XL
- **Network**: Direct Ethernet connection to Main Control PC
- **Camera**: Hamamatsu camera interface

#### SLM PC
- **Purpose**: SLM Hardware Driver
- **Connection**: SLM connected via PCIE adapter
- **Network**: Direct Ethernet connection to Main Control PC
- **PCIE**: SLM hardware connection via PCIE adapter

### Network Requirements

- **Direct Ethernet connections** between PCs (not through a switch)
- Low-latency network configuration for real-time control
- See `GUI/services_config.yaml` for IP topology

### Peripheral Hardware

| Component | Connection | Model/Notes |
|-----------|-----------|-------------|
| **Arduino Due** | USB Serial → Main PC | 12-bit DAC for laser/heater control |
| **Hamamatsu Camera** | Direct → Camera PC | Images dumped to RAMdisk |
| **SLM Hardware** | PCIE adapter → SLM PC | Spatial Light Modulator |
| **Laser** | DAC0 (Arduino Due Pin 66) | Controlled via Arduino |
| **Objective Heater** | DAC1 (Arduino Due Pin 67) | Controlled via Arduino |
| **Environment Sensor** | I2C (SHT3 → Arduino Due) | Temperature/Humidity monitoring |

## ⚡ Performance Specifications

### Timing Performance

```
┌─────────────────────────────────────────────────────────────┐
│                    SYSTEM LATENCIES                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Arduino Command Processing:        millisecond-level      │
│  ├─ Serial communication                                   │
│  ├─ Command execution                                      │
│  └─ Response transmission                                  │
│                                                             │
│  Camera Frame Processing:           ~40 ms                 │
│  ├─ Image size: 1152x1152                                  │
│  ├─ Particle count: ~3500 particles                        │
│  └─ Processing: 32 AMD EPYC gen 2 cores                    │
│                                                             │
│  SLM Hologram Update:               200 fps (5 ms/frame)   │
│  ├─ Hologram generation (GPU)                              │
│  ├─ Network transfer                                       │
│  └─ Display refresh                                        │
│  └─ Note: SLM hardware max 300 fps, limited by generator   │
│                                                             │
│  End-to-End Response:               ~40-50 ms              │
│  (Detection → SLM update)                                  │
└─────────────────────────────────────────────────────────────┘
```

### Throughput Specifications

| Metric | Value | Notes |
|--------|-------|-------|
| **Particle Tracking** | ~40 ms latency | 1152x1152 images, ~3500 particles |
| **Tracking Hardware** | 32 cores | AMD EPYC gen 2 processors |
| **Arduino Commands** | millisecond-level | Streaming protocol |
| **SLM Updates** | 200 fps (iteration=2) | Generator limited (A4000 GPU) |
| **SLM Hardware Max** | 300 fps | PCIE adapter connection |
| **Data Logging** | 1 MB/s | Compressed HDF5 |

## 🔍 Data Flow Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              DATA FLOW PIPELINE                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  Camera          Image           Tracking          Position         SLM         │
│  Capture   ───▶  Buffer    ───▶  Engine     ───▶   Data      ───▶  Control     │
│     │              │               │                │              │            │
│     │              │               │                │              │            │
│  ┌─────┐        ┌─────┐         ┌─────┐          ┌─────┐        ┌─────┐        │
│  │CMOS │        │Ring │         │Track│          │gRPC │        │CUDA │        │
│  │Sens │        │Buff │         │ Py  │          │Msg  │        │Kern │        │
│  │or   │        │er   │         │Proc │          │     │        │el   │        │
│  └─────┘        └─────┘         └─────┘          └─────┘        └─────┘        │
│     │              │               │                │              │            │
│     │              │               ▼                ▼              ▼            │
│     │              │           ┌─────────────────────────────────────────┐      │
│     │              │           │           Data Storage                  │      │
│     │              │           │                                         │      │
│     │              └──────────▶│  ┌─────┐    ┌─────┐    ┌─────┐        │      │
│     │                          │  │HDF5 │    │TIFF │    │ CSV │        │      │
│     │                          │  │Logs │    │Image│    │Meta │        │      │
│     │                          │  └─────┘    └─────┘    └─────┘        │      │
│     │                          └─────────────────────────────────────────┘      │
│     │                                                                           │
│     └─────────────────────────────────────────────────────────────────────────▶│
│                                Real-time Display                               │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 🛠️ Configuration

### Service Configuration (`GUI/service_config.yaml`)

```yaml
services:
  arduino_grpc:
    enabled: true
    port: 50051
    serial_port: /dev/ttyACM0  # Windows: COM3
    baud: 2000000
    
  image_server:
    enabled: true
    port: 50052
    tracker_processes: 32
    tile_size: 256
    
  slm_control:
    enabled: true
    port: 50053
    gpu_index: 0
    hologram_size: [512, 512]
```

### Pin Configuration (`Arduino/pin_config.json`)

```json
{
  "LASER_POWER_CONTROL_DAC_PIN": {
    "pin": "DAC0",
    "kind": "dac_pin",
    "unit": "W",
    "conversion": 1,
    "min_value": 0.0,
    "max_value": 3.3
  }
}
```

## 🐛 Troubleshooting

### Common Issues

**Arduino Connection Failed**
```bash
# Check device enumeration
ls /dev/ttyACM*  # Linux
# or
Get-WmiObject -Class Win32_SerialPort  # Windows PowerShell

# Test serial communication
python Arduino/interface/due_bridge.py --port /dev/ttyACM0 --test
```

**GPU Not Detected**
```bash
# Verify CUDA installation
nvidia-smi
python -c "import cupy; print(cupy.cuda.runtime.getDeviceCount())"

# Check CuPy installation
python -c "import cupy; print(cupy.__version__)"
```

**gRPC Connection Issues**
```bash
# Test service connectivity
grpcurl -plaintext localhost:50051 list
grpcurl -plaintext localhost:50052 list
grpcurl -plaintext localhost:50053 list
```

**Performance Issues**
- Ensure CPU affinity is set correctly in service config
- Monitor GPU memory usage during operation
- Check disk I/O performance for data logging
- Verify network bandwidth for remote cameras

### Logging

All services log to `logs/AutoLogs/` with structured JSON format:
- `arduino_grpc_server.log` - Hardware communication
- `image_server_metrics.log` - Tracking performance
- `slm_generator_metrics.log` - GPU operations
- `dashboard.log` - GUI events

### Performance Monitoring

Use the built-in dashboard to monitor:
- Service health and uptime
- Real-time performance metrics
- Hardware resource utilization
- Error rates and warnings

## 📚 Additional Documentation

- [`Arduino/README.md`](Arduino/README.md) - Hardware interface details
- [`Camera/README.md`](Camera/README.md) - Tracking system architecture
- [`GUI/README.md`](GUI/README.md) - Dashboard and service management
- [`SLM/README.md`](SLM/README.md) - Hologram generation system
- [`logs/README.md`](logs/README.md) - Monitoring and diagnostics

## 🤝 Contributing

1. Follow the established architecture patterns
2. Maintain gRPC API compatibility
3. Add comprehensive tests for new features
4. Update documentation for API changes
5. Profile performance impact of modifications

## 📄 License

This project is proprietary software developed for integrating, SLM, LASER, CAMERA, MICROSCOPE, etc, required for high speed Holographic Optical Tweezer at @ECFL@IITGN@INDIA; https://chandanmishra.people.iitgn.ac.in/