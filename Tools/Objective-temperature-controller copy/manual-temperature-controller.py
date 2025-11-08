"""Manual Temperature Controller with Web GUI.

Simplified version focused on manual control:
1. Fetches objective temperature from remote telemetry server (10.0.63.195:9002)
2. Manual PWM control (0-0.4) with heating/cooling mode selection
3. Real-time monitoring with web dashboard
4. Temperature and control graphs
5. CSV logging for data persistence
6. No automatic PID control - pure manual operation
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import serial
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    print("Error: 'requests' module not found. Install it with: pip install requests")
    raise

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============================================================================
# Arduino Serial Interface
# ============================================================================

class ArduinoController:
    """Controls Arduino via serial for PWM and heating/cooling mode."""
    
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 2.0):
        """Initialize Arduino controller.
        
        Args:
            port: Serial port (e.g., "COM4")
            baudrate: Serial baud rate
            timeout: Serial timeout in seconds
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        
        self.current_pwm: Optional[float] = None
        self.current_pin7_state: Optional[str] = None
    
    def connect(self) -> None:
        """Connect to Arduino."""
        try:
            if self.serial and self.serial.is_open:
                logger.info("Already connected to Arduino on %s", self.port)
                return
            
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout
            )
            
            # Wait for Arduino to reset
            time.sleep(2)
            
            # Clear any startup messages
            self.serial.reset_input_buffer()
            
            logger.info("Connected to Arduino on %s", self.port)
            
        except Exception as exc:
            logger.error("Failed to connect to Arduino: %s", exc)
            self.serial = None
            raise
    
    def disconnect(self) -> None:
        """Disconnect from Arduino."""
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info("Disconnected from Arduino")
    
    def _send_command(self, command: str) -> Optional[str]:
        """Send command to Arduino and read response.
        
        Args:
            command: Command string to send
        
        Returns:
            Response from Arduino or None
        """
        with self._lock:
            if not self.serial or not self.serial.is_open:
                logger.error("Arduino not connected")
                return None
            
            try:
                # Send command
                self.serial.write(f"{command}\n".encode('utf-8'))
                
                # Read response
                response = self.serial.readline().decode('utf-8').strip()
                return response
                
            except Exception as exc:
                logger.error("Error sending command '%s': %s", command, exc)
                return None
    
    def set_pwm(self, duty: float) -> bool:
        """Set PWM duty cycle.
        
        Args:
            duty: PWM duty cycle (0.0 to 0.4)
        
        Returns:
            True if successful
        """
        duty = max(0.0, min(duty, 0.4))
        response = self._send_command(f"PWM {duty:.4f}")
        if response is not None:
            self.current_pwm = duty
            return True
        return False
    
    def set_pin7_high(self) -> bool:
        """Set PIN7 HIGH (heating mode)."""
        response = self._send_command("PIN7 HIGH")
        if response is not None:
            self.current_pin7_state = "HIGH"
            return True
        return False
    
    def set_pin7_low(self) -> bool:
        """Set PIN7 LOW (cooling mode)."""
        response = self._send_command("PIN7 LOW")
        if response is not None:
            self.current_pin7_state = "LOW"
            return True
        return False
    
    def set_mode(self, mode: str, pwm: float) -> bool:
        """Set heating/cooling mode and PWM.
        
        Args:
            mode: "HEATING", "COOLING", or "OFF"
            pwm: PWM duty cycle (0.0 to 0.4)
        
        Returns:
            True if successful
        """
        if mode == "OFF":
            success = self.set_pwm(0.0)
            return success
        elif mode == "HEATING":
            pin7_success = self.set_pin7_high()
            pwm_success = self.set_pwm(pwm)
            return pin7_success and pwm_success
        elif mode == "COOLING":
            pin7_success = self.set_pin7_low()
            pwm_success = self.set_pwm(pwm)
            return pin7_success and pwm_success
        else:
            logger.error("Invalid mode: %s", mode)
            return False


# ============================================================================
# Telemetry Client
# ============================================================================

