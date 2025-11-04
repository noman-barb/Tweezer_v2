# Quick Start Guide - Arduino Due Telemetry HTTP Server v2

## Prerequisites

1. Arduino gRPC server must be running (`grpc_server_streaming.py`)
2. Python environment with required packages (grpcio, pyyaml)

## Start the Server

### Option 1: Direct Python

```bash
cd GUI/external_access
python telemetry_server_v2.py
```

The server will:
- Read `../services_config.yaml` to find the Arduino gRPC endpoint
- Connect to the gRPC server
- Start HTTP server on `http://0.0.0.0:9002`

### Option 2: Using the startup script

```bash
cd GUI/external_access
./start_telemetry_server.sh
```

### Option 3: Via services manager

If you're using the services manager, the telemetry server is already configured in `services_config.yaml` and will be managed automatically.

## Test the Server

### Quick test with curl

```bash
# Get telemetry data
curl http://localhost:9002/telemetry

# Health check
curl http://localhost:9002/health
```

### Use the test script

```bash
cd GUI/external_access
python test_telemetry_server.py

# Continuous monitoring
python test_telemetry_server.py --monitor
```

## Access the Data

The telemetry data is available at:

**http://localhost:9002/telemetry**

Response includes:
- Connection status
- Timestamp of last update
- All measurements from Arduino Due (DAC outputs, analog inputs, sensors)
- Each measurement has: name, value, unit, status, voltage, etc.

## Common Issues

**Connection refused:**
- Make sure Arduino gRPC server is running on port 50051
- Check `services_config.yaml` has correct host/port

**Empty measurements:**
- Check `Arduino/pin_config.json` has entries with `log_default: true`
- Verify Arduino Due is connected and responding

**Port already in use:**
- Another instance may be running
- Check with: `lsof -i :9002`
- Use different port: `--http-port 9003`

## Integration Examples

### Python

```python
import requests

response = requests.get("http://localhost:9002/telemetry")
data = response.json()

if data["connected"]:
    for name, meas in data["measurements"].items():
        print(f"{name}: {meas['value']} {meas['unit']}")
```

### JavaScript/Web

```javascript
fetch('http://localhost:9002/telemetry')
    .then(r => r.json())
    .then(data => console.log(data.measurements));
```

### curl + jq

```bash
# Get all laser power readings
curl -s http://localhost:9002/telemetry | \
    jq '.measurements | with_entries(select(.key | contains("LASER")))'

# Monitor specific value
watch -n 1 'curl -s http://localhost:9002/telemetry | \
    jq -r ".measurements.LASER_POWER_CONTROL_DAC_PIN.value"'
```

## Next Steps

- See `README_telemetry_v2.md` for complete documentation
- Check `test_telemetry_server.py` for testing examples
- Integrate with your monitoring/visualization tools
