# Arduino Due Telemetry HTTP Server v2

This server provides HTTP access to real-time telemetry data from the Arduino Due via gRPC streaming.

## Features

- **Auto-configuration**: Reads `services_config.yaml` to automatically discover the Arduino gRPC server endpoint
- **gRPC Streaming**: Connects to `grpc_server_streaming.py` via `grpc_client_streaming.py` for low-latency telemetry
- **Real-time monitoring**: Tracks all telemetry measurements that `dashboard_gui.py` monitors
- **HTTP API**: Exposes telemetry data via HTTP GET endpoint
- **No caching**: All responses include cache-control headers to ensure fresh data
- **CORS enabled**: Accessible from web browsers

## Architecture

```
telemetry_server_v2.py
    ↓ (reads config)
services_config.yaml
    ↓ (gets Arduino gRPC endpoint)
grpc_client_streaming.py
    ↓ (connects via gRPC)
grpc_server_streaming.py
    ↓ (reads from hardware)
Arduino Due
```

## Telemetry Data

The server monitors all configured pins from `pin_config.json`, including:

- **DAC outputs**: e.g., Laser Power Control, Objective Heater Control
- **Analog inputs**: e.g., Seed Monitor, Amplifier readings
- **I2C sensors**: e.g., SHTC3 temperature/humidity sensors

Each measurement includes:
- `name`: Channel name from pin_config.json
- `pin_label`: Pin identifier (e.g., "DAC0", "A11")
- `source`: Data source (e.g., "dac_pin", "analog_read", "shtc3")
- `value`: Converted value in specified units
- `unit`: Unit of measurement (e.g., "W", "°C", "V")
- `volts`: Raw voltage reading
- `raw`: Raw ADC/DAC value
- `min_value`, `max_value`: Configured limits
- `status`: "ok" or "error"
- `updated_at`: Timestamp of last update
- `error`: Error message if status is "error"
- `metadata`: Additional sensor metadata

## HTTP Endpoints

### GET /telemetry

Returns all telemetry data in JSON format.

**Response structure:**
```json
{
  "connected": true,
  "timestamp": "2025-11-04T12:34:56.789Z",
  "server_time": "2025-11-04T12:34:56.789Z",
  "grpc_target": "localhost:50051",
  "measurements": {
    "LASER_POWER_CONTROL_DAC_PIN": {
      "name": "LASER_POWER_CONTROL_DAC_PIN",
      "pin_label": "DAC0",
      "source": "dac_pin",
      "value": 2.5,
      "unit": "W",
      "volts": 2.5,
      "raw": 3103,
      "min_value": 0.0,
      "max_value": 3.3,
      "status": "ok",
      "updated_at": "2025-11-04T12:34:56.789Z",
      "quantity": null,
      "metadata": null
    },
    "SEED_MONITOR_ANALOG_READ": {
      "name": "SEED_MONITOR_ANALOG_READ",
      "pin_label": "A11",
      "source": "analog_read",
      "value": 1.65,
      "unit": "VOLT",
      "volts": 1.65,
      "raw": null,
      "min_value": null,
      "max_value": null,
      "status": "ok",
      "updated_at": "2025-11-04T12:34:56.789Z",
      "quantity": null,
      "metadata": null
    }
  },
  "error": null
}
```

**Headers:**
```
Content-Type: application/json
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
Access-Control-Allow-Origin: *
```

### GET /health

Returns server health status.

**Response:**
```json
{
  "status": "ok",
  "timestamp": "2025-11-04T12:34:56.789Z"
}
```

Status values:
- `ok`: Connected to gRPC server and receiving telemetry
- `disconnected`: Not connected to gRPC server

## Usage

### Command Line

```bash
# Use default configuration
python telemetry_server_v2.py

# Specify custom config file
python telemetry_server_v2.py --config /path/to/services_config.yaml

# Override gRPC target
python telemetry_server_v2.py --grpc-target localhost:50051

# Custom HTTP host and port
python telemetry_server_v2.py --http-host 0.0.0.0 --http-port 9002

# Enable debug logging
python telemetry_server_v2.py --log-level DEBUG
```

### Using the startup script

```bash
# Default configuration
./start_telemetry_server.sh

# With environment variables
HTTP_HOST=0.0.0.0 HTTP_PORT=9002 LOG_LEVEL=INFO ./start_telemetry_server.sh
```

### Via services configuration

The server can be managed by the services manager. It's already configured in `services_config.yaml`:

