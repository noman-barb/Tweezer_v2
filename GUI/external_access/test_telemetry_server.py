#!/usr/bin/env python3
"""Test script for Arduino Due Telemetry HTTP Server v2."""

import argparse
import json
import sys
import time
from typing import Any, Dict

try:
    import requests
except ImportError:
    print("Error: requests library not found. Install with: pip install requests")
    sys.exit(1)


def test_endpoint(url: str, endpoint: str) -> Dict[str, Any]:
    """Test a specific endpoint."""
    full_url = f"{url}{endpoint}"
    print(f"\nTesting: {full_url}")
    print("-" * 80)
    
    try:
        response = requests.get(full_url, timeout=5)
        print(f"Status Code: {response.status_code}")
        print(f"Headers:")
        for key, value in response.headers.items():
            if key.lower() in ("content-type", "cache-control", "pragma", "expires"):
                print(f"  {key}: {value}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"\nResponse Data:")
            print(json.dumps(data, indent=2, default=str))
            return data
        else:
            print(f"Error: {response.text}")
            return {}
            
    except requests.exceptions.ConnectionError:
        print(f"ERROR: Could not connect to {full_url}")
        print("Make sure the telemetry server is running!")
        return {}
    except requests.exceptions.Timeout:
        print(f"ERROR: Request timed out")
        return {}
    except Exception as exc:
        print(f"ERROR: {exc}")
        return {}


def test_telemetry_data(url: str) -> bool:
    """Test the telemetry endpoint and validate data."""
    data = test_endpoint(url, "/telemetry")
    
    if not data:
        return False
    
    print("\n" + "=" * 80)
    print("TELEMETRY DATA SUMMARY")
    print("=" * 80)
    
    connected = data.get("connected", False)
    print(f"Connected: {connected}")
    
    if not connected:
        error = data.get("error")
        print(f"Error: {error}")
        return False
    
    timestamp = data.get("timestamp")
    print(f"Last Update: {timestamp}")
    
    measurements = data.get("measurements", {})
    print(f"\nNumber of Measurements: {len(measurements)}")
    
    if measurements:
        print("\nMeasurement Details:")
        print("-" * 80)
        for name, meas in measurements.items():
            value = meas.get("value")
            unit = meas.get("unit")
            status = meas.get("status", "unknown")
            source = meas.get("source", "unknown")
            
            status_icon = "✓" if status == "ok" else "✗"
            print(f"{status_icon} {name}")
            print(f"    Value: {value} {unit}")
            print(f"    Source: {source}")
            print(f"    Pin: {meas.get('pin_label')}")
            
            if meas.get("error"):
                print(f"    Error: {meas.get('error')}")
    
    return True


def test_health_check(url: str) -> bool:
    """Test the health endpoint."""
    data = test_endpoint(url, "/health")
    
    if not data:
        return False
    
    print("\n" + "=" * 80)
    print("HEALTH CHECK")
    print("=" * 80)
    
    status = data.get("status", "unknown")
    print(f"Status: {status}")
    
    return status == "ok"


def continuous_monitor(url: str, interval: float = 1.0) -> None:
    """Continuously monitor telemetry data."""
    print(f"\nContinuously monitoring {url}/telemetry")
    print(f"Update interval: {interval}s")
    print("Press Ctrl+C to stop")
    print("=" * 80)
    
    try:
        while True:
            try:
                response = requests.get(f"{url}/telemetry", timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    
                    if data.get("connected"):
                        timestamp = data.get("timestamp", "unknown")
                        measurements = data.get("measurements", {})
                        
                        # Print summary line
                        print(f"[{timestamp}] {len(measurements)} measurements")
                        
                        # Print first few measurements as examples
                        for i, (name, meas) in enumerate(list(measurements.items())[:3]):
                            value = meas.get("value")
                            unit = meas.get("unit")
                            print(f"  - {name}: {value} {unit}")
                        
                        if len(measurements) > 3:
                            print(f"  ... and {len(measurements) - 3} more")
                    else:
                        error = data.get("error", "Unknown error")
                        print(f"[DISCONNECTED] {error}")
                else:
                    print(f"[ERROR] Status {response.status_code}")
                    
            except requests.exceptions.RequestException as exc:
                print(f"[ERROR] {exc}")
            
            time.sleep(interval)
            
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test Arduino Due Telemetry HTTP Server v2"
    )
    parser.add_argument(
        "--url",
        type=str,
        default="http://localhost:9002",
        help="Base URL of telemetry server (default: http://localhost:9002)",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Continuously monitor telemetry data",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Update interval for monitoring mode (default: 1.0s)",
    )
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("Arduino Due Telemetry HTTP Server v2 - Test Script")
    print("=" * 80)
    print(f"Target URL: {args.url}")
    
    if args.monitor:
        continuous_monitor(args.url, args.interval)
    else:
        # Run all tests
        health_ok = test_health_check(args.url)
        telemetry_ok = test_telemetry_data(args.url)
        
        print("\n" + "=" * 80)
        print("TEST RESULTS")
        print("=" * 80)
        print(f"Health Check: {'PASS' if health_ok else 'FAIL'}")
        print(f"Telemetry Data: {'PASS' if telemetry_ok else 'FAIL'}")
        
        if health_ok and telemetry_ok:
            print("\n✓ All tests passed!")
            sys.exit(0)
        else:
            print("\n✗ Some tests failed!")
            sys.exit(1)


if __name__ == "__main__":
    main()