class TelemetryClient:
    """Fetches telemetry data from remote server."""
    
    def __init__(self, base_url: str, timeout: float = 5.0):
        """Initialize telemetry client.
        
        Args:
            base_url: Base URL of telemetry server (e.g., "http://10.0.63.195:9002")
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
    
    def fetch_telemetry(self) -> Optional[Dict[str, Any]]:
        """Fetch current telemetry data.
        
        Returns:
            Dictionary with telemetry data or None if failed
        """
        try:
            response = requests.get(f"{self.base_url}/telemetry", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        
        except Exception as exc:
            logger.error("Failed to fetch telemetry: %s", exc)
            return None
    
    def get_objective_temperature(self) -> Optional[float]:
        """Get objective temperature from telemetry.
        
        Returns:
            Temperature in °C or None if not available
        """
        data = self.fetch_telemetry()
        if not data:
            return None
        
        measurements = data.get("measurements", {})
        
        if not measurements:
            logger.warning("No measurements in telemetry data")
            return None
        
        # Look for objective temperature keys
        obj_temp_keys = [
            "OBJECTIVE_TEMPERATURE_ANALOG_READ",
            "OBJECTIVE_TEMPERATURE",
            "OBJ_TEMP",
            "OBJECTIVE_TEMP",
            "SHTC3_TEMPERATURE",
        ]
        
        for key in obj_temp_keys:
            if key in measurements:
                temp = measurements[key]
                if temp is not None:
                    return float(temp)
        
        # Log available keys to help debug
        available_keys = list(measurements.keys())
        logger.warning(
            "Objective temperature not found. Available keys: %s",
            ", ".join(available_keys[:10]) if available_keys else "none"
        )
        return None
    
    def get_ambient_data(self) -> Tuple[Optional[float], Optional[float]]:
        """Get ambient temperature and humidity.
        
        Returns:
            Tuple of (temperature °C, humidity %RH) or (None, None)
        """
        data = self.fetch_telemetry()
        if not data:
            return None, None
        
        measurements = data.get("measurements", {})
        
        temp = measurements.get("SHTC3_TEMPERATURE")
        humidity = measurements.get("SHTC3_HUMIDITY")
        
        if temp is not None:
            temp = float(temp)
        if humidity is not None:
            humidity = float(humidity)
        
        return temp, humidity


# ============================================================================
# Data Logger
# ============================================================================

class DataLogger:
    """CSV-based data logger with persistence."""
    
    def __init__(self, log_dir: Path):
        """Initialize data logger.
        
        Args:
            log_dir: Directory to store log files
        """
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_log_file: Optional[Path] = None
        self.csv_writer: Optional[csv.DictWriter] = None
        self.csv_file: Optional[Any] = None
        
        self._lock = threading.Lock()
        
        # Create new log file
        self._create_new_log_file()
    
    def _create_new_log_file(self) -> None:
        """Create a new log file with timestamp."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"manual_temp_log_{timestamp}.csv"
        self.current_log_file = self.log_dir / filename
        
        # Close previous file if open
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception:
                pass
        
        # Open new file
        self.csv_file = open(self.current_log_file, 'w', newline='', encoding='utf-8')
        
        fieldnames = [
            "timestamp",
            "objective_temp",
            "pwm",
            "mode",
            "room_temp",
            "humidity",
        ]
        
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
        self.csv_writer.writeheader()
        self.csv_file.flush()
        
        logger.info("Created new log file: %s", self.current_log_file)
    
    def log_data(
        self,
        objective_temp: Optional[float],
        pwm: float,
        mode: str,
        room_temp: Optional[float] = None,
        humidity: Optional[float] = None,
    ) -> None:
        """Log data point to CSV.
        
        Args:
            objective_temp: Objective temperature in °C
            pwm: PWM duty cycle (0.0 to 0.4)
            mode: "HEATING", "COOLING", or "OFF"
            room_temp: Room temperature in °C
            humidity: Relative humidity in %RH
        """
        with self._lock:
            if not self.csv_writer:
                return
            
            row = {
                "timestamp": datetime.now().isoformat(),
                "objective_temp": objective_temp if objective_temp is not None else "",
                "pwm": f"{pwm:.4f}",
                "mode": mode,
                "room_temp": room_temp if room_temp is not None else "",
                "humidity": humidity if humidity is not None else "",
            }
            
            self.csv_writer.writerow(row)
            self.csv_file.flush()
    
    def close(self) -> None:
        """Close log file."""
        with self._lock:
            if self.csv_file:
                try:
                    self.csv_file.close()
                    logger.info("Log file closed")
                except Exception as exc:
                    logger.error("Error closing log file: %s", exc)


