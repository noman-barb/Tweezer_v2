# Objective Temperature Controller

A comprehensive temperature control system for maintaining objective temperature using a Peltier-based heating/cooling system controlled via Arduino.

## Features

- **PI Controller**: Proportional-Integral controller with configurable gains and deadband (±0.1°C default)
- **Dual Mode**: Automatic switching between heating and cooling modes with 10-second minimum delay to prevent rapid cycling
- **Remote Telemetry**: Fetches objective temperature from remote telemetry server (10.0.63.195:9002)
- **Arduino Control**: Controls Arduino via serial (COM4) with PWM output (0-0.4 range) and digital pin for mode switching
- **Web GUI**: Beautiful, responsive web interface showing:
  - Real-time objective temperature, setpoint, room temperature, and humidity
  - Live time-series plots for temperature vs setpoint
  - Control output visualization (PWM and heating/cooling mode)
  - Interactive setpoint adjustment
- **Data Logging**: Persistent CSV logging with historical data loading and gap detection
- **HTTP API**: No-cache HTTP GET endpoints for remote control and monitoring

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Temperature Controller                        │
│                                                                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │   Telemetry  │───▶│      PI      │───▶│   Arduino    │      │
│  │    Client    │    │  Controller  │    │  Controller  │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                    │                    │              │
│         │                    │                    │              │
│         ▼                    ▼                    ▼              │
│  ┌──────────────────────────────────────────────────────┐      │
│  │              Data Logger (CSV)                        │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                   │
│  ┌──────────────────────────────────────────────────────┐      │
│  │           HTTP Server + Web GUI (Port 9003)          │      │
│  └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

## Hardware Setup

### Arduino Connection
- **Port**: COM4
- **PWM Output**: Pin 9 (62.5 kHz PWM, duty cycle 0.0-0.4)
- **Mode Control**: Pin 7
  - **LOW**: Cooling mode (Peltier cools)
  - **HIGH**: Heating mode (Peltier heats)

### Peltier System
- Liquid circulation around objective through collar at constant flow rate
- PIN7 LOW: Peltier cools the circulating liquid
- PIN7 HIGH: Peltier reverses and heats the liquid

## Installation

1. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Upload Arduino sketch** (provided in requirements) to Arduino Uno/Nano/Pro Mini

3. **Ensure telemetry server is running** on 10.0.63.195:9002

## Usage

### Basic Usage

Run with default settings:
```bash
python objective-temperature-controller.py
```

Then open your browser to: **http://localhost:9003**

### Advanced Configuration

```bash
python objective-temperature-controller.py \
    --arduino-port COM4 \
    --telemetry-url http://10.0.63.195:9002 \
    --setpoint 25.0 \
    --kp 0.05 \
    --ki 0.01 \
    --deadband 0.1 \
    --control-interval 1.0 \
    --http-host 0.0.0.0 \
    --http-port 9003 \
    --log-dir ./logs \
    --log-level INFO
```

### Command Line Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--arduino-port` | COM4 | Arduino serial port |
| `--telemetry-url` | http://10.0.63.195:9002 | Telemetry server URL |
| `--log-dir` | ./logs | Directory for log files |
| `--setpoint` | 25.0 | Initial temperature setpoint (°C) |
| `--kp` | 0.05 | Proportional gain |
| `--ki` | 0.01 | Integral gain |
| `--deadband` | 0.1 | Temperature tolerance ±°C |
| `--control-interval` | 1.0 | Control loop interval (seconds) |
| `--http-host` | 0.0.0.0 | HTTP server bind address |
| `--http-port` | 9003 | HTTP server port |
| `--log-level` | INFO | Logging verbosity |

## HTTP API

### Get Current Data
```
GET http://localhost:9003/api/current
```

Returns:
```json
{
  "timestamp": "2025-11-04T12:34:56.789Z",
  "objective_temp": 25.12,
  "setpoint": 25.0,
  "pwm": 0.045,
  "mode": "HEATING",
  "room_temp": 23.5,
  "humidity": 45.2
}
```

### Get/Set Setpoint
```
GET http://localhost:9003/api/setpoint
GET http://localhost:9003/api/setpoint?value=26.5
```

### Get Historical Data
```
GET http://localhost:9003/api/history
```

Returns all logged data points from CSV files and current buffer.

### Web GUI
```
GET http://localhost:9003/
```

## Control Algorithm

### PI Controller
- **Proportional term**: Responds to current error (Kp × |error|)
- **Integral term**: Responds to accumulated error over time (Ki × ∫error dt)
- **Anti-windup**: Integral term is clamped to prevent excessive accumulation
- **Deadband**: ±0.1°C tolerance zone where control output is zero

