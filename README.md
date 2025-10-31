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

The Tweezer Control System follows a distributed microservices architecture with gRPC communication for low-latency operations:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           TWEEZER CONTROL SYSTEM                               │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    │
│  │    GUI      │    │   Camera    │    │   Arduino   │    │     SLM     │    │
│  │ Dashboard   │    │   Server    │    │    gRPC     │    │  Generator  │    │
│  │             │    │             │    │   Server    │    │   Service   │    │
│  │  ┌───────┐  │    │  ┌───────┐  │    │             │    │             │    │
│  │  │Service│  │    │  │Track- │  │    │  ┌───────┐  │    │  ┌───────┐  │    │
│  │  │Manager│  │    │  │py     │  │    │  │Serial │  │    │  │CUDA   │  │    │
│  │  └───────┘  │    │  │Engine │  │    │  │Bridge │  │    │  │Engine │  │    │
│  │             │    │  └───────┘  │    │  └───────┘  │    │  └───────┘  │    │
│  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘    │
│         │                   │                   │                   │         │
│         └───────────────────┼───────────────────┼───────────────────┘         │
│                             │                   │                             │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                      gRPC Communication Layer                              │
│  │                                                                             │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  │   :50050    │    │   :50052    │    │   :50051    │    │   :50053    │  │
│  │  │ Dashboard   │    │   Image     │    │   Arduino   │    │    SLM      │  │
│  │  │   Server    │    │  Exchange   │    │  Streaming  │    │  Control    │  │
│  │  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘  │
│  └─────────────────────────────────────────────────────────────────────────────┘
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                         Hardware Layer                                     │
│  │                                                                             │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  │   Display   │    │   Camera    │    │  Arduino    │    │     SLM     │  │
│  │  │  Monitor    │    │  Hardware   │    │     Due     │    │  Hardware   │  │
│  │  │             │    │             │    │             │    │             │  │
│  │  │  ┌───────┐  │    │  ┌───────┐  │    │  ┌───────┐  │    │  ┌───────┐  │  │
│  │  │  │DearPy│  │    │  │CMOS   │  │    │  │DAC/ADC│  │    │  │Spatial│  │  │
│  │  │  │GUI    │  │    │  │Sensor │  │    │  │I/O    │  │    │  │Light  │  │  │
│  │  │  └───────┘  │    │  └───────┘  │    │  └───────┘  │    │  │Mod.   │  │  │
│  │  └─────────────┘    └─────────────┘    └─────────────┘    │  └───────┘  │  │
│  └─────────────────────────────────────────────────────────────────────────────┘
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

Connect your hardware in this order:

```
1. Arduino Due → USB (Serial communication)
2. Camera → USB3/GigE (High-speed imaging)
3. SLM → GPU/Display port (Hologram display)
4. Power supplies → DAC outputs (Laser/heater control)
```

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
Hardware interface for precision control of:
- DAC outputs (laser power, heater control)
- ADC inputs (sensor monitoring)
- Digital I/O (triggers, status)
- Serial communication with CRC error checking

**Key Features:**
- Sub-millisecond response time
- CRC-8 error detection
- Streaming gRPC interface
- 12-bit DAC/ADC resolution

### Camera Module (`Camera/`)
Real-time particle tracking and image analysis:
- Multi-threaded TrackPy integration
- HDF5 data storage with compression
- gRPC image streaming
- Real-time visualization

**Key Features:**
- 100+ fps tracking performance
- Tile-based processing for large images
- Lossless image compression
- Sub-pixel tracking accuracy

### GUI Module (`GUI/`)
Centralized control dashboard:
- Service lifecycle management
- Real-time system monitoring
- Configuration management
- Data visualization

**Key Features:**
- DearPyGUI-based interface
- Service health monitoring
- Live performance metrics
- Configuration hot-reloading

### SLM Module (`SLM/`)
Spatial Light Modulator control:
- CUDA-accelerated hologram generation
- Real-time pattern updates
- Hardware abstraction layer
- Performance optimization

**Key Features:**
- GPU-accelerated FFT
- Sub-frame latency updates
- Multiple hologram algorithms
- Hardware vendor abstraction

## 💻 Hardware Requirements

### Minimum Specifications

```
CPU: Intel i5-8400 / AMD Ryzen 5 2600
RAM: 16 GB DDR4
GPU: NVIDIA GTX 1060 6GB (CUDA 12.0+)
Storage: 500GB SSD
USB: 3x USB 3.0 ports
Network: Gigabit Ethernet (for GigE cameras)
```

### Recommended Specifications

```
CPU: Intel i7-12700K / AMD Ryzen 7 5800X
RAM: 32 GB DDR4-3200
GPU: NVIDIA RTX 4070 / A4000 (12GB VRAM)
Storage: 1TB NVMe SSD
USB: 4x USB 3.2 ports
Network: 10 Gigabit Ethernet
```

### Supported Hardware

| Component | Models | Notes |
|-----------|--------|-------|
| **Arduino** | Due (recommended), Mega2560 | Due required for 12-bit DAC |
| **Camera** | CMOS sensors via USB3/GigE | >2MP recommended |
| **SLM** | Most display-based SLMs | GPU driver support required |
| **GPU** | NVIDIA with CUDA 12.0+ | AMD not supported |

## ⚡ Performance Specifications

### Timing Performance

```
┌─────────────────────────────────────────────────────────────┐
│                    SYSTEM LATENCIES                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Arduino Command Processing:        < 1 ms                 │
│  ├─ Serial communication:           ~0.2 ms                │
│  ├─ Command execution:              ~0.1 ms                │
│  └─ Response transmission:          ~0.1 ms                │
│                                                             │
│  Camera Frame Processing:           5-10 ms                │
│  ├─ Image acquisition:              ~2 ms                  │
│  ├─ Particle detection:             ~3 ms                  │
│  └─ Result transmission:            ~1 ms                  │
│                                                             │
│  SLM Hologram Update:               1-5 ms                 │
│  ├─ Pattern generation (GPU):       ~1 ms                  │
│  ├─ Memory transfer:                ~0.5 ms                │
│  └─ Display refresh:                ~3 ms                  │
│                                                             │
│  End-to-End Response:               10-20 ms               │
│  (Detection → SLM update)                                  │
└─────────────────────────────────────────────────────────────┘
```

### Throughput Specifications

| Metric | Value | Notes |
|--------|-------|-------|
| **Image Processing** | 100+ fps | Dependent on image size |
| **Particle Tracking** | 1000+ particles/frame | TrackPy optimized |
| **Arduino Commands** | 1000+ Hz | Streaming protocol |
| **SLM Updates** | 200+ Hz | GPU memory permitting |
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

This project is proprietary software developed for optical tweezer research applications.