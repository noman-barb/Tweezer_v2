"""Objective Temperature Controller with Web GUI.

This system:
1. Fetches objective temperature from remote telemetry server (10.0.63.195:9002)
2. Uses PI controller to maintain temperature within ±0.1°C (configurable)
3. Controls Arduino (COM4) with PWM (0-0.4) and heating/cooling mode (PIN7)
4. Prevents rapid mode switching (minimum 10 seconds between switches)
5. Provides HTTP server with GET endpoint for setpoint control (no-cache)
6. Web GUI showing: current temps, humidity, time-series plots, setpoint control
7. Maintains CSV log with data persistence and gap detection
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
import serial

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============================================================================
# PID Controller with Deadband and Mode Switching
# ============================================================================

class PIController:
    """PI controller with deadband and heating/cooling mode switching."""
    
    def __init__(
        self,
        setpoint: float = 25.0,
        kp: float = 0.05,
        ki: float = 0.01,
        deadband: float = 0.1,
        pwm_min: float = 0.0,
        pwm_max: float = 0.4,
        mode_switch_delay: float = 10.0,
        heating_pwm_cap: Optional[float] = None,
    ):
        """Initialize PI controller.
        
        Args:
            setpoint: Target temperature in °C
            kp: Proportional gain
            ki: Integral gain
            deadband: Temperature tolerance ±°C
            pwm_min: Minimum PWM value (0.0)
            pwm_max: Maximum PWM value (0.4)
            mode_switch_delay: Minimum seconds between heating/cooling mode switches
        """
        self.setpoint = setpoint
        self.kp = kp
        self.ki = ki
        self.deadband = deadband
        self.pwm_min = pwm_min
        self.pwm_max = pwm_max
        self.mode_switch_delay = mode_switch_delay
        # Limit PWM magnitude during HEATING so large PWM is primarily used for cooling.
        self.heating_pwm_cap = (
            float(heating_pwm_cap) if heating_pwm_cap is not None else pwm_max * 0.5
        )
        
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time: Optional[float] = None
        
        self.current_mode: Optional[str] = None  # "HEATING" or "COOLING"
        self.last_mode_switch_time: Optional[float] = None
        
        self._lock = threading.Lock()
    
    def set_setpoint(self, setpoint: float) -> None:
        """Update temperature setpoint."""
        with self._lock:
            self.setpoint = setpoint
            logger.info("Setpoint updated to %.2f°C", setpoint)
    
    def get_setpoint(self) -> float:
        """Get current setpoint."""
        with self._lock:
            return self.setpoint
    
    def set_kp(self, kp: float) -> None:
        """Update proportional gain."""
        with self._lock:
            self.kp = kp
            logger.info("Kp updated to %.4f", kp)
    
    def get_kp(self) -> float:
        """Get current Kp."""
        with self._lock:
            return self.kp
    
    def set_ki(self, ki: float) -> None:
        """Update integral gain."""
        with self._lock:
            self.ki = ki
            logger.info("Ki updated to %.4f", ki)
    
    def get_ki(self) -> float:
        """Get current Ki."""
        with self._lock:
            return self.ki
    
    def set_deadband(self, deadband: float) -> None:
        """Update deadband."""
        with self._lock:
            self.deadband = deadband
            logger.info("Deadband updated to %.2f°C", deadband)
    
    def get_deadband(self) -> float:
        """Get current deadband."""
        with self._lock:
            return self.deadband

    def set_mode_switch_delay(self, delay: float) -> None:
        """Update minimum seconds between heating/cooling mode switches."""
        with self._lock:
            self.mode_switch_delay = max(0.0, float(delay))
            logger.info("Mode switch delay updated to %.1f s", self.mode_switch_delay)

    def get_mode_switch_delay(self) -> float:
        """Get current mode switch delay in seconds."""
        with self._lock:
            return self.mode_switch_delay

    def set_heating_pwm_cap(self, cap: float) -> None:
        """Set maximum PWM allowed in HEATING mode (<= pwm_max)."""
        with self._lock:
            cap = float(cap)
            self.heating_pwm_cap = max(self.pwm_min, min(cap, self.pwm_max))
            logger.info("Heating PWM cap set to %.3f", self.heating_pwm_cap)

    def get_heating_pwm_cap(self) -> float:
        with self._lock:
            return self.heating_pwm_cap
    
    def reset(self) -> None:
        """Reset controller state."""
        with self._lock:
            self.integral = 0.0
            self.last_error = 0.0
            self.last_time = None
            logger.info("PI controller reset")
    
    def compute(self, current_temp: float) -> Tuple[float, str]:
        """Compute PWM output and heating/cooling mode.
        
        Args:
            current_temp: Current objective temperature in °C
        
        Returns:
            Tuple of (pwm_value, mode) where mode is "HEATING", "COOLING", or "OFF"
        """
        with self._lock:
            current_time = time.time()
            
            # Calculate error
            error = self.setpoint - current_temp
            
            # Check if within deadband
            if abs(error) <= self.deadband:
                # Within tolerance - turn off
                self.integral = 0.0  # Reset integral when in deadband
                self.current_mode = "OFF"  # Update mode state
                return 0.0, "OFF"
            
            # Time delta
            if self.last_time is None:
                dt = 0.0
            else:
                dt = current_time - self.last_time
            
            self.last_time = current_time
            
            # Determine desired mode
            if error > 0:
                desired_mode = "HEATING"
            else:
                desired_mode = "COOLING"
            
            # Check if mode switch is allowed
            if self.current_mode is not None and desired_mode != self.current_mode:
                if self.last_mode_switch_time is not None:
                    time_since_switch = current_time - self.last_mode_switch_time
                    if time_since_switch < self.mode_switch_delay:
                        # Too soon to switch - maintain current mode with zero output
                        logger.debug(
                            "Mode switch delayed (%.1f/%.1f sec)",
                            time_since_switch,
                            self.mode_switch_delay,
                        )
                        return 0.0, self.current_mode
            
            # Update mode if switching
            if desired_mode != self.current_mode:
                logger.info("Mode switching: %s → %s", self.current_mode, desired_mode)
                self.current_mode = desired_mode
                self.last_mode_switch_time = current_time
                self.integral = 0.0  # Reset integral on mode switch
            
            # Proportional term based on error magnitude (mode encodes sign)
            p_term = self.kp * abs(error)

            # Integral zone: integrate only when sufficiently far from setpoint
            # to avoid large PWM for small errors. Use 2x deadband as a simple I-zone.
            i_zone = max(0.0, self.deadband * 2.0)

            if dt > 0:
                if abs(error) > i_zone and self.ki > 0:
                    # Integrate only outside the I-zone
                    self.integral += error * dt
                else:
                    # Apply a small leak toward zero inside I-zone to unwind residual integral
                    leak_per_sec = 0.1  # 10%/s decay inside I-zone
                    decay = max(0.0, min(1.0, leak_per_sec * dt))
                    self.integral *= (1.0 - decay)

            # Dynamic anti-windup: clamp integral contribution to remaining headroom
            if self.ki > 0:
                # Remaining headroom before hitting pwm_max based on P term
                headroom = max(0.0, self.pwm_max - p_term)
                max_integral = headroom / self.ki
                self.integral = max(-max_integral, min(self.integral, max_integral))
            else:
                self.integral = 0.0

            output = p_term + self.ki * self.integral

            # Clamp output strictly within limits
            pwm = max(self.pwm_min, min(output, self.pwm_max))
            # Apply asymmetric limit: reduce max PWM when heating
            if self.current_mode == "HEATING":
                pwm = min(pwm, self.heating_pwm_cap)
            
            self.last_error = error
            
            # Ensure current_mode is set (should always be set by this point)
            assert self.current_mode is not None
            return pwm, self.current_mode


# ============================================================================
# Arduino Serial Interface
# ============================================================================

class ArduinoController:
    """Controls Arduino via serial for PWM and heating/cooling mode."""
    
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 2.0, invert_direction: bool = False):
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
        self.invert_direction: bool = invert_direction

    def set_invert_direction(self, invert: bool) -> None:
        self.invert_direction = bool(invert)
        logger.info("Invert direction set to %s", self.invert_direction)

    def get_invert_direction(self) -> bool:
        return self.invert_direction
    
    def connect(self) -> None:
        """Connect to Arduino."""
        try:
            logger.info("Connecting to Arduino on %s at %d baud", self.port, self.baudrate)
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
            )
            # Wait for Arduino to reset
            time.sleep(2.0)
            
            # Read initial output
            while self.serial.in_waiting > 0:
                line = self.serial.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    logger.info("Arduino: %s", line)
            
            logger.info("Successfully connected to Arduino")
            
        except Exception as exc:
            logger.exception("Failed to connect to Arduino: %s", exc)
            raise
    
    def disconnect(self) -> None:
        """Disconnect from Arduino."""
        if self.serial and self.serial.is_open:
            try:
                # Turn off before disconnecting
                self.set_pwm(0.0)
                time.sleep(0.1)
                self.serial.close()
                logger.info("Disconnected from Arduino")
            except Exception as exc:
                logger.exception("Error during Arduino disconnect: %s", exc)
    
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
                self.serial.flush()
                
                # Read response
                time.sleep(0.05)  # Give Arduino time to respond
                response_lines = []
                while self.serial.in_waiting > 0:
                    line = self.serial.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        response_lines.append(line)
                        logger.debug("Arduino: %s", line)
                
                # Return concatenated response lines; if Arduino doesn't send any
                # response for a valid command, treat it as an empty string to
                # indicate success without a message.
                return "\n".join(response_lines) if response_lines else ""
                
            except Exception as exc:
                logger.exception("Error sending command '%s': %s", command, exc)
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
        # Consider an empty response as success; only None indicates a failure
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
            # Turn off PWM
            return self.set_pwm(0.0)

        elif mode == "HEATING":
            # Determine hardware direction
            desired_state = "LOW" if self.invert_direction else "HIGH"
            # Only change direction if needed to reduce chatter
            changed = False
            if self.current_pin7_state != desired_state:
                if desired_state == "HIGH":
                    if not self.set_pin7_high():
                        return False
                else:
                    if not self.set_pin7_low():
                        return False
                changed = True
            # Small delay after direction change for H-bridge stability
            if changed:
                time.sleep(0.02)
            return self.set_pwm(pwm)

        elif mode == "COOLING":
            # Determine hardware direction
            desired_state = "HIGH" if self.invert_direction else "LOW"
            # Only change direction if needed to reduce chatter
            changed = False
            if self.current_pin7_state != desired_state:
                if desired_state == "HIGH":
                    if not self.set_pin7_high():
                        return False
                else:
                    if not self.set_pin7_low():
                        return False
                changed = True
            # Small delay after direction change for H-bridge stability
            if changed:
                time.sleep(0.02)
            return self.set_pwm(pwm)
        
        else:
            logger.error("Unknown mode: %s", mode)
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
            url = f"{self.base_url}/telemetry"
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return data
        
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
            logger.debug("No telemetry data available")
            return None
        
        measurements = data.get("measurements", {})
        
        if not measurements:
            logger.warning("Telemetry data has no 'measurements' field")
            logger.debug("Available keys: %s", list(data.keys()))
            return None
        
        # Look for objective temperature keys (adjust as needed)
        # Common keys might be: "OBJECTIVE_TEMPERATURE", "OBJ_TEMP", etc.
        obj_temp_keys = [
            "OBJECTIVE_TEMPERATURE_ANALOG_READ",  # Primary: actual objective sensor
            "OBJECTIVE_TEMPERATURE",
            "OBJ_TEMP",
            "OBJECTIVE_TEMP",
            "SHTC3_TEMPERATURE",  # Fallback: room temperature sensor
        ]
        
        for key in obj_temp_keys:
            if key in measurements:
                value = measurements[key]
                if isinstance(value, (int, float)):
                    logger.debug("Found temperature: %s = %.2f°C", key, float(value))
                    return float(value)
        
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
        filename = f"objective_temp_log_{timestamp}.csv"
        self.current_log_file = self.log_dir / filename
        
        # Close previous file if open
        if self.csv_file:
            try:
                self.csv_file.close()
            except:
                pass
        
        # Open new file
        self.csv_file = open(self.current_log_file, 'w', newline='', encoding='utf-8')
        
        fieldnames = [
            "timestamp",
            "objective_temp",
            "setpoint",
            "pwm",
            "mode",
            "room_temp",
            "humidity",
            "error",
        ]
        
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
        self.csv_writer.writeheader()
        self.csv_file.flush()
        
        logger.info("Created new log file: %s", self.current_log_file)
    
    def log_data(
        self,
        objective_temp: Optional[float],
        setpoint: float,
        pwm: float,
        mode: str,
        room_temp: Optional[float] = None,
        humidity: Optional[float] = None,
    ) -> None:
        """Log data point to CSV.
        
        Args:
            objective_temp: Objective temperature in °C
            setpoint: Target temperature in °C
            pwm: PWM duty cycle (0.0 to 0.4)
            mode: "HEATING", "COOLING", or "OFF"
            room_temp: Room temperature in °C
            humidity: Relative humidity in %RH
        """
        with self._lock:
            if not self.csv_writer or not self.csv_file:
                return
            
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "objective_temp": f"{objective_temp:.3f}" if objective_temp is not None else "",
                "setpoint": f"{setpoint:.3f}",
                "pwm": f"{pwm:.4f}",
                "mode": mode,
                "room_temp": f"{room_temp:.2f}" if room_temp is not None else "",
                "humidity": f"{humidity:.2f}" if humidity is not None else "",
                "error": "",
            }
            
            try:
                self.csv_writer.writerow(row)
                self.csv_file.flush()
            except Exception as exc:
                logger.exception("Failed to write log entry: %s", exc)
    
    def close(self) -> None:
        """Close log file."""
        with self._lock:
            if self.csv_file:
                try:
                    self.csv_file.close()
                    logger.info("Closed log file")
                except:
                    pass


# ============================================================================
# Temperature Controller (Main Control Loop)
# ============================================================================

class TemperatureController:
    """Main temperature controller integrating all components."""
    
    def __init__(
        self,
        arduino_port: str,
        telemetry_url: str,
        log_dir: Path,
        setpoint: float = 25.0,
        kp: float = 0.05,
        ki: float = 0.01,
        deadband: float = 0.1,
        control_interval: float = 1.0,
    ):
        """Initialize temperature controller.
        
        Args:
            arduino_port: Arduino serial port
            telemetry_url: Telemetry server URL
            log_dir: Directory for log files
            setpoint: Initial temperature setpoint in °C
            kp: Proportional gain
            ki: Integral gain
            deadband: Temperature tolerance ±°C
            control_interval: Control loop interval in seconds
        """
        self.arduino_port = arduino_port
        self.telemetry_url = telemetry_url
        self.log_dir = log_dir
        self.control_interval = control_interval
        
        # Initialize components
        self.pi_controller = PIController(
            setpoint=setpoint,
            kp=kp,
            ki=ki,
            deadband=deadband,
            pwm_min=0.0,
            pwm_max=0.4,
            mode_switch_delay=10.0,
        )
        
        self.arduino = ArduinoController(port=arduino_port)
        self.telemetry = TelemetryClient(base_url=telemetry_url)
        self.logger = DataLogger(log_dir=log_dir)
        
        # Control loop state
        self.running = False
        self.control_thread: Optional[threading.Thread] = None
        self.pid_enabled = False  # PID control is disabled by default
        
        # Data buffer for GUI (keep last 1000 points)
        self.data_buffer: deque = deque(maxlen=1000)
        self.buffer_lock = threading.Lock()

        # Moving average smoothing for objective temperature
        self.ma_window: int = 1  # 1 = no smoothing
        self._ma_buffer: deque = deque(maxlen=self.ma_window)

    def set_ma_window(self, window: int) -> None:
        """Set moving average window (number of samples). 1 disables smoothing."""
        w = max(1, int(window))
        if w != self.ma_window:
            self.ma_window = w
            self._ma_buffer = deque(list(self._ma_buffer)[-w:], maxlen=w)
            logger.info("Moving average window updated to %d", self.ma_window)

    def get_ma_window(self) -> int:
        """Get current moving average window size."""
        return self.ma_window
    
    def start(self) -> None:
        """Start the temperature controller."""
        if self.running:
            logger.warning("Controller already running")
            return
        
        # Try to connect to Arduino (but don't fail if it's not available yet)
        try:
            self.arduino.connect()
        except Exception as exc:
            logger.error("Failed to connect to Arduino: %s", exc)
            logger.warning("Continuing without Arduino - setpoint/mode changes will not work")
        
        # Start control loop
        self.running = True
        self.control_thread = threading.Thread(target=self._control_loop, daemon=True)
        self.control_thread.start()
        
        logger.info("Temperature controller started")
    
    def stop(self) -> None:
        """Stop the temperature controller."""
        if not self.running:
            return
        
        logger.info("Stopping temperature controller...")
        self.running = False
        
        # Wait for control thread
        if self.control_thread:
            self.control_thread.join(timeout=5.0)
        
        # Disconnect Arduino
        self.arduino.disconnect()
        
        # Close logger
        self.logger.close()
        
        logger.info("Temperature controller stopped")
    
    def _control_loop(self) -> None:
        """Main control loop (runs in separate thread)."""
        logger.info("Control loop started")
        
        while self.running:
            try:
                loop_start = time.time()
                
                # 1. Fetch objective temperature
                raw_temp = self.telemetry.get_objective_temperature()
                # Apply moving average smoothing
                obj_temp = None
                if raw_temp is not None:
                    if self._ma_buffer.maxlen != self.ma_window:
                        self._ma_buffer = deque(maxlen=self.ma_window)
                    self._ma_buffer.append(float(raw_temp))
                    obj_temp = sum(self._ma_buffer) / len(self._ma_buffer)
                
                # 2. Get ambient data
                room_temp, humidity = self.telemetry.get_ambient_data()
                
                # 3. Get setpoint (always available)
                setpoint = self.pi_controller.get_setpoint()
                
                # 4. Initialize default values
                pwm = 0.0
                mode = "OFF"
                
                # 5. Check if PID control is enabled
                if self.pid_enabled and obj_temp is not None:
                    # Compute PI control only if enabled
                    pwm, mode = self.pi_controller.compute(obj_temp)
                    
                    # 6. Send commands to Arduino (only if connected)
                    if self.arduino.serial and self.arduino.serial.is_open:
                        success = self.arduino.set_mode(mode, pwm)
                        if not success:
                            logger.warning("Failed to set Arduino mode/PWM")
                    else:
                        logger.debug("Arduino not connected, skipping control output")
                elif not self.pid_enabled:
                    # PID disabled - keep PWM at 0
                    mode = "STANDBY"
                    if self.arduino.serial and self.arduino.serial.is_open:
                        self.arduino.set_mode("OFF", 0.0)
                    logger.debug("PID control disabled - PWM held at 0.0")
                else:
                    logger.warning("No objective temperature available - displaying ambient data only")
                
                # 7. Log data (even if obj_temp is None)
                self.logger.log_data(
                    objective_temp=obj_temp,
                    setpoint=setpoint,
                    pwm=pwm,
                    mode=mode,
                    room_temp=room_temp,
                    humidity=humidity,
                )
                
                # 8. Update data buffer for GUI (ALWAYS update so GUI shows something)
                data_point = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "objective_temp": obj_temp,
                    "setpoint": setpoint,
                    "pwm": pwm,
                    "mode": mode,
                    "room_temp": room_temp,
                    "humidity": humidity,
                }
                
                with self.buffer_lock:
                    self.data_buffer.append(data_point)
                
                # 9. Log status
                logger.info(
                    "T_obj=%s, Setpoint=%.2f°C, PWM=%.3f, Mode=%s, T_room=%s, RH=%s",
                    f"{obj_temp:.2f}°C" if obj_temp is not None else "N/A",
                    setpoint,
                    pwm,
                    mode,
                    f"{room_temp:.2f}°C" if room_temp is not None else "N/A",
                    f"{humidity:.1f}%%" if humidity is not None else "N/A",
                )
                
                # 10. Sleep for control interval
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.control_interval - elapsed)
                time.sleep(sleep_time)
                
            except Exception as exc:
                logger.exception("Error in control loop: %s", exc)
                # Still update GUI with error state
                error_data_point = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "objective_temp": None,
                    "setpoint": self.pi_controller.get_setpoint(),
                    "pwm": 0.0,
                    "mode": "ERROR",
                    "room_temp": None,
                    "humidity": None,
                }
                with self.buffer_lock:
                    self.data_buffer.append(error_data_point)
                time.sleep(self.control_interval)
        
        logger.info("Control loop stopped")
    
    def set_setpoint(self, setpoint: float) -> None:
        """Update temperature setpoint."""
        self.pi_controller.set_setpoint(setpoint)
    
    def get_setpoint(self) -> float:
        """Get current setpoint."""
        return self.pi_controller.get_setpoint()
    
    def set_control_interval(self, interval: float) -> None:
        """Update control loop interval."""
        self.control_interval = max(0.1, min(interval, 10.0))  # Clamp 0.1-10 seconds
        logger.info("Control interval updated to %.2f seconds", self.control_interval)
    
    def get_control_interval(self) -> float:
        """Get current control interval."""
        return self.control_interval
    
    def enable_pid(self) -> None:
        """Enable PID control."""
        self.pid_enabled = True
        logger.info("PID control ENABLED")
    
    def disable_pid(self) -> None:
        """Disable PID control and set PWM to 0."""
        self.pid_enabled = False
        # Immediately turn off output
        if self.arduino.serial and self.arduino.serial.is_open:
            self.arduino.set_mode("OFF", 0.0)
        logger.info("PID control DISABLED - PWM set to 0.0")
    
    def is_pid_enabled(self) -> bool:
        """Check if PID control is enabled."""
        return self.pid_enabled
    
    def get_controller_params(self) -> Dict[str, Any]:
        """Get all controller parameters."""
        return {
            "setpoint": self.pi_controller.get_setpoint(),
            "kp": self.pi_controller.get_kp(),
            "ki": self.pi_controller.get_ki(),
            "deadband": self.pi_controller.get_deadband(),
            "mode_switch_delay": self.pi_controller.get_mode_switch_delay(),
            "heating_pwm_cap": self.pi_controller.get_heating_pwm_cap(),
            "control_interval": self.control_interval,
            "pid_enabled": self.pid_enabled,
            "ma_window": self.get_ma_window(),
            "invert_direction": self.arduino.get_invert_direction() if self.arduino else False,
        }
    
    def get_current_data(self) -> Dict[str, Any]:
        """Get current controller data for GUI."""
        with self.buffer_lock:
            if self.data_buffer:
                # Return a COPY of the latest data
                latest = self.data_buffer[-1].copy()
            else:
                latest = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "objective_temp": None,
                    "setpoint": self.get_setpoint(),
                    "pwm": 0.0,
                    "mode": "OFF",
                    "room_temp": None,
                    "humidity": None,
                }
            
            # Add PID enabled status
            latest["pid_enabled"] = self.pid_enabled
            return latest
    
    def get_historical_data(self, max_points: int = 1000) -> List[Dict[str, Any]]:
        """Get historical data buffer."""
        with self.buffer_lock:
            return list(self.data_buffer)[-max_points:]
    
    def load_historical_logs(self) -> List[Dict[str, Any]]:
        """Load all historical log files."""
        all_data = []
        
        try:
            log_files = sorted(self.log_dir.glob("objective_temp_log_*.csv"))
            
            for log_file in log_files:
                try:
                    with open(log_file, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            # Parse data
                            data_point = {
                                "timestamp": row.get("timestamp", ""),
                                "objective_temp": float(row["objective_temp"]) if row.get("objective_temp") else None,
                                "setpoint": float(row["setpoint"]) if row.get("setpoint") else None,
                                "pwm": float(row["pwm"]) if row.get("pwm") else 0.0,
                                "mode": row.get("mode", "OFF"),
                                "room_temp": float(row["room_temp"]) if row.get("room_temp") else None,
                                "humidity": float(row["humidity"]) if row.get("humidity") else None,
                            }
                            all_data.append(data_point)
                
                except Exception as exc:
                    logger.error("Failed to load log file %s: %s", log_file, exc)
        
        except Exception as exc:
            logger.exception("Failed to load historical logs: %s", exc)
        
        return all_data


# ============================================================================
# HTTP Server with Web GUI
# ============================================================================

class ControllerHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for temperature controller."""
    
    # Class variables set by server
    controller: Optional[TemperatureController] = None
    
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
        elif path == "/api/setpoint":
            self._serve_setpoint(parsed.query)
        elif path == "/api/params":
            self._serve_params(parsed.query)
        elif path == "/api/pid/enable":
            self._serve_pid_enable()
        elif path == "/api/pid/disable":
            self._serve_pid_disable()
        elif path == "/api/pid/status":
            self._serve_pid_status()
        else:
            self.send_error(404, "Not Found")
    
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
    
    def _serve_health(self) -> None:
        """Serve health/status information for debugging."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        with self.controller.buffer_lock:
            buffer_size = len(self.controller.data_buffer)
            has_data = buffer_size > 0
            latest_data = self.controller.data_buffer[-1] if has_data else None
        
        health = {
            "status": "ok",
            "running": self.controller.running,
            "pid_enabled": self.controller.pid_enabled,
            "buffer_size": buffer_size,
            "has_data": has_data,
            "latest_timestamp": latest_data.get("timestamp") if latest_data else None,
            "arduino_connected": self.controller.arduino.serial is not None and self.controller.arduino.serial.is_open,
        }
        self._send_json(health)
    
    def _serve_current_data(self) -> None:
        """Serve current controller data."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        data = self.controller.get_current_data()
        logger.debug(f"Serving current data: {data}")
        self._send_json(data)
    
    def _serve_history(self) -> None:
        """Serve historical data."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        # Load all historical logs
        all_data = self.controller.load_historical_logs()
        
        # Add current buffer data
        current_data = self.controller.get_historical_data()
        all_data.extend(current_data)
        
        self._send_json({"data": all_data})
    
    def _serve_setpoint(self, query: str) -> None:
        """Get or set temperature setpoint."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        params = parse_qs(query)
        
        # Check if setting new setpoint
        if "value" in params:
            try:
                new_setpoint = float(params["value"][0])
                self.controller.set_setpoint(new_setpoint)
                response = {
                    "setpoint": new_setpoint,
                    "message": f"Setpoint updated to {new_setpoint:.2f}°C",
                }
                self._send_json(response)
            except ValueError:
                self.send_error(400, "Invalid setpoint value")
        else:
            # Return current setpoint
            current_setpoint = self.controller.get_setpoint()
            self._send_json({"setpoint": current_setpoint})
    
    def _serve_params(self, query: str) -> None:
        """Get or set controller parameters."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        params = parse_qs(query)
        
        # Check if updating parameters
        updated = {}
        try:
            if "kp" in params:
                new_kp = float(params["kp"][0])
                self.controller.pi_controller.set_kp(new_kp)
                updated["kp"] = new_kp
            
            if "ki" in params:
                new_ki = float(params["ki"][0])
                self.controller.pi_controller.set_ki(new_ki)
                updated["ki"] = new_ki
            
            if "deadband" in params:
                new_deadband = float(params["deadband"][0])
                self.controller.pi_controller.set_deadband(new_deadband)
                updated["deadband"] = new_deadband
            
            if "mode_switch_delay" in params:
                new_delay = float(params["mode_switch_delay"][0])
                self.controller.pi_controller.set_mode_switch_delay(new_delay)
                updated["mode_switch_delay"] = new_delay

            if "heating_pwm_cap" in params:
                new_cap = float(params["heating_pwm_cap"][0])
                self.controller.pi_controller.set_heating_pwm_cap(new_cap)
                updated["heating_pwm_cap"] = new_cap

            if "control_interval" in params:
                new_interval = float(params["control_interval"][0])
                self.controller.set_control_interval(new_interval)
                updated["control_interval"] = new_interval

            if "ma_window" in params:
                new_ma = int(params["ma_window"][0])
                self.controller.set_ma_window(new_ma)
                updated["ma_window"] = new_ma

            if "invert_direction" in params:
                inv_str = params["invert_direction"][0].strip().lower()
                inv = inv_str in ("1", "true", "yes", "on")
                self.controller.arduino.set_invert_direction(inv)
                updated["invert_direction"] = inv
            
            # Get all current parameters
            current_params = self.controller.get_controller_params()
            
            if updated:
                response = {
                    **current_params,
                    "updated": updated,
                    "message": f"Updated: {', '.join(updated.keys())}",
                }
            else:
                response = current_params
            
            self._send_json(response)
            
        except ValueError as e:
            self.send_error(400, f"Invalid parameter value: {e}")
    
    def _serve_pid_enable(self) -> None:
        """Enable PID control."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        self.controller.enable_pid()
        response = {
            "pid_enabled": True,
            "message": "PID control enabled",
        }
        self._send_json(response)
    
    def _serve_pid_disable(self) -> None:
        """Disable PID control."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        self.controller.disable_pid()
        response = {
            "pid_enabled": False,
            "message": "PID control disabled - PWM set to 0.0",
        }
        self._send_json(response)
    
    def _serve_pid_status(self) -> None:
        """Get PID control status."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        response = {
            "pid_enabled": self.controller.is_pid_enabled(),
        }
        self._send_json(response)
    
    def _serve_gui(self) -> None:
        """Serve web GUI."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Objective Temperature Controller</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.js"></script>
    <!-- Time scale in Chart.js requires an adapter; use date-fns adapter -->
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
            padding: 20px 30px;
            background: white;
            border-bottom: 2px solid #e9ecef;
        }
        
        .control-group {
            display: flex;
            align-items: center;
            gap: 15px;
            flex-wrap: wrap;
        }
        
        .control-group label {
            font-weight: 600;
            color: #555;
        }
        
        .control-group input[type="number"] {
            padding: 10px 15px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 1em;
            width: 120px;
            transition: border-color 0.3s;
        }
        
        .control-group input[type="number"]:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .control-group button {
            padding: 10px 25px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 1em;
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
        
        .loading {
            text-align: center;
            padding: 40px;
            font-size: 1.2em;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🌡️ Objective Temperature Controller</h1>
            <p></p>
        </header>
        
        <div class="status-bar">
            <div class="status-item">
                <h3>Objective Temperature</h3>
                <div class="value" id="obj-temp">--</div>
            </div>
            <div class="status-item">
                <h3>Setpoint</h3>
                <div class="value" id="setpoint">--</div>
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
                <div class="value" id="mode">--</div>
            </div>
            <div class="status-item">
                <h3>PWM Output</h3>
                <div class="value" id="pwm">--</div>
            </div>
        </div>
        
        <div class="controls">
            <div class="control-group" style="justify-content: center; padding: 10px; background: #f0f0f0; border-radius: 8px; margin-bottom: 15px;">
                <button id="pid-toggle-btn" onclick="togglePID()" style="font-size: 1.2em; padding: 15px 40px; min-width: 200px;">
                    ▶️ Run PID
                </button>
                <span id="pid-status" style="margin-left: 20px; font-weight: bold; font-size: 1.1em; color: #999;">STANDBY</span>
            </div>
            
            <div class="control-group">
                <label for="setpoint-input">Set Temperature:</label>
                <input type="number" id="setpoint-input" step="0.1" min="15" max="35" value="25.0">
                <span class="unit">°C</span>
                <button onclick="updateSetpoint()">Update Setpoint</button>
            </div>
            
            <div class="control-group" style="margin-top: 15px;">
                <label for="kp-input">Kp (Proportional):</label>
                <input type="number" id="kp-input" step="0.001" min="0" max="1" value="0.05">
                
                <label for="ki-input" style="margin-left: 20px;">Ki (Integral):</label>
                <input type="number" id="ki-input" step="any" min="0" max="1" value="0.01">
                
                <label for="interval-input" style="margin-left: 20px;">Interval (s):</label>
                <input type="number" id="interval-input" step="0.1" min="0.1" max="10" value="1.0">
                
                <label for="deadband-input" style="margin-left: 20px;">Deadband (°C):</label>
                <input type="number" id="deadband-input" step="0.05" min="0" max="2" value="0.1">

                <label for="mode-delay-input" style="margin-left: 20px;">Mode Delay (s):</label>
                <input type="number" id="mode-delay-input" step="1" min="0" max="120" value="10">

                <label for="ma-window-input" style="margin-left: 20px;">MA Window (samples):</label>
                <input type="number" id="ma-window-input" step="1" min="1" max="600" value="1">

                <label for="heating-cap-input" style="margin-left: 20px;">Heating PWM Cap:</label>
                <input type="number" id="heating-cap-input" step="0.01" min="0" max="0.4" placeholder="0.20">

                <label for="invert-dir-input" style="margin-left: 20px;">Invert Direction:</label>
                <input type="checkbox" id="invert-dir-input">
                
                <button onclick="updateParams()">Update Parameters</button>
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
        
        // Chart data
        let tempChart, controlChart;
        let historicalData = [];
        
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
                            label: 'Setpoint',
                            data: [],
                            borderColor: '#ff6b6b',
                            borderWidth: 2,
                            borderDash: [5, 5],
                            pointRadius: 0,
                            fill: false,
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
            console.log('Updating status with data:', data);
            
            try {
                document.getElementById('obj-temp').innerHTML = 
                    data.objective_temp !== null ? `${data.objective_temp.toFixed(2)}<span class="unit">°C</span>` : '--';
                
                document.getElementById('setpoint').innerHTML = 
                    `${data.setpoint.toFixed(2)}<span class="unit">°C</span>`;
                
                document.getElementById('room-temp').innerHTML = 
                    data.room_temp !== null ? `${data.room_temp.toFixed(2)}<span class="unit">°C</span>` : '--';
                
                document.getElementById('humidity').innerHTML = 
                    data.humidity !== null ? `${data.humidity.toFixed(1)}<span class="unit">%RH</span>` : '--';
                
                const modeElement = document.getElementById('mode');
                modeElement.textContent = data.mode;
                modeElement.className = 'value mode-' + data.mode.toLowerCase();
                
                document.getElementById('pwm').innerHTML = 
                    `${data.pwm.toFixed(3)}<span class="unit"></span>`;
                
                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
                
                console.log('Status update complete');
            } catch (error) {
                console.error('Error updating status:', error);
            }
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
            
            tempChart.data.datasets[1].data.push({
                x: timestamp,
                y: data.setpoint
            });
            
            // Control chart
            controlChart.data.datasets[0].data.push({
                x: timestamp,
                y: data.pwm
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
                const response = await fetch('/api/history');
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
                        
                        tempChart.data.datasets[1].data.push({
                            x: timestamp,
                            y: point.setpoint
                        });
                        
                        controlChart.data.datasets[0].data.push({
                            x: timestamp,
                            y: point.pwm
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
                console.log('Fetching current data from /api/current...');
                const response = await fetch('/api/current');
                console.log('Response status:', response.status);
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                console.log('Received data:', data);
                
                updateStatus(data);
                updateCharts(data);
            } catch (error) {
                console.error('Error fetching data:', error);
                console.error('Error details:', error.message);
            }
        }
        
        // Update setpoint
        async function updateSetpoint() {
            const input = document.getElementById('setpoint-input');
            const value = parseFloat(input.value);
            
            if (isNaN(value)) {
                alert('Invalid setpoint value');
                return;
            }
            
            try {
                const response = await fetch(`/api/setpoint?value=${value}`);
                const data = await response.json();
                
                alert(data.message || 'Setpoint updated');
            } catch (error) {
                console.error('Error updating setpoint:', error);
                alert('Failed to update setpoint');
            }
        }
        
        // Update controller parameters
        async function updateParams() {
            const kp = parseFloat(document.getElementById('kp-input').value);
            const ki = parseFloat(document.getElementById('ki-input').value);
            const interval = parseFloat(document.getElementById('interval-input').value);
            const deadband = parseFloat(document.getElementById('deadband-input').value);
            const modeDelay = parseFloat(document.getElementById('mode-delay-input').value);
            const maWindow = parseInt(document.getElementById('ma-window-input').value);
            const invertDir = document.getElementById('invert-dir-input').checked;
            
            if (isNaN(kp) || isNaN(ki) || isNaN(interval) || isNaN(deadband) || isNaN(modeDelay) || isNaN(maWindow)) {
                alert('Invalid parameter values');
                return;
            }
            
            try {
                const heatingCap = parseFloat(document.getElementById('heating-cap-input')?.value || '');
                const queryParts = [
                    `kp=${kp}`,
                    `ki=${ki}`,
                    `control_interval=${interval}`,
                    `deadband=${deadband}`,
                    `mode_switch_delay=${modeDelay}`,
                    `ma_window=${maWindow}`,
                    `invert_direction=${invertDir}`,
                ];
                if (!isNaN(heatingCap)) queryParts.push(`heating_pwm_cap=${heatingCap}`);
                const response = await fetch(`/api/params?${queryParts.join('&')}`);
                const data = await response.json();
                
                alert(data.message || 'Parameters updated');
            } catch (error) {
                console.error('Error updating parameters:', error);
                alert('Failed to update parameters');
            }
        }
        
        // Load current parameters
        async function loadParameters() {
            try {
                const response = await fetch('/api/params');
                const data = await response.json();
                
                // Update input fields with current values
                document.getElementById('setpoint-input').value = data.setpoint.toFixed(1);
                document.getElementById('kp-input').value = data.kp.toFixed(3);
                // Preserve small values like 0.00015 without rounding away
                document.getElementById('ki-input').value = (typeof data.ki === 'number') ? String(data.ki) : data.ki;
                document.getElementById('interval-input').value = data.control_interval.toFixed(1);
                if (typeof data.deadband === 'number') document.getElementById('deadband-input').value = data.deadband.toFixed(2);
                if (typeof data.mode_switch_delay === 'number') document.getElementById('mode-delay-input').value = data.mode_switch_delay.toFixed(0);
                if (typeof data.ma_window === 'number') document.getElementById('ma-window-input').value = data.ma_window;
                if (typeof data.invert_direction === 'boolean') document.getElementById('invert-dir-input').checked = data.invert_direction;
                if (typeof data.heating_pwm_cap === 'number') {
                    const el = document.getElementById('heating-cap-input');
                    if (el) el.value = data.heating_pwm_cap.toFixed(3);
                }
            } catch (error) {
                console.error('Error loading parameters:', error);
            }
        }
        
        // Load PID status and update button
        async function loadPIDStatus() {
            try {
                const response = await fetch('/api/pid/status');
                const data = await response.json();
                
                const button = document.getElementById('pid-toggle-btn');
                const statusSpan = document.getElementById('pid-status');
                
                if (data.pid_enabled) {
                    button.textContent = '⏹️ Stop PID';
                    button.style.background = 'linear-gradient(135deg, #dc3545 0%, #c82333 100%)';
                    button.style.color = 'white';
                    statusSpan.textContent = 'RUNNING';
                    statusSpan.style.color = '#28a745';
                } else {
                    button.textContent = '▶️ Run PID';
                    button.style.background = 'linear-gradient(135deg, #28a745 0%, #218838 100%)';
                    button.style.color = 'white';
                    statusSpan.textContent = 'STANDBY';
                    statusSpan.style.color = '#999';
                }
            } catch (error) {
                console.error('Error loading PID status:', error);
            }
        }
        
        // Toggle PID control
        async function togglePID() {
            try {
                const statusResponse = await fetch('/api/pid/status');
                const statusData = await statusResponse.json();
                
                const endpoint = statusData.pid_enabled ? '/api/pid/disable' : '/api/pid/enable';
                const response = await fetch(endpoint);
                const data = await response.json();
                
                alert(data.message || 'PID control toggled');
                
                // Update button state
                await loadPIDStatus();
            } catch (error) {
                console.error('Error toggling PID:', error);
                alert('Failed to toggle PID control');
            }
        }
        
        // Initialize
        window.onload = async function() {
            console.log('=== Dashboard Initializing ===');
            console.log('UPDATE_INTERVAL:', UPDATE_INTERVAL);
            
            try {
                console.log('Initializing charts...');
                initCharts();
                
                console.log('Loading parameters...');
                await loadParameters();
                
                console.log('Loading PID status...');
                await loadPIDStatus();
                
                console.log('Loading historical data...');
                await loadHistoricalData();
                
                console.log('Starting periodic updates...');
                setInterval(fetchCurrentData, UPDATE_INTERVAL);
                setInterval(loadPIDStatus, UPDATE_INTERVAL);
                
                console.log('Fetching initial current data...');
                await fetchCurrentData();
                
                console.log('=== Dashboard Initialized Successfully ===');
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
    controller: TemperatureController,
    host: str = "0.0.0.0",
    port: int = 9003,
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
        logger.info("Shutting down HTTP server...")
    finally:
        httpd.shutdown()


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Objective Temperature Controller with Web GUI"
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
        default=Path(__file__).parent / "logs",
        help="Directory for log files (default: ./logs)",
    )
    parser.add_argument(
        "--setpoint",
        type=float,
        default=22.0,
        help="Initial temperature setpoint in °C (default: 25.0)",
    )
    parser.add_argument(
        "--kp",
        type=float,
        default=0.025,
        help="Proportional gain (default: 0.05)",
    )
    parser.add_argument(
        "--ki",
        type=float,
        default=0.00015,
        help="Integral gain (default: 0.01)",
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=0.25,
        help="Temperature deadband ±°C (default: 0.1)",
    )
    parser.add_argument(
        "--mode-switch-delay",
        type=float,
        default=20.0,
        help="Minimum seconds between heating/cooling mode switches (default: 10.0)",
    )
    parser.add_argument(
        "--heating-pwm-cap",
        type=float,
        default=None,
        help="Optional cap on PWM during HEATING (<= pwm_max, default: 50% of pwm_max)",
    )
    parser.add_argument(
        "--ma-window",
        type=int,
        default=10,
        help="Moving average window (samples) for objective temperature (default: 1 = off)",
    )
    parser.add_argument(
        "--invert-direction",
        action="store_true",
        help="Invert H-bridge direction mapping (heating uses LOW, cooling uses HIGH)",
    )
    parser.add_argument(
        "--control-interval",
        type=float,
        default=2.0,
        help="Control loop interval in seconds (default: 1.0)",
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
        default=9003,
        help="HTTP server port (default: 9003)",
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
    controller = TemperatureController(
        arduino_port=args.arduino_port,
        telemetry_url=args.telemetry_url,
        log_dir=args.log_dir,
        setpoint=args.setpoint,
        kp=args.kp,
        ki=args.ki,
        deadband=args.deadband,
        control_interval=args.control_interval,
    )
    # Apply PI extra and smoothing
    controller.pi_controller.set_mode_switch_delay(args.mode_switch_delay)
    if args.heating_pwm_cap is not None:
        controller.pi_controller.set_heating_pwm_cap(args.heating_pwm_cap)
    controller.set_ma_window(args.ma_window)
    controller.arduino.set_invert_direction(args.invert_direction)
    
    # Start controller
    try:
        controller.start()
        
        # Run HTTP server (blocks until interrupted)
        run_http_server(
            controller,
            host=args.http_host,
            port=args.http_port,
        )
    
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    
    finally:
        # Cleanup
        logger.info("Shutting down...")
        controller.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
