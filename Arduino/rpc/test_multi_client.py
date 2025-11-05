"""Test script to verify multiple clients can connect simultaneously."""

import sys
import time
from pathlib import Path

# Add the RPC path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from grpc_client_streaming import DueStreamingClient


def test_multi_client():
    """Test multiple simultaneous connections."""
    target = "localhost:50051"
    
    print("Creating Client 1 (commands + telemetry)...")
    client1 = DueStreamingClient(target, timeout=5.0, enable_commands=True, enable_telemetry=True)
    
    print("Creating Client 2 (telemetry only)...")
    client2 = DueStreamingClient(target, timeout=5.0, enable_commands=False, enable_telemetry=True)
    
    # Telemetry callback for client 1
    def telemetry1(timestamp, measurements):
        print(f"[Client1] {timestamp}: {len(measurements)} measurements")
    
    # Telemetry callback for client 2
    def telemetry2(timestamp, measurements):
        print(f"[Client2] {timestamp}: {len(measurements)} measurements")
    
    try:
        print("\nConnecting Client 1...")
        client1.set_telemetry_callback(telemetry1)
        client1.connect()
        print("✓ Client 1 connected")
        
        print("\nConnecting Client 2...")
        client2.set_telemetry_callback(telemetry2)
        client2.connect()
        print("✓ Client 2 connected")
        
        print("\nBoth clients connected! Testing for 10 seconds...")
        
        # Test sending commands from client 1
        print("\nTesting commands from Client 1...")
        try:
            client1.set_voltage_mode(True)
            print("✓ Client 1 can send commands")
        except Exception as e:
            print(f"✗ Client 1 command failed: {e}")
        
        # Test that client 2 cannot send commands
        print("\nTesting that Client 2 cannot send commands...")
        try:
            client2.set_voltage_mode(True)
            print("✗ Client 2 should not be able to send commands!")
        except RuntimeError as e:
            print(f"✓ Client 2 correctly prevented from sending commands: {e}")
        
        # Wait and observe telemetry
        time.sleep(10)
        
        print("\n✓ Multi-client test successful!")
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        print("\nShutting down clients...")
        client1.shutdown()
        client2.shutdown()
        print("✓ Clients shut down")


if __name__ == "__main__":
    test_multi_client()
