# Architecture Diagram

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Tweezer System                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐  │
│  │   Camera     │─────▶│ImageServer   │◀────▶│  Dashboard   │  │
│  │  Hardware    │      │with Tracking │      │     GUI      │  │
│  └──────────────┘      └──────────────┘      └──────────────┘  │
│                               │                                  │
│                               │ gRPC                            │
│                               │ (port 50052)                    │
│                               │                                  │
│                               ▼                                  │
│                        ┌──────────────┐                         │
│                        │  Live g(r)   │                         │
│                        │  Visualizer  │ ◀─── You are here!     │
│                        └──────────────┘                         │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Live g(r) Internal Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                     LiveGrVisualizer Class                          │
├────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────────┐                                            │
│  │   gRPC Client      │  Connects to ImageServer                   │
│  │   (ImageExchange   │  Fetches tracking data                     │
│  │   Stub)            │  (x, y positions)                          │
│  └─────────┬──────────┘                                            │
│            │                                                        │
│            │ Raw particle positions                                │
│            ▼                                                        │
│  ┌────────────────────┐                                            │
│  │  GrCalculator      │  Computes pair correlation                │
│  │                    │  - Calls trackpy.pair_correlation_2d       │
│  │                    │  - Converts pixels → σ                     │
│  │                    │  - Filters short-range noise               │
│  └─────────┬──────────┘                                            │
│            │                                                        │
│            │ r, g(r) arrays                                        │
│            ▼                                                        │
│  ┌────────────────────┐                                            │
│  │ TrackingDataBuffer │  Manages data & averaging                 │
│  │                    │  - Stores particle positions               │
│  │                    │  - Maintains g(r) history (deque)         │
│  │                    │  - Calculates time-averaged g(r)          │
│  │                    │  - Computes error bands (std dev)         │
│  └─────────┬──────────┘                                            │
│            │                                                        │
│            │ Averaged data                                         │
│            ▼                                                        │
│  ┌────────────────────┐                                            │
│  │  Matplotlib        │  Real-time visualization                  │
│  │  FuncAnimation     │  - Update particles scatter                │
│  │                    │  - Update g(r) line plot                  │
│  │                    │  - Update statistics text                 │
│  │                    │  Non-blocking updates                     │
│  └────────────────────┘                                            │
│                                                                      │
└────────────────────────────────────────────────────────────────────┘
```

## Data Flow

```
ImageServer                                                Live g(r)
━━━━━━━━━━━                                               ━━━━━━━━━━

   Camera                                                    
     │                                                       
     ▼                                                       
┌──────────┐                                                
│  TIFF    │                                                
│  Frame   │                                                
└────┬─────┘                                                
     │                                                       
     ▼                                                       
┌──────────┐                                                
│ trackpy  │                                                
│ locate() │                                                
└────┬─────┘                                                
     │                                                       
     ▼                                                       
┌──────────┐           gRPC GetLatestTracks()       ┌──────────────┐
│Detections│  ─────────────────────────────────────▶│  Fetch Data  │
│{x,y,mass}│  ◀─────────────────────────────────────│   (polling)  │
└──────────┘                                         └──────┬───────┘
                                                            │
                                                            ▼
                                                     ┌──────────────┐
                                                     │   Convert    │
                                                     │ to DataFrame │
                                                     └──────┬───────┘
                                                            │
                                                            ▼
                                                     ┌──────────────┐
                                                     │  Calculate   │
                                                     │    g(r)      │
                                                     └──────┬───────┘
                                                            │
                                                            ▼
                                                     ┌──────────────┐
                                                     │   Buffer &   │
                                                     │   Average    │
                                                     └──────┬───────┘
                                                            │
                                                            ▼
                                                     ┌──────────────┐
                                                     │   Display    │
                                                     │  (matplotlib)│
                                                     └──────────────┘
