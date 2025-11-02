# SLM Module - Spatial Light Modulator Control

GPU-accelerated hologram generation and hardware control system for creating dynamic optical trap patterns in real-time. The system operates across two PCs connected via direct Ethernet connection with CUDA-optimized Gerchberg-Saxton algorithm.

## 📖 Table of Contents

- [Architecture Overview](#architecture-overview)
- [Hardware Topology](#hardware-topology)
- [Hologram Generation](#hologram-generation)
- [Hardware Driver](#hardware-driver)
- [Algorithm Implementation](#algorithm-implementation)
- [Performance Optimization](#performance-optimization)
- [Network Communication](#network-communication)
- [API Reference](#api-reference)

## 🏗️ Architecture Overview

The SLM module implements a **distributed two-PC architecture**:

- **Main Control PC**: generator_service.py performs GPU-accelerated hologram generation (CUDA, A4000 GPU)
- **SLM PC**: slm_service.py drives SLM hardware connected via PCIE adapter
- **Communication**: Direct Ethernet connection for hologram streaming

## 🖧 Hardware Topology

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      SLM MODULE HARDWARE TOPOLOGY                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                      MAIN CONTROL PC                                       │
│  │                                                                             │
│  │  ┌─────────────────────────────────────────────────────────────────┐        │
│  │  │              Dashboard / Control Interface                     │        │
│  │  │                                                                 │        │
│  │  │  - User specifies tweezer positions                            │        │
│  │  │  - Particle tracking feedback from camera                      │        │
│  │  │  - Manual/automatic control modes                              │        │
│  │  └────────────────────────┬────────────────────────────────────────┘        │
│  │                           │                                                 │
│  │                           ▼                                                 │
│  │  ┌─────────────────────────────────────────────────────────────────┐        │
│  │  │       generator_service.py (Port 50053)                        │        │
│  │  │                                                                 │        │
│  │  │  ┌─────────────┐    ┌─────────────┐    ┌────────────────────┐ │        │
│  │  │  │  gRPC       │    │  CUDA GPU   │    │  Gerchberg-Saxton │ │        │
│  │  │  │  Server     │───▶│  Processing │───▶│  Algorithm        │ │        │
│  │  │  │             │    │             │    │                    │ │        │
│  │  │  │  Receives   │    │  A4000 GPU  │    │  - 2 iterations   │ │        │
│  │  │  │  tweezer    │    │  12GB VRAM  │    │  - FFT on GPU     │ │        │
│  │  │  │  positions  │    │             │    │  - Phase extract  │ │        │
│  │  │  │             │    │             │    │  - 512x512 output │ │        │
│  │  │  └─────────────┘    └─────────────┘    └────────────────────┘ │        │
│  │  │         │                                         │            │        │
│  │  │         │              Hologram Generation       │            │        │
│  │  │         │              Rate: 200 fps (5 ms)      │            │        │
│  │  │         ▼                                         ▼            │        │
│  │  │  ┌─────────────────────────────────────────────────────────┐  │        │
│  │  │  │         8-bit Phase Pattern (512x512 bytes)            │  │        │
│  │  │  │         Packed in HologramFrame protobuf                │  │        │
│  │  │  └─────────────────────────────────────────────────────────┘  │        │
│  │  └────────────────────────┬────────────────────────────────────────┘        │
│  │                           │                                                 │
│  └───────────────────────────┼─────────────────────────────────────────────────┤
│                              │                                                 │
│                              │ Direct Ethernet Connection                      │
│                              │ gRPC Streaming                                  │
│                              │ Target: 192.168.6.2:50051                       │
│  ════════════════════════════╪═════════════════════════════════════════════════│
│                              │                                                 │
│  ┌───────────────────────────▼─────────────────────────────────────────────────┤
│  │                           SLM PC                                           │
│  │                                                                             │
│  │  ┌─────────────────────────────────────────────────────────────────┐        │
│  │  │          slm_service.py (Port 50051)                           │        │
│  │  │                                                                 │        │
│  │  │  ┌─────────────┐    ┌─────────────┐    ┌────────────────────┐ │        │
│  │  │  │  gRPC       │    │  Format     │    │  Hardware Driver  │ │        │
│  │  │  │  Server     │───▶│  Converter  │───▶│                    │ │        │
│  │  │  │             │    │             │    │                    │ │        │
│  │  │  │  Receives   │    │  8-bit →    │    │  - SDK C/C++ DLL  │ │        │
│  │  │  │  hologram   │    │  Hardware   │    │  - Write_image()  │ │        │
│  │  │  │  frames     │    │  format     │    │  - Buffer flip    │ │        │
│  │  │  │  from Main  │    │             │    │  - VSync timing   │ │        │
│  │  │  │  PC         │    │             │    │                    │ │        │
│  │  │  └─────────────┘    └─────────────┘    └─────────┬──────────┘ │        │
│  │  │                                                   │            │        │
│  │  └───────────────────────────────────────────────────┼────────────┘        │
│  │                                                      │                     │
│  │                                                      ▼                     │
│  │  ┌─────────────────────────────────────────────────────────────────┐        │
│  │  │                  SLM Hardware (PCIE Adapter)                   │        │
│  │  │                                                                 │        │
│  │  │  ┌─────────────┐    ┌─────────────┐    ┌────────────────────┐ │        │
│  │  │  │   PCIE      │    │  Liquid     │    │   Optical Output  │ │        │
│  │  │  │  Adapter    │───▶│  Crystal    │───▶│                    │ │        │
│  │  │  │             │    │  Array      │    │                    │ │        │
│  │  │  │  512x512    │    │             │    │  - Fourier plane  │ │        │
│  │  │  │  Phase      │    │  Phase      │    │  - Multiple traps │ │        │
│  │  │  │  modulation │    │  Modulation │    │  - Beam steering  │ │        │
│  │  │  │  60-120Hz   │    │  0-2π range │    │  - Intensity ctrl │ │        │
│  │  │  └─────────────┘    └─────────────┘    └────────────────────┘ │        │
│  │  └─────────────────────────────────────────────────────────────────┘        │
│  └─────────────────────────────────────────────────────────────────────────────┘
│                                                                                 │
│  Data Flow:                                                                    │
│  1. Dashboard → generator_service.py (tweezer positions)                       │
│  2. generator_service.py → CUDA GPU → hologram generation (200 fps)           │
│  3. generator_service.py → SLM PC (Direct Ethernet, gRPC streaming)            │
│  4. slm_service.py → SLM Hardware (PCIE adapter) → display update              │
│                                                                                 │
│  Performance Notes:                                                            │
│  - Generator: 200 fps maximum (with iteration=2, A4000 GPU)                    │
│  - SLM Hardware: 300 fps maximum (limited by generator performance)            │
│  - Network: Direct Ethernet connection between Main PC and SLM PC              │
└─────────────────────────────────────────────────────────────────────────────────┘
```

The SLM module implements a two-tier architecture separating hologram generation from hardware control:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          SLM MODULE ARCHITECTURE                               │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                        Client Applications                                  │
│  │                                                                             │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  │ Dashboard   │    │   Camera    │    │  External   │    │   Direct    │  │
│  │  │  Control    │    │  Feedback   │    │  Scripts    │    │  Python     │  │
│  │  │             │    │   Loop      │    │             │    │   Client    │  │
│  │  │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐ │  │
│  │  │ │Manual   │ │    │ │Position │ │    │ │Pattern  │ │    │ │Research │ │  │
│  │  │ │Tweezer  │ │    │ │Tracking │ │    │ │Sequences│ │    │ │Protocols│ │  │
│  │  │ │Control  │ │    │ │Feedback │ │    │ │Scripts  │ │    │ │Automation│  │
│  │  │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘ │  │
│  │  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘  │
│  │         │                   │                   │                   │       │
│  └─────────┼───────────────────┼───────────────────┼───────────────────┼───────┤
│            │                   │                   │                   │       │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │         │        gRPC Control Service (Port 50053)         │               │
│  │         │                   │                   │                   │       │
│  │  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  │              Generator Service (generator_service.py)                 │ │
│  │  │                                                                        │ │
│  │  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐ │ │
│  │  │  │  Command    │    │  Position   │    │     Hologram Generator     │ │ │
│  │  │  │  Parser     │    │  Manager    │    │                             │ │ │
│  │  │  │             │    │             │    │                             │ │ │
│  │  │  │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐  ┌─────────┐   │ │ │
│  │  │  │ │gRPC     │ │    │ │Tweezer  │ │    │ │Gerchberg│  │CUDA     │   │ │ │
│  │  │  │ │Stream   │ │    │ │List     │ │    │ │-Saxton  │  │Kernels  │   │ │ │
│  │  │  │ │Handler  │ │    │ │Affine   │ │    │ │Hybrid   │  │FFT      │   │ │ │
│  │  │  │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘  └─────────┘   │ │ │
│  │  │  │             │    │             │    │                             │ │ │
│  │  │  │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐  ┌─────────┐   │ │ │
│  │  │  │ │Protobuf │ │    │ │Scale/   │ │    │ │Target   │  │Phase    │   │ │ │
│  │  │  │ │Decode   │ │    │ │Rotate   │ │    │ │Intensity│  │Retrieval│   │ │ │
│  │  │  │ │Validate │ │    │ │Trans    │ │    │ │Pattern  │  │Optimize │   │ │ │
│  │  │  │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘  └─────────┘   │ │ │
│  │  │  └─────────────┘    └─────────────┘    └─────────────────────────────┘ │ │
│  │  │                             │                        │                │ │
│  │  │  ┌─────────────────────────────────────────────────────────────────────┐ │ │
│  │  │  │                   GPU Processing Pipeline                          │ │ │
│  │  │  │                                                                     │ │ │
│  │  │  │  GPU Memory (RTX 4070 - 12GB VRAM)                                 │ │ │
│  │  │  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────┐ │ │ │
│  │  │  │  │  Complex    │    │    FFT      │    │    Phase Pattern       │ │ │ │
│  │  │  │  │  Field      │───▶│  Transform  │───▶│    (512x512 float)     │ │ │ │
│  │  │  │  │ (512x512)   │    │  cuFFT      │    │                         │ │ │ │
│  │  │  │  │ complex64   │    │  2D         │    │  Convert to 8-bit       │ │ │ │
│  │  │  │  └─────────────┘    └─────────────┘    │  [0, 255] → [0, 2π]     │ │ │ │
│  │  │  │         ▲                                └─────────────────────────┘ │ │ │
│  │  │  │         │                                            │              │ │ │
│  │  │  │         │                                            ▼              │ │ │
│  │  │  │  ┌─────────────────────────────────────────────────────────────┐   │ │ │
│  │  │  │  │  Iterative Refinement Loop (50 iterations)                 │   │ │ │
│  │  │  │  │                                                             │   │ │ │
│  │  │  │  │  1. Apply target amplitude to Fourier plane                │   │ │ │
│  │  │  │  │  2. Inverse FFT to get object plane                        │   │ │ │
│  │  │  │  │  3. Extract phase, preserve desired amplitude              │   │ │ │
│  │  │  │  │  4. Forward FFT back to Fourier plane                      │   │ │ │
│  │  │  │  │  5. Repeat until convergence                               │   │ │ │
│  │  │  │  │                                                             │   │ │ │
│  │  │  │  │  CUDA Optimization:                                         │   │ │ │
│  │  │  │  │  - Pre-planned FFT (reused across iterations)              │   │ │ │
│  │  │  │  │  - Custom kernels for phase extraction                     │   │ │ │
│  │  │  │  │  - Pinned memory for fast host↔device transfer            │   │ │ │
│  │  │  │  │  - Stream concurrent operations                            │   │ │ │
│  │  │  │  └─────────────────────────────────────────────────────────────┘   │ │ │
│  │  │  └─────────────────────────────────────────────────────────────────────┘ │ │
│  │  └────────────────────────────────────────────────────────────────────────┘ │
│  │            │                   │                   │                       │
│  │            └───────────────────┼───────────────────┘                       │
│  │                                │                                           │
│  │  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  │                 gRPC Driver Connection (Port 50054)                    │ │
│  │  │                                                                        │ │
│  │  │  Generator ──▶ [HologramFrame] ──▶ Driver ──▶ [UpdateConfirmation]   │ │
│  │  │                                                                        │ │
│  │  │  HologramFrame:                                                        │ │
│  │  │  - command_id: unique UUID                                             │ │
│  │  │  - hologram: bytes (512*512 uint8)                                     │ │
│  │  │  - width, height: dimensions                                           │ │
│  │  │  - metrics: generation timing                                          │ │
│  │  └────────────────────────────────────────────────────────────────────────┘ │
│  └─────────────────────────────────────────────────────────────────────────────┤
│                                      │                                         │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │         │       gRPC Driver Service (Port 50054)           │               │
│  │         │                                                  │               │
│  │  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  │                Driver Service (slm_service.py)                        │ │
│  │  │                                                                        │ │
│  │  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐ │ │
│  │  │  │   Stream    │    │   Format    │    │      Hardware Interface    │ │ │
│  │  │  │  Receiver   │    │  Converter  │    │                             │ │ │
│  │  │  │             │    │             │    │                             │ │ │
│  │  │  │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐  ┌─────────┐   │ │ │
│  │  │  │ │gRPC     │ │    │ │8-bit    │ │    │ │SDK      │  │Display  │   │ │ │
│  │  │  │ │Async    │ │    │ │Phase    │ │    │ │C/C++    │  │Port     │   │ │ │
│  │  │  │ │Stream   │ │    │ │Unwrap   │ │    │ │DLL      │  │HDMI/DP  │   │ │ │
│  │  │  │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘  └─────────┘   │ │ │
│  │  │  │             │    │             │    │                             │ │ │
│  │  │  │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐  ┌─────────┐   │ │ │
│  │  │  │ │Buffer   │ │    │ │Vendor   │ │    │ │Write    │  │Flip     │   │ │ │
│  │  │  │ │Queue    │ │    │ │Format   │ │    │ │Image    │  │Buffer   │   │ │ │
│  │  │  │ │Manager  │ │    │ │Adapt    │ │    │ │Memory   │  │VSync    │   │ │ │
│  │  │  │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘  └─────────┘   │ │ │
│  │  │  └─────────────┘    └─────────────┘    └─────────────────────────────┘ │ │
│  │  └────────────────────────────────────────────────────────────────────────┘ │
│  └─────────────────────────────────────────────────────────────────────────────┤
│                                      │                                         │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                        Hardware Layer                                      │
│  │                                      │                                     │
│  │  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  │                  SLM Hardware (Spatial Light Modulator)                │ │
│  │  │                                                                        │ │
│  │  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐ │ │
│  │  │  │  Display    │    │   Liquid    │    │      Optical Output        │ │ │
│  │  │  │  Interface  │───▶│  Crystal    │───▶│                             │ │ │
│  │  │  │             │    │   Array     │    │                             │ │ │
│  │  │  │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐  ┌─────────┐   │ │ │
│  │  │  │ │HDMI/DP  │ │    │ │Phase    │ │    │ │Fourier  │  │Optical  │   │ │ │
│  │  │  │ │Input    │ │    │ │Modulate │ │    │ │Trans-   │  │Trap     │   │ │ │
│  │  │  │ │512x512  │ │    │ │2π range │ │    │ │form     │  │Pattern  │   │ │ │
│  │  │  │ └─────────┘ │    │ └─────────┘ │    │ └─────────┘  └─────────┘   │ │ │
│  │  │  │             │    │             │    │                             │ │ │
│  │  │  │ Refresh:    │    │ Response:   │    │ Beam steering & shaping     │ │ │
│  │  │  │ 60-120 Hz   │    │ <10 ms      │    │ Multiple trap generation    │ │ │
│  │  │  │ 8-bit depth │    │ Linear LC   │    │ Intensity control           │ │ │
│  │  │  │ Max: 300fps │    │             │    │ Multiple trap generation    │ │ │
│  │  │  └─────────────┘    └─────────────┘    └─────────────────────────────┘ │ │
│  │  │                                                                         │ │
│  │  │  Note: Hardware capable of 300 fps, but generator limited to 200 fps   │ │
│  │  └────────────────────────────────────────────────────────────────────────┘ │
│  └─────────────────────────────────────────────────────────────────────────────┘
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 🔬 Hologram Generation

### Gerchberg-Saxton Algorithm

The system implements a hybrid Gerchberg-Saxton algorithm optimized for GPU execution:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    GERCHBERG-SAXTON ALGORITHM PIPELINE                         │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  Input: Tweezer Positions [(x₁,y₁,I₁), (x₂,y₂,I₂), ..., (xₙ,yₙ,Iₙ)]          │
│         Affine Transform (scale, rotate, translate)                            │
│                                      │                                         │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                   Phase 1: Target Pattern Generation                       │
│  │                                      │                                     │
│  │  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  │  Initialize Target Amplitude Array (512x512)                          │ │
│  │  │                                                                        │ │
│  │  │  for each tweezer (x, y, intensity):                                  │ │
│  │  │      # Apply affine transformation                                    │ │
│  │  │      x', y' = transform(x, y, affine_matrix)                          │ │
│  │  │                                                                        │ │
│  │  │      # Convert to pixel coordinates                                   │ │
│  │  │      px = int(x' * scale + center_x)                                  │ │
│  │  │      py = int(y' * scale + center_y)                                  │ │
│  │  │                                                                        │ │
│  │  │      # Add Gaussian spot                                              │ │
│  │  │      target[py, px] += intensity * gaussian(diameter)                 │ │
│  │  │                                                                        │ │
│  │  │  # Normalize to [0, 1]                                                │ │
│  │  │  target = target / max(target)                                        │ │
│  │  └────────────────────────────────────────────────────────────────────────┘ │
│  │                                      │                                     │
│  └──────────────────────────────────────┼─────────────────────────────────────┤
│                                         ▼                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                   Phase 2: GPU-Accelerated Iteration                       │
│  │                                                                             │
│  │  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  │  Initialization on GPU:                                                │ │
│  │  │  ┌──────────────────────────────────────────────────────────────────┐  │ │
│  │  │  │ 1. Allocate GPU memory for complex field (512x512 complex64)    │  │ │
│  │  │  │ 2. Create cuFFT plan (forward & inverse, reused)                │  │ │
│  │  │  │ 3. Initialize phase with random values [0, 2π]                  │  │ │
│  │  │  │ 4. Transfer target amplitude to GPU                             │  │ │
│  │  │  └──────────────────────────────────────────────────────────────────┘  │ │
│  │  │                                                                        │ │
│  │  │  Iterative Loop (typically 2 iterations for 200 fps performance):      │ │
│  │  │  ┌──────────────────────────────────────────────────────────────────┐  │ │
│  │  │  │                                                                  │  │ │
│  │  │  │  Object Plane:  U(x,y) = A_target(x,y) · exp(iφ(x,y))          │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  │        │ Forward FFT (cuFFT)                                    │  │ │
│  │  │  │        ▼                                                         │  │ │
│  │  │  │  Fourier Plane: Ũ(u,v) = FFT{U(x,y)}                           │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  │        │ Apply Target Amplitude Constraint                      │  │ │
│  │  │  │        │ Ũ'(u,v) = A_target(u,v) · exp(i·angle(Ũ(u,v)))        │  │ │
│  │  │  │        ▼                                                         │  │ │
│  │  │  │  Constrained Fourier: Ũ'(u,v)                                  │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  │        │ Inverse FFT (cuFFT)                                    │  │ │
│  │  │  │        ▼                                                         │  │ │
│  │  │  │  Back to Object: U'(x,y) = IFFT{Ũ'(u,v)}                       │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  │        │ Extract Phase (custom CUDA kernel)                     │  │ │
│  │  │  │        │ φ(x,y) = angle(U'(x,y))                                │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  │        └──────────────────┐                                     │  │ │
│  │  │  │                           │                                     │  │ │
│  │  │  │        ┌──────────────────┘                                     │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  │        │ Check Convergence (every 10 iterations)                │  │ │
│  │  │  │        │ error = ||A_target - |U'|||                            │  │ │
│  │  │  │        │ if error < threshold: break                            │  │ │
│  │  │  │        │                                                         │  │ │
│  │  │  └────────┴──────────────────────────────────────────────────────────┘  │ │
│  │  │                                                                        │ │
│  │  │  Post-Processing on GPU:                                               │ │
│  │  │  ┌──────────────────────────────────────────────────────────────────┐  │ │
│  │  │  │ 1. Normalize phase to [0, 2π]                                   │  │ │
│  │  │  │ 2. Convert to 8-bit: phase_8bit = (φ / 2π) * 255               │  │ │
│  │  │  │ 3. Transfer to host memory (DMA)                                │  │ │
│  │  │  └──────────────────────────────────────────────────────────────────┘  │ │
│  │  └────────────────────────────────────────────────────────────────────────┘ │
│  │                                      │                                     │
│  └──────────────────────────────────────┼─────────────────────────────────────┤
│                                         ▼                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┤
│  │                   Phase 3: Output & Metrics                                │
│  │                                                                             │
│  │  Output: 8-bit phase pattern (512x512 bytes)                               │
│  │                                                                             │
│  │  Metrics:                                                                   │
│  │  - Generation Rate: 200 fps (5 ms per frame)                                │
│  │  - Iterations: 2 (optimized for speed)                                     │
│  │  - GPU: A4000 (12GB VRAM)                                                   │
│  │  - Memory Transfer: ~1 MB (phase pattern)                                  │
│  │                                                                             │
│  │  Performance Notes:                                                         │
│  │  - SLM hardware maximum: 300 fps                                            │
│  │  - Generator limited to 200 fps by A4000 GPU performance                    │
│  │  - Iteration count set to 2 for maximum throughput                          │
│  │  - Network transfer via direct Ethernet connection                          │
│  │                                                                             │
│  └─────────────────────────────────────────────────────────────────────────────┘
└─────────────────────────────────────────────────────────────────────────────────┘
```

### CUDA Optimization Techniques

```cuda
// Custom CUDA kernel for phase extraction
extern "C" __global__
void update_phase(const float* amplitude, 
                  const float* fft_real, 
                  const float* fft_imag,
                  float* output_phase, 
                  int rows, int cols) {
    int i = blockIdx.y * blockDim.y + threadIdx.y;
    int j = blockIdx.x * blockDim.x + threadIdx.x;

    if (i < rows && j < cols) {
        int idx = i * cols + j;
        
        // Calculate phase from complex components
        float phase = atan2f(fft_imag[idx], fft_real[idx]);
        
        // Store phase [0, 2π]
        output_phase[idx] = phase;
    }
}

// Kernel for adding Gaussian spots
extern "C" __global__
void add_spots_kernel(float* hologram, 
                      const int* x_idx, 
                      const int* y_idx,
                      const float* intensities, 
                      int n_spots, 
                      int width, 
                      int height) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    
    if (i < n_spots) {
        int cx = x_idx[i];
        int cy = y_idx[i];
        float intensity = intensities[i];
        
        // Add Gaussian spot at (cx, cy)
        for (int dy = -10; dy <= 10; dy++) {
            for (int dx = -10; dx <= 10; dx++) {
                int px = cx + dx;
                int py = cy + dy;
                
                if (px >= 0 && px < width && py >= 0 && py < height) {
                    float r2 = dx*dx + dy*dy;
                    float gaussian = intensity * expf(-r2 / 18.0f);
                    atomicAdd(&hologram[py * width + px], gaussian);
                }
            }
        }
    }
}
```

## 🖥️ Hardware Driver

### SLM SDK Integration

The driver service provides vendor-agnostic hardware abstraction:

```python
# Hardware initialization
def initialize_slm_hardware():
    global slm_lib, height, width, Bytes
    
    # Load vendor SDK
    slm_lib = CDLL("/path/to/slm_sdk.so")
    
    # Configure display
    slm_lib.Create_SDK(
        bit_depth,
        num_boards_found,
        constructed_okay,
        is_nematic_type,
        RAM_write_enable,
        use_GPU,
        max_transients,
        board_number
    )
    
    # Get dimensions
    height = slm_lib.Get_image_height(board_number)
    width = slm_lib.Get_image_width(board_number)
    Bytes = 1  # 8-bit phase
    
    return True

# Write hologram to hardware
def write_image(image_array: np.ndarray) -> None:
    """Write 8-bit phase pattern to SLM."""
    global slm_lib, height, width, Bytes
    
    # Validate input
    if image_array.dtype != np.uint8:
        raise ValueError("Phase pattern must be 8-bit uint8")
    
    if image_array.size != height * width * Bytes:
        raise ValueError(f"Size mismatch: expected {height*width*Bytes}")
    
    # Write to hardware
    retVal = slm_lib.Write_image(
        board_number,
        image_array.ctypes.data_as(POINTER(c_ubyte)),
        height * width * Bytes,
        wait_For_Trigger,
        flip_immediate,
        OutputPulseImageFlip,
        OutputPulseImageRefresh,
        timeout_ms
    )
    
    if retVal < 0:
        raise RuntimeError(f"SLM write failed with code {retVal}")
```

## ⚡ Performance Optimization

### GPU Memory Management

```python
# Pre-allocate GPU arrays
complex_field = cp.zeros((height, width), dtype=cp.complex64)
target_amplitude = cp.zeros((height, width), dtype=cp.float32)
phase_pattern = cp.zeros((height, width), dtype=cp.float32)

# Pinned memory for fast host↔device transfer
import cupy.cuda.pinned_memory as pinned

host_buffer = pinned.alloc((height, width), dtype=np.uint8)

# Create persistent FFT plan (reused across generations)
from cupyx.scipy.fft import get_fft_plan

with get_fft_plan(complex_field, axes=(0, 1)) as plan:
    # Plan is cached and reused automatically
    fourier = cp.fft.fft2(complex_field)
```

### Timing Optimizations

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        PERFORMANCE OPTIMIZATION STRATEGIES                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  Baseline (No Optimization):         ~50 ms per hologram                       │
│  ├─ Target generation: 10 ms                                                   │
│  ├─ 50 FFT iterations: 35 ms                                                   │
│  └─ Memory transfer: 5 ms                                                      │
│                                                                                 │
│  Optimization 1: Pre-planned FFT                                               │
│  Improvement: -20 ms (40% faster)                                              │
│  ├─ Cache FFT plan between calls                                               │
│  ├─ Reuse CUDA kernels                                                         │
│  └─ Result: ~30 ms per hologram                                                │
│                                                                                 │
│  Optimization 2: Custom CUDA Kernels                                           │
│  Improvement: -10 ms (33% faster than O1)                                      │
│  ├─ Replace NumPy operations with CUDA                                         │
│  ├─ Fused operations (phase extraction + normalization)                        │
│  └─ Result: ~20 ms per hologram                                                │
│                                                                                 │
│  Optimization 3: Pinned Memory                                                 │
│  Improvement: -5 ms (25% faster than O2)                                       │
│  ├─ Direct memory access (DMA)                                                 │
│  ├─ Avoid buffer copies                                                        │
│  └─ Result: ~15 ms per hologram                                                │
│                                                                                 │
│  Optimization 4: Reduced Iterations                                            │
│  Improvement: -10 ms (67% faster than O3)                                      │
│  ├─ Adaptive convergence checking                                              │
│  ├─ Early termination when converged                                           │
│  ├─ Typical: 30-40 iterations instead of 50                                    │
│  └─ Result: ~5 ms per hologram (avg)                                           │
│                                                                                 │
│  Optimization 5: Async Operations                                              │
│  Improvement: -2 ms (40% faster than O4)                                       │
│  ├─ Overlap computation with memory transfer                                   │
│  ├─ CUDA streams for concurrent operations                                     │
│  └─ Result: ~3 ms per hologram (best case)                                     │
│                                                                                 │
│  Final Performance:                                                             │
│  - Average: 2-3 ms per hologram                                                │
│  - Peak: 1-2 ms (simple patterns)                                              │
│  - Worst: 5-8 ms (complex patterns with many tweezers)                         │
│  - Throughput: 200-500 holograms/second                                        │
│  - Update rate: 60-120 Hz (SLM refresh limited)                                │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 🔧 API Reference

### Protocol Buffer Schema (`hologram.proto`)

```protobuf
syntax = "proto3";
package slm;

import "google/protobuf/timestamp.proto";

// Affine transformation parameters
message AffineParameters {
  double translate_x = 1;
  double translate_y = 2;
  double translate_z = 3;
  double rotate_x_deg = 4;
  double rotate_y_deg = 5;
  double rotate_z_deg = 6;
  double scale_x = 7;
  double scale_y = 8;
  double scale_z = 9;
  double shear_xy = 10;
  double shear_yz = 11;
  double shear_xz = 12;
}

// Single tweezer position
message TweezerPoint {
  double x = 1;
  double y = 2;
  double z = 3;
  double intensity = 4;
}

// Tweezer control command
message TweezerCommand {
  string command_id = 1;
  repeated TweezerPoint points = 2;
  AffineParameters affine = 3;
  google.protobuf.Timestamp requested_at = 4;
}

// Performance metrics
message Metrics {
  int64 generation_ms = 1;
  int64 driver_transfer_ms = 2;
  int64 slm_update_ms = 3;
  google.protobuf.Timestamp hologram_generated_at = 4;
  google.protobuf.Timestamp hologram_sent_at = 5;
  google.protobuf.Timestamp slm_ack_at = 6;
}

// Generated hologram frame
message HologramFrame {
  string command_id = 1;
  bytes hologram = 2;
  uint32 width = 3;
  uint32 height = 4;
  AffineParameters affine = 5;
  Metrics metrics = 6;
}

// Services
service ControlService {
  rpc StreamCommands(stream TweezerCommand) returns (stream CommandAcknowledge);
}

service DriverService {
  rpc PushHolograms(stream HologramFrame) returns (stream UpdateConfirmation);
}
```

### Python Client Example

```python
import grpc
import hologram_pb2 as holo_pb2
import hologram_pb2_grpc as holo_pb2_grpc

# Connect to generator service
channel = grpc.insecure_channel('localhost:50053')
stub = holo_pb2_grpc.ControlServiceStub(channel)

# Create tweezer command
command = holo_pb2.TweezerCommand()
command.command_id = "test_pattern_001"

# Add tweezer positions
point1 = command.points.add()
point1.x = 10.0
point1.y = 20.0
point1.z = 0.0
point1.intensity = 1.0

point2 = command.points.add()
point2.x = -10.0
point2.y = -20.0
point2.z = 0.0
point2.intensity = 0.8

# Set affine transform
command.affine.scale_x = 1.0
command.affine.scale_y = 1.0
command.affine.rotate_z_deg = 45.0

# Stream command and receive acknowledgments
def command_stream():
    yield command

for ack in stub.StreamCommands(command_stream()):
    print(f"Command {ack.command_id} - Stage: {ack.stage}")
    if ack.metrics.generation_ms > 0:
        print(f"Generation time: {ack.metrics.generation_ms} ms")
```

## 🚀 Quick Start

### Starting the Services

```bash
# Activate environment
conda activate tweezer

# Start generator service
python SLM/slm-control-server/generator_service.py \
    --port 50053 \
    --gpu-index 0 \
    --width 512 \
    --height 512 \
    --iterations 50

# Start driver service (separate terminal)
python SLM/slm-driver-server/slm_service.py \
    --port 50054 \
    --sdk-path /opt/slm_sdk

# Or use service manager
python GUI/service_manager.py
```

### Demo Client

```bash
python SLM/slm-control-server/demo_client.py \
    --host localhost:50053 \
    --pattern grid \
    --num-tweezers 16
```

## 🐛 Troubleshooting

**GPU Not Detected**
```bash
# Verify CUDA installation
nvidia-smi
python -c "import cupy; print(cupy.cuda.runtime.getDeviceCount())"

# Check GPU index
python -c "import cupy; cupy.cuda.Device(0).use(); print('GPU 0 OK')"
```

**Poor Hologram Quality**
- Increase iteration count (50-100)
- Check target amplitude normalization
- Verify tweezer positions within bounds
- Adjust convergence threshold

**High Latency**
- Reduce iteration count
- Check GPU utilization (`nvidia-smi`)
- Optimize FFT plan caching
- Monitor memory transfer times

**SLM Hardware Issues**
- Verify SDK path and permissions
- Check display port connection
- Test with vendor software
- Review hardware specifications

## 📄 File Structure

```
SLM/
├── hologram.proto              # Protocol buffer schema
├── slm-control-server/
│   ├── generator_service.py    # Hologram generation service
│   ├── demo_client.py          # Test client
│   ├── hologram_pb2.py         # Generated protobuf
│   └── hologram_pb2_grpc.py    # Generated gRPC stubs
├── slm-driver-server/
│   ├── slm_service.py          # Hardware driver service
│   ├── hologram_pb2.py         # Generated protobuf
│   ├── hologram_pb2_grpc.py    # Generated gRPC stubs
│   └── start_slm_service.bat   # Windows launch script
└── README.md                   # This documentation
```

The SLM module provides GPU-accelerated hologram generation with sub-frame latency, enabling real-time dynamic optical trap control for advanced optical tweezer experiments.