# ============================================================================
# Manual Temperature Controller
# ============================================================================

class ManualTemperatureController:
    """Manual temperature controller with monitoring."""
    
    def __init__(
        self,
        arduino_port: str,
        telemetry_url: str,
        log_dir: Path,
        update_interval: float = 1.0,
    ):
        """Initialize manual temperature controller.
        
        Args:
            arduino_port: Arduino serial port
            telemetry_url: Telemetry server URL
            log_dir: Directory for log files
            update_interval: Data update interval in seconds
        """
        self.arduino_port = arduino_port
        self.telemetry_url = telemetry_url
        self.log_dir = log_dir
        self.update_interval = update_interval
        
        # Initialize components
        self.arduino = ArduinoController(port=arduino_port)
        self.telemetry = TelemetryClient(base_url=telemetry_url)
        self.logger = DataLogger(log_dir=log_dir)
        
        # Control loop state
        self.running = False
        self.update_thread: Optional[threading.Thread] = None
        
        # Manual control state
        self.manual_mode = "OFF"
        self.manual_pwm = 0.0
        
        # Data buffer for GUI (keep last 1000 points)
        self.data_buffer: deque = deque(maxlen=1000)
        self.buffer_lock = threading.Lock()
    
    def start(self) -> None:
        """Start the controller."""
        if self.running:
            logger.warning("Controller already running")
            return
        
        # Try to connect to Arduino
        try:
            self.arduino.connect()
        except Exception as exc:
            logger.warning("Could not connect to Arduino: %s", exc)
        
        # Start update loop
        self.running = True
        self.update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self.update_thread.start()
        
        logger.info("Manual temperature controller started")
    
    def stop(self) -> None:
        """Stop the controller."""
        if not self.running:
            return
        
        logger.info("Stopping manual temperature controller...")
        self.running = False
        
        # Wait for update thread
        if self.update_thread:
            self.update_thread.join(timeout=5)
        
        # Disconnect Arduino
        self.arduino.disconnect()
        
        # Close logger
        self.logger.close()
        
        logger.info("Manual temperature controller stopped")
    
    def _update_loop(self) -> None:
        """Main update loop (runs in separate thread)."""
        logger.info("Update loop started")
        
        while self.running:
            try:
                # Fetch telemetry data
                obj_temp = self.telemetry.get_objective_temperature()
                room_temp, humidity = self.telemetry.get_ambient_data()
                
                # Log data
                self.logger.log_data(
                    objective_temp=obj_temp,
                    pwm=self.manual_pwm,
                    mode=self.manual_mode,
                    room_temp=room_temp,
                    humidity=humidity,
                )
                
                # Store in buffer for GUI
                data_point = {
                    "timestamp": datetime.now().isoformat(),
                    "objective_temp": obj_temp,
                    "pwm": self.manual_pwm,
                    "mode": self.manual_mode,
                    "room_temp": room_temp,
                    "humidity": humidity,
                }
                
                with self.buffer_lock:
                    self.data_buffer.append(data_point)
                
            except Exception as exc:
                logger.error("Error in update loop: %s", exc)
            
            # Wait for next update
            time.sleep(self.update_interval)
        
        logger.info("Update loop stopped")
    
    def set_manual_control(self, mode: str, pwm: float) -> bool:
        """Set manual control output.
        
        Args:
            mode: "HEATING", "COOLING", or "OFF"
            pwm: PWM duty cycle (0.0 to 0.4)
        
        Returns:
            True if successful
        """
        mode = mode.upper()
        pwm = max(0.0, min(pwm, 0.4))
        
        # Update state
        self.manual_mode = mode
        self.manual_pwm = pwm if mode != "OFF" else 0.0
        
        # Apply to Arduino
        if self.arduino.serial and self.arduino.serial.is_open:
            success = self.arduino.set_mode(mode, pwm)
            if success:
                logger.info("Manual control set: mode=%s, pwm=%.3f", mode, pwm)
            else:
                logger.error("Failed to set manual control")
            return success
        else:
            logger.warning("Arduino not connected - control not applied")
            return False
    
    def get_current_data(self) -> Dict[str, Any]:
        """Get current controller data for GUI."""
        with self.buffer_lock:
            if self.data_buffer:
                latest = self.data_buffer[-1]
            else:
                latest = {
                    "timestamp": datetime.now().isoformat(),
                    "objective_temp": None,
                    "pwm": 0.0,
                    "mode": "OFF",
                    "room_temp": None,
                    "humidity": None,
                }
            return latest
    
    def get_historical_data(self, max_points: int = 1000) -> List[Dict[str, Any]]:
        """Get historical data buffer."""
        with self.buffer_lock:
            data = list(self.data_buffer)
            if len(data) > max_points:
                data = data[-max_points:]
            return data
    
    def load_historical_logs(self) -> List[Dict[str, Any]]:
        """Load all historical log files."""
        all_data = []
        
        try:
            log_files = sorted(self.log_dir.glob("manual_temp_log_*.csv"))
            
            for log_file in log_files:
                try:
                    with open(log_file, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            try:
                                data_point = {
                                    "timestamp": row["timestamp"],
                                    "objective_temp": float(row["objective_temp"]) if row["objective_temp"] else None,
                                    "pwm": float(row["pwm"]) if row["pwm"] else 0.0,
                                    "mode": row["mode"],
                                    "room_temp": float(row["room_temp"]) if row["room_temp"] else None,
                                    "humidity": float(row["humidity"]) if row["humidity"] else None,
                                }
                                all_data.append(data_point)
                            except (ValueError, KeyError) as e:
                                logger.warning("Skipping invalid row in %s: %s", log_file.name, e)
                                continue
                except Exception as exc:
                    logger.error("Error reading log file %s: %s", log_file.name, exc)
        
        except Exception as exc:
            logger.error("Error loading historical logs: %s", exc)
        
        return all_data


# ============================================================================
# HTTP Server with Web GUI
# ============================================================================

class ControllerHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for temperature controller."""
    
    # Class variable set by server
    controller: Optional[ManualTemperatureController] = None
    
    def log_message(self, format: str, *args: Any) -> None:
        """Override to use our logger."""
        logger.info("%s - %s", self.address_string(), format % args)
    
    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path == "/" or path == "/gui":
            self._serve_gui()
        elif path == "/api/health":
            self._serve_health()
        elif path == "/api/current":
            self._serve_current_data()
        elif path == "/api/history":
            self._serve_history()
        elif path == "/api/control":
            self._serve_control(parsed.query)
        else:
            self._send_error(404, "Not Found")
    
    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        """Send JSON response."""
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def _send_error(self, status: int, message: str) -> None:
        """Send error response."""
        self._send_json({"error": message}, status)
    
    def _serve_health(self) -> None:
        """Serve health/status information."""
        if not self.controller:
            self._send_error(500, "Controller not available")
            return
        
        health = {
            "status": "ok",
            "running": self.controller.running,
            "arduino_connected": self.controller.arduino.serial is not None and self.controller.arduino.serial.is_open,
            "mode": self.controller.manual_mode,
            "pwm": self.controller.manual_pwm,
        }
        self._send_json(health)
    
    def _serve_current_data(self) -> None:
        """Serve current controller data."""
        if not self.controller:
            self._send_error(500, "Controller not available")
            return
        
        data = self.controller.get_current_data()
        self._send_json(data)
    
    def _serve_history(self) -> None:
        """Serve historical data."""
        if not self.controller:
            self._send_error(500, "Controller not available")
            return
        
        # Load all historical logs
        all_data = self.controller.load_historical_logs()
        
        # Add current buffer data
        current_data = self.controller.get_historical_data()
        all_data.extend(current_data)
        
        self._send_json({"data": all_data})
    
    def _serve_control(self, query: str) -> None:
        """Set manual control output."""
        if not self.controller:
            self._send_error(500, "Controller not available")
            return
        
        params = parse_qs(query)
        
        if "mode" not in params or "pwm" not in params:
            self._send_error(400, "Missing required parameters: mode, pwm")
            return
        
        try:
            mode = params["mode"][0]
            pwm = float(params["pwm"][0])
            
            success = self.controller.set_manual_control(mode, pwm)
            
            if success:
                response = {
                    "success": True,
                    "message": f"Control set to {mode} with PWM={pwm:.3f}",
                    "mode": self.controller.manual_mode,
                    "pwm": self.controller.manual_pwm,
                }
                self._send_json(response)
            else:
                self._send_error(500, "Failed to apply control")
        
        except (ValueError, IndexError) as e:
            self._send_error(400, f"Invalid parameters: {e}")
    
    def _serve_gui(self) -> None:
        """Serve web GUI."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Manual Temperature Controller</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/date-fns@2.30.0"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        
        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }
        
        header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.2);
        }
        
        header p {
            font-size: 1.1em;
            opacity: 0.9;
        }
        
        .status-bar {
            display: flex;
            gap: 20px;
            padding: 20px 30px;
            background: #f8f9fa;
            border-bottom: 2px solid #e9ecef;
            flex-wrap: wrap;
        }
        
        .status-item {
            flex: 1;
            min-width: 200px;
            background: white;
            padding: 15px 20px;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        
        .status-item h3 {
            font-size: 0.9em;
            color: #666;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .status-item .value {
            font-size: 1.8em;
            font-weight: bold;
            color: #667eea;
        }
        
        .status-item .unit {
            font-size: 0.9em;
            color: #999;
            margin-left: 5px;
        }
        
        .mode-heating {
            color: #ff6b6b !important;
        }
        
        .mode-cooling {
            color: #4ecdc4 !important;
        }
        
        .mode-off {
            color: #95a5a6 !important;
        }
        
        .controls {
            padding: 30px;
            background: white;
            border-bottom: 2px solid #e9ecef;
        }
        
        .control-heading {
            font-size: 1.5em;
            margin-bottom: 20px;
            color: #333;
        }
        
        .control-group {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        
        .control-group label {
            font-weight: 600;
            color: #555;
            min-width: 80px;
        }
        
        .control-group select {
            padding: 12px 20px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 1.1em;
            min-width: 150px;
            transition: border-color 0.3s;
            cursor: pointer;
        }
        
        .control-group select:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .control-group input[type="number"] {
            padding: 12px 20px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 1.1em;
            width: 150px;
            transition: border-color 0.3s;
        }
        
        .control-group input[type="number"]:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .control-group input[type="range"] {
            flex: 1;
            min-width: 200px;
        }
        
        .control-group .range-value {
            font-size: 1.2em;
            font-weight: bold;
            color: #667eea;
            min-width: 60px;
        }
        
        .control-group button {
            padding: 12px 30px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 1.1em;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .control-group button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102,126,234,0.4);
        }
        
        .control-group button:active {
            transform: translateY(0);
        }
        
        .button-off {
            background: linear-gradient(135deg, #95a5a6 0%, #7f8c8d 100%) !important;
        }
        
        .charts {
            padding: 30px;
        }
        
        .chart-container {
            margin-bottom: 30px;
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        
        .chart-container h2 {
            margin-bottom: 15px;
            color: #333;
            font-size: 1.3em;
        }
        
        canvas {
            max-height: 300px;
        }
        
        .footer {
            padding: 20px 30px;
            background: #f8f9fa;
            text-align: center;
            color: #666;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🎮 Manual Temperature Controller</h1>
            <p>Direct control with real-time monitoring</p>
        </header>
        
        <div class="status-bar">
            <div class="status-item">
                <h3>Objective Temperature</h3>
                <div class="value" id="obj-temp">--</div>
            </div>
            <div class="status-item">
                <h3>Room Temperature</h3>
                <div class="value" id="room-temp">--</div>
            </div>
            <div class="status-item">
                <h3>Humidity</h3>
                <div class="value" id="humidity">--</div>
            </div>
            <div class="status-item">
                <h3>Control Mode</h3>
                <div class="value" id="mode">OFF</div>
            </div>
            <div class="status-item">
                <h3>PWM Output</h3>
                <div class="value" id="pwm">0.000</div>
            </div>
        </div>
        
        <div class="controls">
            <h2 class="control-heading">Manual Control</h2>
            
            <div class="control-group">
                <label for="mode-select">Mode:</label>
                <select id="mode-select" onchange="updateMode()">
                    <option value="OFF">OFF</option>
                    <option value="HEATING">HEATING</option>
                    <option value="COOLING">COOLING</option>
                </select>
            </div>
            
            <div class="control-group">
                <label for="pwm-slider">PWM:</label>
                <input type="range" id="pwm-slider" min="0" max="0.4" step="0.001" value="0" oninput="updatePWMDisplay()">
                <span class="range-value" id="pwm-display">0.000</span>
            </div>
            
            <div class="control-group">
                <button id="apply-btn" onclick="applyControl()">Apply Control</button>
                <button class="button-off" onclick="turnOff()">Turn OFF</button>
            </div>
        </div>
        
        <div class="charts">
            <div class="chart-container">
                <h2>Temperature vs Time</h2>
                <canvas id="tempChart"></canvas>
            </div>
            
            <div class="chart-container">
                <h2>Control Output vs Time</h2>
                <canvas id="controlChart"></canvas>
            </div>
        </div>
        
        <div class="footer">
            Last updated: <span id="last-update">Never</span>
        </div>
    </div>
    
    <script>
        // Configuration
        const UPDATE_INTERVAL = 1000; // 1 second
        const MAX_DATA_POINTS = 600; // 10 minutes at 1 Hz
        
        // Chart instances
        let tempChart, controlChart;
        
        // Initialize charts
        function initCharts() {
            const tempCtx = document.getElementById('tempChart').getContext('2d');
            tempChart = new Chart(tempCtx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'Objective Temperature',
                            data: [],
                            borderColor: '#667eea',
                            backgroundColor: 'rgba(102,126,234,0.1)',
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.4,
                        },
                        {
                            label: 'Room Temperature',
                            data: [],
                            borderColor: '#ff6b6b',
                            backgroundColor: 'rgba(255,107,107,0.1)',
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.4,
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                unit: 'minute',
                                displayFormats: {
                                    minute: 'HH:mm'
                                }
                            },
                            title: {
                                display: true,
                                text: 'Time'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'Temperature (°C)'
                            }
                        }
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top'
                        }
                    }
                }
            });
            
            const controlCtx = document.getElementById('controlChart').getContext('2d');
            controlChart = new Chart(controlCtx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'PWM Output',
                            data: [],
                            borderColor: '#4ecdc4',
                            backgroundColor: 'rgba(78,205,196,0.1)',
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y',
                        },
                        {
                            label: 'Mode (Heating=1, Off=0, Cooling=-1)',
                            data: [],
                            borderColor: '#95a5a6',
                            borderWidth: 2,
                            pointRadius: 0,
                            stepped: true,
                            yAxisID: 'y1',
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                unit: 'minute',
                                displayFormats: {
                                    minute: 'HH:mm'
                                }
                            },
                            title: {
                                display: true,
                                text: 'Time'
                            }
                        },
                        y: {
                            type: 'linear',
                            display: true,
                            position: 'left',
                            title: {
                                display: true,
                                text: 'PWM (0-0.4)'
                            },
                            min: 0,
                            max: 0.5,
                        },
                        y1: {
                            type: 'linear',
                            display: true,
                            position: 'right',
                            title: {
                                display: true,
                                text: 'Mode'
                            },
                            min: -1.5,
                            max: 1.5,
                            grid: {
                                drawOnChartArea: false,
                            },
                        }
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top'
                        }
                    }
                }
            });
        }
        
        // Update status display
        function updateStatus(data) {
            document.getElementById('obj-temp').innerHTML = 
                data.objective_temp !== null ? `${data.objective_temp.toFixed(2)}<span class="unit">°C</span>` : '--';
            
            document.getElementById('room-temp').innerHTML = 
                data.room_temp !== null ? `${data.room_temp.toFixed(2)}<span class="unit">°C</span>` : '--';
            
            document.getElementById('humidity').innerHTML = 
                data.humidity !== null ? `${data.humidity.toFixed(1)}<span class="unit">%RH</span>` : '--';
            
            const modeElement = document.getElementById('mode');
            const modeClass = data.mode ? `mode-${data.mode.toLowerCase()}` : 'mode-off';
            modeElement.textContent = data.mode || 'OFF';
            modeElement.className = `value ${modeClass}`.trim();
            
            document.getElementById('pwm').innerHTML = 
                `${(data.pwm || 0).toFixed(3)}<span class="unit"></span>`;
            
            document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
        }
        
        // Update charts
        function updateCharts(data) {
            const timestamp = new Date(data.timestamp);
            
            // Temperature chart
            if (data.objective_temp !== null) {
                tempChart.data.datasets[0].data.push({
                    x: timestamp,
                    y: data.objective_temp
                });
            }
            
            if (data.room_temp !== null) {
                tempChart.data.datasets[1].data.push({
                    x: timestamp,
                    y: data.room_temp
                });
            }
            
            // Control chart
            controlChart.data.datasets[0].data.push({
                x: timestamp,
                y: data.pwm || 0
            });
            
            // Mode as numeric: HEATING=1, OFF=0, COOLING=-1
            let modeValue = 0;
            if (data.mode === 'HEATING') modeValue = 1;
            else if (data.mode === 'COOLING') modeValue = -1;
            
            controlChart.data.datasets[1].data.push({
                x: timestamp,
                y: modeValue
            });
            
            // Limit data points
            if (tempChart.data.datasets[0].data.length > MAX_DATA_POINTS) {
                tempChart.data.datasets[0].data.shift();
            }
            if (tempChart.data.datasets[1].data.length > MAX_DATA_POINTS) {
                tempChart.data.datasets[1].data.shift();
            }
            if (controlChart.data.datasets[0].data.length > MAX_DATA_POINTS) {
                controlChart.data.datasets[0].data.shift();
            }
            if (controlChart.data.datasets[1].data.length > MAX_DATA_POINTS) {
                controlChart.data.datasets[1].data.shift();
            }
            
            tempChart.update('none');
            controlChart.update('none');
        }
        
        // Load historical data
        async function loadHistoricalData() {
            try {
                const response = await fetch('/api/history', { cache: 'no-store' });
                const data = await response.json();
                
                if (data.data && data.data.length > 0) {
                    // Take last MAX_DATA_POINTS
                    const recentData = data.data.slice(-MAX_DATA_POINTS);
                    
                    // Clear charts
                    tempChart.data.datasets[0].data = [];
                    tempChart.data.datasets[1].data = [];
                    controlChart.data.datasets[0].data = [];
                    controlChart.data.datasets[1].data = [];
                    
                    // Add historical data
                    recentData.forEach(point => {
                        const timestamp = new Date(point.timestamp);
                        
                        if (point.objective_temp !== null) {
                            tempChart.data.datasets[0].data.push({
                                x: timestamp,
                                y: point.objective_temp
                            });
                        }
                        
                        if (point.room_temp !== null) {
                            tempChart.data.datasets[1].data.push({
                                x: timestamp,
                                y: point.room_temp
                            });
                        }
                        
                        controlChart.data.datasets[0].data.push({
                            x: timestamp,
                            y: point.pwm || 0
                        });
                        
                        let modeValue = 0;
                        if (point.mode === 'HEATING') modeValue = 1;
                        else if (point.mode === 'COOLING') modeValue = -1;
                        
                        controlChart.data.datasets[1].data.push({
                            x: timestamp,
                            y: modeValue
                        });
                    });
                    
                    tempChart.update();
                    controlChart.update();
                }
            } catch (error) {
                console.error('Error loading historical data:', error);
            }
        }
        
        // Fetch current data
        async function fetchCurrentData() {
            try {
                const response = await fetch('/api/current', { cache: 'no-store' });
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                
                updateStatus(data);
                updateCharts(data);
            } catch (error) {
                console.error('Error fetching data:', error);
            }
        }
        
        // Update PWM display
        function updatePWMDisplay() {
            const slider = document.getElementById('pwm-slider');
            const display = document.getElementById('pwm-display');
            display.textContent = parseFloat(slider.value).toFixed(3);
        }
        
        // Update mode (enable/disable PWM slider)
        function updateMode() {
            const modeSelect = document.getElementById('mode-select');
            const pwmSlider = document.getElementById('pwm-slider');
            
            if (modeSelect.value === 'OFF') {
                pwmSlider.disabled = true;
            } else {
                pwmSlider.disabled = false;
            }
        }
        
        // Apply control
        async function applyControl() {
            const mode = document.getElementById('mode-select').value;
            const pwm = document.getElementById('pwm-slider').value;
            
            try {
                const response = await fetch(`/api/control?mode=${mode}&pwm=${pwm}`, { cache: 'no-store' });
                const data = await response.json();
                
                if (data.success) {
                    console.log(data.message);
                } else {
                    alert('Failed to apply control: ' + (data.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Error applying control:', error);
                alert('Failed to apply control');
            }
        }
        
        // Turn off
        async function turnOff() {
            document.getElementById('mode-select').value = 'OFF';
            document.getElementById('pwm-slider').value = 0;
            updatePWMDisplay();
            updateMode();
            await applyControl();
        }
        
        // Initialize
        window.onload = async function() {
            console.log('Initializing manual temperature controller dashboard...');
            
            try {
                initCharts();
                await loadHistoricalData();
                
                // Start periodic updates
                setInterval(fetchCurrentData, UPDATE_INTERVAL);
                
                // Fetch initial data
                await fetchCurrentData();
                
                // Initialize mode state
                updateMode();
                
                console.log('Dashboard initialized successfully');
            } catch (error) {
                console.error('Error during initialization:', error);
            }
        };
    </script>
</body>
</html>"""
        
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_http_server(
    controller: ManualTemperatureController,
    host: str = "0.0.0.0",
    port: int = 9004,
) -> None:
    """Run the HTTP server.
    
    Args:
        controller: Temperature controller instance
        host: Host to bind to
        port: Port to bind to
    """
    ControllerHTTPHandler.controller = controller
    
    server_address = (host, port)
    httpd = HTTPServer(server_address, ControllerHTTPHandler)
    
    logger.info("HTTP server listening on http://%s:%d", host, port)
    logger.info("Open http://%s:%d in your browser", host, port)
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("HTTP server stopped by user")
    finally:
        httpd.shutdown()


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manual Temperature Controller with Web GUI"
    )
    parser.add_argument(
        "--arduino-port",
        type=str,
        default="COM4",
        help="Arduino serial port (default: COM4)",
    )
    parser.add_argument(
        "--telemetry-url",
        type=str,
        default="http://10.0.63.195:9002",
        help="Telemetry server URL (default: http://10.0.63.195:9002)",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(__file__).parent / "logs_manual",
        help="Directory for log files (default: ./logs_manual)",
    )
    parser.add_argument(
        "--update-interval",
        type=float,
        default=1.0,
        help="Data update interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--http-host",
        type=str,
        default="0.0.0.0",
        help="HTTP server host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=9004,
        help="HTTP server port (default: 9004)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )
    
    args = parser.parse_args()
    
    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    
    # Create controller
    controller = ManualTemperatureController(
        arduino_port=args.arduino_port,
        telemetry_url=args.telemetry_url,
        log_dir=args.log_dir,
        update_interval=args.update_interval,
    )
    
    # Start controller
    try:
        controller.start()
        
        # Run HTTP server (blocks until Ctrl+C)
        run_http_server(
            controller=controller,
            host=args.http_host,
            port=args.http_port,
        )
    
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    
    finally:
        controller.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