```

## Display Layout

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                      Live g(r) Visualizer                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┯━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃                                │                                  ┃
┃  ┌─────────────────────────┐  │  ┌───────────────────────────┐  ┃
┃  │   Particle Positions    │  │  │                           │  ┃
┃  │                         │  │  │   g(r) Correlation        │  ┃
┃  │   • • •  • •            │  │  │                           │  ┃
┃  │  • • • • •   •          │  │  │      ╱╲                   │  ┃
┃  │   • •   • • •           │  │  │     ╱  ╲____              │  ┃
┃  │  • • •    •  •          │  │  │    ╱        ────────      │  ┃
┃  │ • •  • •   • •          │  │  │   ╱                ────   │  ┃
┃  │  • • • • •   •          │  │  │  ╱                      ──│  ┃
┃  │   •  • • • •            │  │  │ ╱                         │  ┃
┃  │  • •  •   • •           │  │  │╱                          │  ┃
┃  │                         │  │  │                           │  ┃
┃  │  0───────────1456       │  │  │  0────────r(σ)──────▶    │  ┃
┃  └─────────────────────────┘  │  └───────────────────────────┘  ┃
┃                                │                                  ┃
┃  ┌─────────────────────────┐  │  - Cyan line: averaged g(r)     ┃
┃  │    Statistics Panel     │  │  - Shaded: ±1 std deviation     ┃
┃  │                         │  │  - Dashed: g(r) = 1 reference   ┃
┃  │  ╔═══════════════════╗  │  │                                  ┃
┃  │  ║  Particles:   247 ║  │  │                                  ┃
┃  │  ║  Frames:     1432 ║  │  │                                  ┃
┃  │  ║  FPS:       25.43 ║  │  │                                  ┃
┃  │  ║  Uptime:    56.3s ║  │  │                                  ┃
┃  │  ║  Status: Connected║  │  │                                  ┃
┃  │  ╚═══════════════════╝  │  │                                  ┃
┃  │                         │  │                                  ┃
┃  └─────────────────────────┘  │                                  ┃
┃                                │                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┷━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  Connected to localhost:50052 • History: 20 frames • 10 Hz      ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

## Component Interaction Sequence

```
User              Visualizer       Buffer         Calculator      Server
  │                   │              │                │              │
  │  [Start]          │              │                │              │
  ├──────────────────▶│              │                │              │
  │                   │              │                │              │
  │                   │  connect()   │                │              │
  │                   ├──────────────┼────────────────┼─────────────▶│
  │                   │              │                │              │
  │                   │◀─────────────┼────────────────┼──────────────┤
  │                   │              │                │      [ACK]   │
  │                   │              │                │              │
  │                   │  fetch()     │                │              │
  │                   ├──────────────┼────────────────┼─────────────▶│
  │                   │              │                │              │
  │                   │◀─────────────┼────────────────┼──────────────┤
  │                   │              │                │   positions  │
  │                   │              │                │              │
  │                   │ compute_gr() │                │              │
  │                   ├──────────────┼───────────────▶│              │
  │                   │              │                │              │
  │                   │◀─────────────┼────────────────┤              │
  │                   │              │     r, g(r)    │              │
  │                   │              │                │              │
  │                   │  update()    │                │              │
  │                   ├──────────────▶│                │              │
  │                   │              │                │              │
  │                   │  get_avg()   │                │              │
  │                   ├──────────────▶│                │              │
  │                   │              │                │              │
  │                   │◀─────────────┤                │              │
  │                   │  avg r, g(r) │                │              │
  │                   │              │                │              │
  │  [Display]        │              │                │              │
  │◀──────────────────┤              │                │              │
  │                   │              │                │              │
  │  [Repeat @ 10Hz]  │              │                │              │
  │                   │              │                │              │
```

## Utility Functions Architecture

```
utils.py
━━━━━━━━

┌─────────────────────────────────────────────────────────┐
│                    Data Export                           │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  save_gr_data()         CSV with metadata               │
│  export_session_data()  Complete session dump           │
│                                                          │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                  Structure Analysis                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  calculate_structure_metrics()  Coordination, peaks     │
│  print_structure_report()       Formatted output        │
│                                                          │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                  Time Series Analysis                    │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  detect_phase_transition()  Monitor for changes         │
│                                                          │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                     Visualization                        │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  create_comparison_plot()  Multi-dataset comparison     │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## File Dependencies

```
live-gr.py
    ├─ numpy
    ├─ pandas
    ├─ matplotlib
    ├─ trackpy
    ├─ scipy (for density calc)
    ├─ grpc
    └─ image_exchange_pb2_grpc (from Camera/)

utils.py
    ├─ numpy
    ├─ pandas
    ├─ matplotlib
    └─ json/csv (stdlib)

example_usage.py
    ├─ live-gr.py
    └─ utils.py
```

---

This architecture provides:
- ✅ Clean separation of concerns
- ✅ Reusable components
- ✅ Easy testing
- ✅ Maintainable codebase
- ✅ Extensible design