### Mode Switching
- **Heating mode**: When temperature < setpoint - deadband
- **Cooling mode**: When temperature > setpoint + deadband
- **OFF mode**: When within deadband
- **Minimum delay**: 10 seconds between mode switches to protect Peltier

### PWM Range
- Output is limited to 0.0-0.4 (0% to 40% duty cycle)
- This prevents excessive power and allows gradual control

## Data Logging

### Log File Format
CSV files with timestamp-based filenames: `objective_temp_log_YYYYMMDD_HHMMSS.csv`

Columns:
- `timestamp`: ISO 8601 timestamp
- `objective_temp`: Objective temperature (°C)
- `setpoint`: Target temperature (°C)
- `pwm`: PWM duty cycle (0.0-0.4)
- `mode`: HEATING, COOLING, or OFF
- `room_temp`: Ambient temperature (°C)
- `humidity`: Relative humidity (%RH)
- `error`: Error messages (if any)

### Historical Data
- GUI automatically loads all historical log files on startup
- Data gaps are handled by Chart.js (breaks in line plots)
- Maximum 600 points displayed in GUI (10 minutes at 1 Hz)

## Web GUI Features

### Status Display
- **Objective Temperature**: Current temperature with ±0.01°C precision
- **Setpoint**: Target temperature
- **Room Temperature**: Ambient temperature from telemetry
- **Humidity**: Relative humidity from telemetry
- **Control Mode**: Visual indicator (red=heating, cyan=cooling, gray=off)
- **PWM Output**: Current PWM duty cycle

### Charts
1. **Temperature vs Time**
   - Objective temperature (blue line)
   - Setpoint (red dashed line)
   - Automatic time scale with minute resolution

2. **Control Output vs Time**
   - PWM output (cyan, left Y-axis, 0-0.5 scale)
   - Mode indicator (gray, right Y-axis, -1=cooling, 0=off, 1=heating)

### Setpoint Control
- Interactive input field with ±0.1°C precision
- Range: 15-35°C
- Real-time update via HTTP API

### Auto-Refresh
- Updates every 1 second
- No-cache headers ensure fresh data
- Responsive design for desktop and mobile

## Tuning Guide

### PID Gains
Start with default values and adjust based on system response:

**Proportional Gain (Kp)**:
- Too low: Slow response, may not reach setpoint
- Too high: Oscillations, overshoot
- **Default**: 0.05

**Integral Gain (Ki)**:
- Too low: Steady-state error, slow to eliminate offset
- Too high: Overshoot, instability
- **Default**: 0.01

### Deadband
- Controls how tightly temperature is maintained
- Smaller deadband = more switching, tighter control
- Larger deadband = less switching, more temperature variation
- **Default**: ±0.1°C

### Control Interval
- How often the control loop runs
- Faster = more responsive, more computational load
- Slower = less responsive, more stable
- **Default**: 1.0 second

## Troubleshooting

### Arduino Not Connecting
- Check COM port (Device Manager on Windows)
- Ensure no other program is using the port
- Verify Arduino is powered and USB cable is connected
- Try unplugging and replugging USB cable

### No Temperature Data
- Verify telemetry server is running: http://10.0.63.195:9002/telemetry
- Check network connectivity to remote server
- Ensure firewall allows connections
- Check telemetry server logs

### Temperature Oscillations
- Reduce Kp gain (try 0.03)
- Reduce Ki gain (try 0.005)
- Increase deadband (try 0.2°C)
- Increase mode switch delay (edit source code)

### GUI Not Loading
- Check HTTP server is running on port 9003
- Try accessing from localhost: http://localhost:9003
- Check firewall settings
- View browser console for JavaScript errors

### Frequent Mode Switching
- Increase deadband
- Increase mode switch delay in source code (default 10 seconds)
- Check for noise in temperature readings
- Verify Peltier system is working correctly

## Safety Considerations

1. **Temperature Limits**: The system does not have hard temperature limits. Add safety checks if needed.
2. **Peltier Protection**: 10-second minimum delay between mode switches protects Peltier from rapid cycling
3. **PWM Limits**: Maximum PWM is 0.4 (40%) to prevent excessive power
4. **Emergency Stop**: Press Ctrl+C to stop the controller (sets PWM to 0)

## License

Part of the Tweezer project. See main repository for license information.

## Author

Created for the Tweezer optical tweezer system at the Barb Lab.

## Version History

- **v1.0** (2025-11-04): Initial release with PI control, web GUI, and data logging