```yaml
services:
  arduino_telemetry:
    enabled: true
    name: Arduino Due Telemetry HTTP Server
    script_path: GUI/external_access/start_telemetry_server.sh
    python_script: GUI/external_access/telemetry_server_v2.py
    cpu_list: '56'
    restart_on_exit: true
    restart_delay: 5
    log_subdir: arduino_telemetry
    args:
      http_host: 0.0.0.0
      http_port: 9002
      log_level: INFO
```

## Testing

### Test with curl

```bash
# Get telemetry data
curl http://localhost:9002/telemetry

# Pretty print JSON
curl -s http://localhost:9002/telemetry | python -m json.tool

# Health check
curl http://localhost:9002/health

# Verify no caching
curl -I http://localhost:9002/telemetry
```

### Test with Python

```python
import requests

# Get telemetry data
response = requests.get("http://localhost:9002/telemetry")
data = response.json()

if data["connected"]:
    for name, measurement in data["measurements"].items():
        print(f"{name}: {measurement['value']} {measurement['unit']}")
else:
    print(f"Not connected: {data['error']}")
```

### Test with JavaScript/Browser

```javascript
fetch('http://localhost:9002/telemetry')
    .then(response => response.json())
    .then(data => {
        console.log('Connected:', data.connected);
        console.log('Measurements:', data.measurements);
    });
```

## Configuration

### services_config.yaml

The server reads `services_config.yaml` to determine the Arduino gRPC server endpoint:

```yaml
services:
  arduino_grpc:
    enabled: true
    args:
      host: 0.0.0.0
      port: 50051
```

If the Arduino gRPC service has `host: 0.0.0.0` or `host: [::]`, the telemetry server automatically converts it to `localhost` for client connections.

### pin_config.json

The measurements available depend on what's configured in `Arduino/pin_config.json`. Each pin with `log_default: true` will be monitored and included in the telemetry data.

## Dependencies

- Python 3.8+
- grpcio
- grpcio-tools
- PyYAML

Install via:
```bash
pip install grpcio grpcio-tools pyyaml
```

Or use the conda environment:
```bash
conda activate tweezer
```

## Logging

Logs are written to stdout/stderr and can be captured by the services manager.

Log levels:
- `DEBUG`: Detailed information for debugging
- `INFO`: General information (default)
- `WARNING`: Warning messages
- `ERROR`: Error messages
- `CRITICAL`: Critical errors

## Troubleshooting

### Connection refused

**Problem**: `Failed to connect to gRPC server: [Errno 111] Connection refused`

**Solution**: Ensure the Arduino gRPC server is running:
```bash
# Check if server is running
ps aux | grep grpc_server_streaming.py

# Check if port is listening
netstat -tuln | grep 50051
```

### No telemetry data

**Problem**: Connected but `measurements` is empty

**Solution**: 
1. Check `pin_config.json` has entries with `log_default: true`
2. Ensure the Arduino Due is connected and responding
3. Check gRPC server logs for errors

### Port already in use

**Problem**: `OSError: [Errno 98] Address already in use`

**Solution**: 
1. Check if another instance is running: `lsof -i :9002`
2. Kill the process: `kill <PID>`
3. Or use a different port: `--http-port 9003`

## Performance

- **Latency**: ~20-50ms (depends on gRPC streaming and network)
- **Update rate**: Matches the gRPC server telemetry rate (typically 0.2s intervals)
- **Concurrent clients**: Supports multiple simultaneous HTTP clients
- **CPU usage**: Minimal (<1% on modern systems)

## Security Considerations

⚠️ **Warning**: This server:
- Binds to `0.0.0.0` (all interfaces) by default
- Has no authentication
- Has CORS enabled for all origins

For production use:
1. Bind to localhost only: `--http-host 127.0.0.1`
2. Use a reverse proxy with authentication (nginx, Apache)
3. Add HTTPS/TLS encryption
4. Restrict CORS origins
5. Use firewall rules to limit access

## Related Files

- `telemetry_server_v2.py`: Main server implementation
- `start_telemetry_server.sh`: Startup script
- `../services_config.yaml`: Services configuration
- `../../Arduino/pin_config.json`: Pin configuration
- `../../Arduino/rpc/grpc_client_streaming.py`: gRPC client
- `../../Arduino/rpc/grpc_server_streaming.py`: gRPC server
- `../dashboard_gui.py`: Reference implementation for telemetry monitoring
