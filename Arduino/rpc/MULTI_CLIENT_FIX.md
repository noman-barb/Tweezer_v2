# Fix for Multiple Client Connections to Arduino Due gRPC Server

## Problem
Both `telemetry_server_v2.py` and `dashboard_gui.py` could not simultaneously connect to the Arduino Due gRPC server. Only the first client to connect would work; subsequent clients would fail or hang.

## Root Causes

### 1. Insufficient Thread Pool Size
**File**: `GUI/services_config.yaml`
- **Issue**: `max_workers: 2` was too low for multiple clients
- **Why it matters**: Each client connection creates 2 streaming RPCs:
  - Command stream (bidirectional)
  - Telemetry stream (server-to-client)
- With 2 workers and 2 clients, we need: 2 clients × 2 streams = 4 threads minimum
- **Fix**: Increased `max_workers: 2` → `max_workers: 10`

### 2. Unnecessary Command Streams
**File**: `Arduino/rpc/grpc_client_streaming.py`
- **Issue**: Every client created BOTH command and telemetry streams, even if only telemetry was needed
- **Why it matters**: 
  - `telemetry_server_v2.py` only needs to read telemetry, not send commands
  - Creating unnecessary command streams wastes worker threads
  - Creates potential for command conflicts
- **Fix**: Made streams optional with `enable_commands` and `enable_telemetry` parameters

### 3. Missing Thread Safety (Already Fixed)
**File**: `Arduino/rpc/grpc_server_streaming.py`
- **Issue**: Command execution wasn't protected by the lock
- **Fix**: Wrapped command execution in `with self._lock:`

## Changes Made

### 1. `Arduino/rpc/grpc_client_streaming.py`

```python
# Added optional stream control
def __init__(self, target: str, *, timeout: float = 5.0, 
             enable_commands: bool = True, 
             enable_telemetry: bool = True):
    ...

def connect(self) -> None:
    # Only start command stream if needed
    if self._enable_commands:
        self._stream_thread = threading.Thread(...)
    
    # Only start telemetry stream if needed
    if self._enable_telemetry:
        self._telemetry_thread = threading.Thread(...)

def call(self, method: str, *args, **kwargs):
    # Check if commands are enabled
    if not self._enable_commands:
        raise RuntimeError("Commands not enabled...")
```

### 2. `GUI/external_access/telemetry_server_v2.py`

```python
# Only enable telemetry stream, not commands
self.client = DueStreamingClient(
    self.grpc_target, 
    timeout=5.0, 
    enable_commands=False,  # Don't create command stream
    enable_telemetry=True   # Only telemetry needed
)
```

### 3. `GUI/services_config.yaml`

```yaml
arduino_grpc:
  args:
    max_workers: 10  # Increased from 2
```

### 4. `Arduino/rpc/grpc_server_streaming.py`

```python
# Command execution now protected by lock
with self._lock:
    result = target(*args, **kwargs)
```

## How It Works Now

### Dashboard GUI
- Creates client with **both** commands and telemetry enabled (default)
- Can send DAC commands, read sensors, etc.
- Receives telemetry updates
- Uses: 2 worker threads (1 for commands, 1 for telemetry)

### Telemetry Server
- Creates client with **only** telemetry enabled
- Cannot send commands (will raise error if attempted)
- Only receives telemetry updates
- Uses: 1 worker thread (just for telemetry)

### Total Thread Usage
- Dashboard: 2 threads
- Telemetry Server: 1 thread
- Total: 3 threads (well under the 10 worker limit)

## Benefits

1. **Multiple clients work simultaneously** - No more blocking
2. **Efficient resource usage** - Only create streams that are needed
3. **Clear separation of concerns** - Telemetry readers can't accidentally send commands
4. **Thread-safe operation** - All command execution is serialized
5. **Scalable** - Can support many more read-only telemetry clients

## Testing

Created `Arduino/rpc/test_multi_client.py` to verify:
- ✓ Client 1 can connect with commands + telemetry
- ✓ Client 2 can connect with telemetry only
- ✓ Both clients receive telemetry simultaneously
- ✓ Client 1 can send commands
- ✓ Client 2 correctly prevented from sending commands
- ✓ No blocking or hanging

## Usage Examples

### For Command + Telemetry (Dashboard)
```python
client = DueStreamingClient(target)
# Both streams enabled by default
client.connect()
client.set_voltage_mode(True)  # Can send commands
```

### For Telemetry Only (Monitoring)
```python
client = DueStreamingClient(
    target, 
    enable_commands=False, 
    enable_telemetry=True
)
client.connect()
# Can only receive telemetry, cannot send commands
```

### For Commands Only (Rare)
```python
client = DueStreamingClient(
    target, 
    enable_commands=True, 
    enable_telemetry=False
)
client.connect()
# Can send commands, but won't receive telemetry
```

## Migration Notes

**Existing code continues to work!** The default behavior is unchanged:
- `enable_commands=True` (default)
- `enable_telemetry=True` (default)

Only `telemetry_server_v2.py` was updated to opt-out of commands.

## Troubleshooting

If you still see connection issues:

1. **Check worker count**: Ensure `max_workers` is sufficient
   - Minimum: (# of clients × 2) if all use commands+telemetry
   - Recommended: 10 or more

2. **Check if server is running**: 
   ```bash
   ps aux | grep grpc_server_streaming
   ```

3. **Check logs**: Look in `logs/service_logs/arduino_grpc_server/`

4. **Test with script**: 
   ```bash
   python Arduino/rpc/test_multi_client.py
   ```
