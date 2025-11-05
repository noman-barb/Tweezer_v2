"""Objective Temperature Controller with Web GUI.

This system:
1. Fetches objective temperature from remote telemetry server (10.0.63.195:9002)
2. Uses PI controller to maintain temperature within ±0.1°C (configurable)
3. Controls Arduino (COM4) with PWM (0-0.4) and heating/cooling mode (PIN7)
4. Prevents rapid mode switching (minimum 10 seconds between switches)
5. Provides HTTP server with GET endpoint for setpoint control (no-cache)
6. Web GUI showing: current temps, humidity, time-series plots, setpoint control
7. Maintains CSV log with data persistence and gap detection
8. Learns asymmetric heat-loss gains for feed-forward PWM compensation
9. Provides per-mode PI gains plus predictive lead settings via HTTP API
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
        kp_heating: Optional[float] = None,
        ki_heating: Optional[float] = None,
        kp_cooling: Optional[float] = None,
        ki_cooling: Optional[float] = None,
    deadband: float = 0.1,
    deadband_heating: Optional[float] = None,
    deadband_cooling: Optional[float] = None,
        pwm_min: float = 0.0,
        pwm_max: float = 0.4,
        mode_switch_delay: float = 10.0,
        heating_pwm_cap: Optional[float] = None,
        near_setpoint_threshold: float = 1.0,  # New parameter for two-tier control
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
        self.kp = float(kp)
        self.ki = float(ki)
        self.kp_heating = float(kp_heating) if kp_heating is not None else float(kp)
        self.ki_heating = float(ki_heating) if ki_heating is not None else float(ki)
        self.kp_cooling = float(kp_cooling) if kp_cooling is not None else float(kp)
        self.ki_cooling = float(ki_cooling) if ki_cooling is not None else float(ki)
        base_deadband = max(0.0, float(deadband))
        self.deadband = base_deadband
        self.deadband_heating = (
            max(0.0, float(deadband_heating))
            if deadband_heating is not None
            else base_deadband
        )
        self.deadband_cooling = (
            max(0.0, float(deadband_cooling))
            if deadband_cooling is not None
            else base_deadband
        )
        self.deadband = max(self.deadband_heating, self.deadband_cooling)
        self.pwm_min = pwm_min
        self.pwm_max = pwm_max
        self.mode_switch_delay = mode_switch_delay
        # Limit PWM magnitude during HEATING so large PWM is primarily used for cooling.
        self.heating_pwm_cap = (
            float(heating_pwm_cap) if heating_pwm_cap is not None else pwm_max * 0.5
        )
        
        # Two-tier control threshold
        self.near_setpoint_threshold = max(0.1, float(near_setpoint_threshold))
        
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time: Optional[float] = None
        
        self.current_mode: Optional[str] = None  # "HEATING" or "COOLING"
        self.last_mode_switch_time: Optional[float] = None
        
        # Temperature history for trend detection (used in near-setpoint control)
        self.temp_history: deque = deque(maxlen=10)  # Keep last 10 temperature readings
        self.temp_time_history: deque = deque(maxlen=10)  # Corresponding timestamps
        
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
            self.kp = float(kp)
            self.kp_heating = self.kp
            self.kp_cooling = self.kp
            logger.info("Kp updated to %.4f", kp)
    
    def get_kp(self) -> float:
        """Get current Kp."""
        with self._lock:
            return self.kp

    def set_kp_heating(self, kp: float) -> None:
        with self._lock:
            self.kp_heating = float(kp)
            self.kp = self.kp_heating
            logger.info("Heating Kp updated to %.4f", self.kp_heating)

    def get_kp_heating(self) -> float:
        with self._lock:
            return self.kp_heating

    def set_kp_cooling(self, kp: float) -> None:
        with self._lock:
            self.kp_cooling = float(kp)
            logger.info("Cooling Kp updated to %.4f", self.kp_cooling)

    def get_kp_cooling(self) -> float:
        with self._lock:
            return self.kp_cooling
    
    def set_ki(self, ki: float) -> None:
        """Update integral gain."""
        with self._lock:
            self.ki = float(ki)
            self.ki_heating = self.ki
            self.ki_cooling = self.ki
            logger.info("Ki updated to %.4f", ki)
    
    def get_ki(self) -> float:
        """Get current Ki."""
        with self._lock:
            return self.ki

    def set_ki_heating(self, ki: float) -> None:
        with self._lock:
            self.ki_heating = float(ki)
            self.ki = self.ki_heating
            logger.info("Heating Ki updated to %.4f", self.ki_heating)

    def get_ki_heating(self) -> float:
        with self._lock:
            return self.ki_heating

    def set_ki_cooling(self, ki: float) -> None:
        with self._lock:
            self.ki_cooling = float(ki)
            logger.info("Cooling Ki updated to %.4f", self.ki_cooling)

    def get_ki_cooling(self) -> float:
        with self._lock:
            return self.ki_cooling
    
    def set_deadband(self, deadband: float) -> None:
        """Update deadband."""
        with self._lock:
            value = max(0.0, float(deadband))
            self.deadband = value
            self.deadband_heating = value
            self.deadband_cooling = value
            logger.info("Deadband updated to %.2f°C (symmetric)", value)
    
    def get_deadband(self) -> float:
        """Get current deadband."""
        with self._lock:
            return self.deadband

    def set_deadband_heating(self, deadband: float) -> None:
        with self._lock:
            self.deadband_heating = max(0.0, float(deadband))
            self.deadband = max(self.deadband_heating, self.deadband_cooling)
            logger.info(
                "Heating-side deadband updated to %.2f°C (cooling=%.2f°C)",
                self.deadband_heating,
                self.deadband_cooling,
            )

    def get_deadband_heating(self) -> float:
        with self._lock:
            return self.deadband_heating

    def set_deadband_cooling(self, deadband: float) -> None:
        with self._lock:
            self.deadband_cooling = max(0.0, float(deadband))
            self.deadband = max(self.deadband_heating, self.deadband_cooling)
            logger.info(
                "Cooling-side deadband updated to %.2f°C (heating=%.2f°C)",
                self.deadband_cooling,
                self.deadband_heating,
            )

    def get_deadband_cooling(self) -> float:
        with self._lock:
            return self.deadband_cooling

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

    def get_pwm_max(self) -> float:
        with self._lock:
            return self.pwm_max

    def get_pwm_min(self) -> float:
        with self._lock:
            return self.pwm_min

    def set_near_setpoint_threshold(self, threshold: float) -> None:
        """Set the threshold for near-setpoint control mode."""
        with self._lock:
            self.near_setpoint_threshold = max(0.1, float(threshold))
            logger.info("Near-setpoint threshold updated to %.2f°C", self.near_setpoint_threshold)

    def get_near_setpoint_threshold(self) -> float:
        """Get current near-setpoint threshold."""
        with self._lock:
            return self.near_setpoint_threshold
    
    def reset(self) -> None:
        """Reset controller state."""
        with self._lock:
            self.integral = 0.0
            self.last_error = 0.0
            self.last_time = None
            self.temp_history.clear()
            self.temp_time_history.clear()
            self.integral = 0.0
            self.last_error = 0.0
            self.last_time = None
            logger.info("PI controller reset")
    
    def _pwm_limit_for_mode(self, mode: str) -> float:
        if mode == "HEATING":
            return self.heating_pwm_cap
        return self.pwm_max

    def _calculate_temp_trend(self) -> Optional[float]:
        """Calculate temperature trend (°C/s) from recent history.
        
        Returns:
            Temperature rate of change in °C/s, or None if insufficient data
        """
        if len(self.temp_history) < 3:
            return None
        
        # Use linear regression for more robust trend estimation
        temps = list(self.temp_history)
        times = list(self.temp_time_history)
        
        n = len(temps)
        sum_t = sum(times)
        sum_temp = sum(temps)
        sum_t_temp = sum(t * temp for t, temp in zip(times, temps))
        sum_t_sq = sum(t * t for t in times)
        
        denominator = n * sum_t_sq - sum_t * sum_t
        if abs(denominator) < 1e-9:
            return None
        
        # Slope of linear fit (°C/s)
        slope = (n * sum_t_temp - sum_t * sum_temp) / denominator
        return slope

    def compute(
        self,
        current_temp: float,
        baseline_pwm: float = 0.0,
        baseline_mode: Optional[str] = None,
    ) -> Tuple[float, str]:
        """Compute PWM output and heating/cooling mode with two-tier control strategy.
        
        Uses aggressive control when far from setpoint (>1°C) and gentle trend-aware
        control when near setpoint (≤1°C) to prevent oscillations.
        
        Args:
            current_temp: Current objective temperature in °C
            baseline_pwm: Feed-forward PWM contribution (same scale as output)
            baseline_mode: Optional preferred mode for the feed-forward term
        
        Returns:
            Tuple of (pwm_value, mode) where mode is "HEATING", "COOLING", or "OFF"
        """
        with self._lock:
            current_time = time.time()
            
            # Update temperature history for trend detection
            self.temp_history.append(current_temp)
            self.temp_time_history.append(current_time)
            
            baseline_pwm = max(self.pwm_min, min(float(baseline_pwm), self.pwm_max))
            if baseline_pwm <= self.pwm_min + 1e-9:
                baseline_pwm = 0.0
            if baseline_pwm == 0.0 or baseline_mode not in ("HEATING", "COOLING"):
                baseline_mode = None
            
            # Calculate error
            error = self.setpoint - current_temp
            abs_error = abs(error)
            active_deadband = self.deadband_heating if error >= 0 else self.deadband_cooling
            within_deadband = abs_error <= active_deadband

            # Determine if we're near the setpoint (two-tier control)
            near_setpoint = abs_error <= self.near_setpoint_threshold
            
            # Calculate temperature trend
            temp_trend = self._calculate_temp_trend()  # °C/s

            if baseline_mode == "HEATING" and error < -self.deadband_cooling:
                logger.debug(
                    "Discarding heating feed-forward (err=%.3f°C requires cooling)",
                    error,
                )
                baseline_mode = None
                baseline_pwm = 0.0
            elif baseline_mode == "COOLING" and error > self.deadband_heating:
                logger.debug(
                    "Discarding cooling feed-forward (err=%.3f°C requires heating)",
                    error,
                )
                baseline_mode = None
                baseline_pwm = 0.0

            preferred_mode = None
            if baseline_mode is not None:
                preferred_mode = baseline_mode
            elif error > 0:
                preferred_mode = "HEATING"
            else:
                preferred_mode = "COOLING"

            # ========================================================================
            # TWO-TIER CONTROL STRATEGY
            # ========================================================================
            
            # TIER 1: Near setpoint (≤1°C) - Gentle, trend-aware control
            if near_setpoint:
                logger.debug("Near setpoint mode: err=%.3f°C, trend=%.4f°C/s", 
                            error, temp_trend if temp_trend is not None else 0.0)
                
                if within_deadband:
                    # Within deadband: only apply feed-forward if available
                    if baseline_pwm > 0.0:
                        target_mode = preferred_mode
                        if target_mode not in ("HEATING", "COOLING"):
                            target_mode = "HEATING" if self.setpoint >= current_temp else "COOLING"

                        if self.current_mode is not None and target_mode != self.current_mode:
                            if self.last_mode_switch_time is not None:
                                time_since_switch = current_time - self.last_mode_switch_time
                                if time_since_switch < self.mode_switch_delay:
                                    logger.debug(
                                        "Mode switch delayed (%.1f/%.1f sec)",
                                        time_since_switch,
                                        self.mode_switch_delay,
                                    )
                                    self.last_error = error
                                    return 0.0, self.current_mode
                        if target_mode != self.current_mode:
                            logger.info("Mode switching: %s → %s", self.current_mode, target_mode)
                            self.current_mode = target_mode
                            self.last_mode_switch_time = current_time
                        self.integral = 0.0
                        assert self.current_mode in ("HEATING", "COOLING")
                        pwm_limit = self._pwm_limit_for_mode(self.current_mode)
                        pwm = min(baseline_pwm, pwm_limit)
                        self.last_error = error
                        return pwm, self.current_mode

                    self.integral = 0.0
                    self.current_mode = "OFF"
                    self.last_error = error
                    return 0.0, "OFF"
                
                # Near setpoint but outside deadband: use trend-aware control
                # Check if temperature is moving toward setpoint
                if temp_trend is not None:
                    # Predict where temperature will be in next 5 seconds
                    predicted_temp = current_temp + temp_trend * 5.0
                    predicted_error = self.setpoint - predicted_temp
                    
                    # If trend is taking us toward setpoint, reduce control effort
                    if abs(predicted_error) < abs(error):
                        # Moving toward setpoint - be gentle
                        if abs(predicted_error) <= active_deadband:
                            # Predicted to enter deadband - turn off to coast
                            logger.debug("Coasting: predicted to reach setpoint (pred_err=%.3f°C)", 
                                        predicted_error)
                            self.integral = 0.0
                            self.current_mode = "OFF"
                            self.last_error = error
                            return 0.0, "OFF"
                        else:
                            # Still approaching but not there yet - use reduced gain
                            logger.debug("Gentle approach: reducing gains by 50%% (pred_err=%.3f°C)", 
                                        predicted_error)
                            gain_reduction = 0.5  # Reduce gains by 50%
                    else:
                        # Moving away from setpoint or staying stable - use normal gains
                        logger.debug("Not approaching setpoint: using normal gains (pred_err=%.3f°C)", 
                                    predicted_error)
                        gain_reduction = 1.0
                else:
                    # No trend data - use normal but slightly reduced gains
                    logger.debug("No trend data: using 70%% gains")
                    gain_reduction = 0.7
            
            # TIER 2: Far from setpoint (>1°C) - Aggressive control
            else:
                logger.debug("Far from setpoint mode: err=%.3f°C (aggressive control)", abs_error)
                gain_reduction = 1.0  # Full gains
            
            # ========================================================================
            # STANDARD PI CONTROL (with potentially reduced gains)
            # ========================================================================
            
            # Time delta
            if self.last_time is None:
                dt = 0.0
            else:
                dt = current_time - self.last_time
            
            self.last_time = current_time
            
            # Determine desired mode
            desired_mode = preferred_mode
            
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
                        desired_mode = self.current_mode
                        if baseline_mode != desired_mode:
                            baseline_pwm = 0.0
            
            # Update mode if switching
            if desired_mode != self.current_mode:
                logger.info("Mode switching: %s → %s", self.current_mode, desired_mode)
                self.current_mode = desired_mode
                self.last_mode_switch_time = current_time
                self.integral = 0.0  # Reset integral on mode switch
            
            # Select gains and limits for the active mode
            assert self.current_mode in ("HEATING", "COOLING")
            if self.current_mode == "HEATING":
                current_kp = self.kp_heating
                current_ki = self.ki_heating
            else:
                current_kp = self.kp_cooling
                current_ki = self.ki_cooling
            pwm_limit = self._pwm_limit_for_mode(self.current_mode)
            
            # Apply gain reduction for near-setpoint control
            # (gain_reduction was set above based on two-tier logic)
            current_kp = current_kp * gain_reduction
            current_ki = current_ki * gain_reduction
            
            # Proportional term based on error magnitude (mode encodes sign)
            p_term = current_kp * abs_error

            # Integral zone: integrate only when sufficiently far from setpoint
            # to avoid large PWM for small errors. Use 2x deadband as a simple I-zone.
            # Pick I-zone per active mode to make asymmetry consistent
            if self.current_mode == "HEATING":
                mode_deadband = self.deadband_heating
            elif self.current_mode == "COOLING":
                mode_deadband = self.deadband_cooling
            else:
                mode_deadband = max(self.deadband_heating, self.deadband_cooling)
            i_zone = max(0.0, mode_deadband * 2.0)

            if dt > 0:
                if abs_error > i_zone and current_ki > 0:
                    # Integrate only outside the I-zone using magnitude so cooling still ramps PWM
                    self.integral += abs_error * dt
                else:
                    # Apply a small leak toward zero inside I-zone to unwind residual integral
                    leak_per_sec = 0.1  # 10%/s decay inside I-zone
                    decay = max(0.0, min(1.0, leak_per_sec * dt))
                    self.integral *= (1.0 - decay)

            # Dynamic anti-windup: clamp integral contribution to remaining headroom
            if current_ki > 0:
                # Remaining headroom before hitting pwm limit based on P term and feed-forward
                headroom = max(0.0, pwm_limit - baseline_pwm - p_term)
                max_integral = headroom / current_ki
                self.integral = max(0.0, min(self.integral, max_integral))
            else:
                self.integral = 0.0

            feedback = p_term + current_ki * self.integral
            output = baseline_pwm + feedback

            # Clamp output strictly within limits
            pwm = max(self.pwm_min, min(output, pwm_limit))
            
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

    def apply_manual_output(self, direction: str, pwm: float) -> bool:
        """Apply raw direction (PIN7 state) and PWM without controller logic."""
        direction = direction.strip().upper()
        pwm = max(0.0, min(float(pwm), 0.4))

        if direction == "OFF":
            return self.set_pwm(0.0)

        if direction not in ("HIGH", "LOW"):
            logger.error("Unknown manual direction: %s", direction)
            return False

        desired_state = direction
        changed = False

        if self.current_pin7_state != desired_state:
            if desired_state == "HIGH":
                if not self.set_pin7_high():
                    return False
            else:
                if not self.set_pin7_low():
                    return False
            changed = True

        if changed:
            time.sleep(0.02)

        return self.set_pwm(pwm)


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
            "pwm_feedforward",
            "pwm_feedforward_target",
            "pwm_feedback",
            "feedforward_mode",
            "temp_rate",
            "lead_seconds",
            "control_source",
            "manual_override",
            "manual_direction",
            "manual_pwm",
            "manual_requested_direction",
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
        error: Optional[float] = None,
        pwm_feedforward: float = 0.0,
        pwm_feedforward_target: float = 0.0,
        pwm_feedback: float = 0.0,
        feedforward_mode: Optional[str] = None,
        temp_rate: Optional[float] = None,
        lead_seconds: Optional[float] = None,
        control_source: str = "PID",
        manual_override: bool = False,
        manual_direction: Optional[str] = None,
        manual_pwm: float = 0.0,
        manual_requested_direction: Optional[str] = None,
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
                "error": f"{error:.3f}" if error is not None else "",
                "pwm_feedforward": f"{pwm_feedforward:.4f}",
                "pwm_feedforward_target": f"{pwm_feedforward_target:.4f}",
                "pwm_feedback": f"{pwm_feedback:.4f}",
                "feedforward_mode": feedforward_mode or "",
                "temp_rate": f"{temp_rate:.5f}" if temp_rate is not None else "",
                "lead_seconds": f"{lead_seconds:.3f}" if lead_seconds is not None else "",
                "control_source": control_source,
                "manual_override": "1" if manual_override else "0",
                "manual_direction": manual_direction or "",
                "manual_pwm": f"{manual_pwm:.4f}" if manual_pwm is not None else "",
                "manual_requested_direction": manual_requested_direction or "",
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
# Feed-forward Heat Loss Estimator
# ============================================================================

class HeatLossEstimator:
    """Estimate steady-state PWM needed to offset heat loss."""

    def __init__(
        self,
        alpha: float = 0.08,
        min_delta: float = 0.2,
        min_pwm: float = 0.005,
        steady_error: float = 0.05,
        steady_rate: float = 0.015,
        max_gain: float = 5.0,
        gain_heating: float = 0.0,
        gain_cooling: float = 0.0,
    ) -> None:
        self.alpha = float(alpha)
        self.min_delta = float(min_delta)
        self.min_pwm = float(min_pwm)
        self.steady_error = float(steady_error)
        self.steady_rate = float(steady_rate)
        self.max_gain = float(max_gain)
        self.gain_heating = max(0.0, float(gain_heating))
        self.gain_cooling = max(0.0, float(gain_cooling))
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.gain_heating = 0.0
            self.gain_cooling = 0.0

    def _smooth_update(self, current: float, new: float) -> float:
        if current == 0.0:
            return new
        return (1.0 - self.alpha) * current + self.alpha * new

    def update(
        self,
        mode: Optional[str],
        pwm: float,
        setpoint: Optional[float],
        room_temp: Optional[float],
        error: Optional[float],
        temp_rate: Optional[float],
    ) -> None:
        if mode not in ("HEATING", "COOLING"):
            return
        if setpoint is None or room_temp is None:
            return
        if pwm < self.min_pwm:
            return
        if error is None or abs(error) > self.steady_error:
            return
        if temp_rate is not None and abs(temp_rate) > self.steady_rate:
            return

        with self._lock:
            if mode == "HEATING":
                delta = setpoint - room_temp
                if delta <= self.min_delta:
                    return
                gain = min(self.max_gain, pwm / delta)
                self.gain_heating = self._smooth_update(self.gain_heating, gain)
            else:
                delta = room_temp - setpoint
                if delta <= self.min_delta:
                    return
                gain = min(self.max_gain, pwm / delta)
                self.gain_cooling = self._smooth_update(self.gain_cooling, gain)

    def predict(
        self,
        setpoint: Optional[float],
        room_temp: Optional[float],
        heating_cap: float,
        cooling_cap: float,
        tolerance: float,
    ) -> Tuple[Optional[str], float]:
        if setpoint is None or room_temp is None:
            return None, 0.0
        delta = setpoint - room_temp
        with self._lock:
            if delta > tolerance and self.gain_heating > 0.0:
                pwm = min(heating_cap, max(0.0, self.gain_heating * delta))
                return "HEATING", pwm
            if delta < -tolerance and self.gain_cooling > 0.0:
                pwm = min(cooling_cap, max(0.0, self.gain_cooling * (-delta)))
                return "COOLING", pwm
        return None, 0.0

    def configure(self, **kwargs: float) -> Dict[str, float]:
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key):
                    continue
                numeric = float(value)
                if key == "alpha":
                    numeric = max(0.0, min(1.0, numeric))
                elif key in ("min_delta", "min_pwm", "steady_error", "steady_rate", "max_gain"):
                    numeric = max(0.0, numeric)
                setattr(self, key, numeric)
        return self.get_params()

    def set_gains(
        self,
        gain_heating: Optional[float] = None,
        gain_cooling: Optional[float] = None,
    ) -> None:
        with self._lock:
            if gain_heating is not None:
                self.gain_heating = max(0.0, float(gain_heating))
            if gain_cooling is not None:
                self.gain_cooling = max(0.0, float(gain_cooling))

    def get_params(self) -> Dict[str, float]:
        with self._lock:
            return {
                "alpha": self.alpha,
                "min_delta": self.min_delta,
                "min_pwm": self.min_pwm,
                "steady_error": self.steady_error,
                "steady_rate": self.steady_rate,
                "max_gain": self.max_gain,
                "gain_heating": self.gain_heating,
                "gain_cooling": self.gain_cooling,
            }


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
        kp: float = 0.045,
        ki: float = 0.0045,
        kp_heating: Optional[float] = 0.038,
        ki_heating: Optional[float] = 0.0035,
        kp_cooling: Optional[float] = 0.055,
        ki_cooling: Optional[float] = 0.0055,
        deadband: float = 0.15,
    deadband_heating: Optional[float] = None,
    deadband_cooling: Optional[float] = None,
        control_interval: float = 1.0,
        mode_switch_delay: float = 18.0,
        heating_pwm_cap: Optional[float] = 0.2,
        pwm_max: float = 0.4,
        feedforward_enabled: bool = True,
        feedforward_tolerance: float = 0.25,
        feedforward_alpha: float = 0.1,
        feedforward_min_delta: float = 0.3,
        feedforward_min_pwm: float = 0.01,
        feedforward_steady_error: float = 0.25,
        feedforward_steady_rate: float = 0.02,
        feedforward_max_gain: float = 6.0,
        feedforward_gain_heating: float = 0.44,
        feedforward_gain_cooling: float = 0.4,
        lead_time_heating: float = 3.5,
        lead_time_cooling: float = 5.0,
        ma_window: int = 30,
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
            deadband_heating: Override for HEATING-side deadband (temperature below setpoint)
            deadband_cooling: Override for COOLING-side deadband (temperature above setpoint)
            control_interval: Control loop interval in seconds
        """
        self.arduino_port = arduino_port
        self.telemetry_url = telemetry_url
        self.log_dir = log_dir
        self.control_interval = control_interval
        
        if deadband_heating is None:
            deadband_heating = deadband * 0.4
        if deadband_cooling is None:
            deadband_cooling = deadband * 0.1

        # Initialize components
        self.pi_controller = PIController(
            setpoint=setpoint,
            kp=kp,
            ki=ki,
            kp_heating=kp_heating,
            ki_heating=ki_heating,
            kp_cooling=kp_cooling,
            ki_cooling=ki_cooling,
            deadband=deadband,
            deadband_heating=deadband_heating,
            deadband_cooling=deadband_cooling,
            pwm_min=0.0,
            pwm_max=pwm_max,
            mode_switch_delay=mode_switch_delay,
            heating_pwm_cap=heating_pwm_cap,
            near_setpoint_threshold=1.0,  # Default 1°C threshold for two-tier control
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
        self.ma_window: int = max(1, int(ma_window))
        self._ma_buffer: deque = deque(maxlen=self.ma_window)

        # Feed-forward and predictive settings
        self.feedforward = HeatLossEstimator(
            alpha=feedforward_alpha,
            min_delta=feedforward_min_delta,
            min_pwm=feedforward_min_pwm,
            steady_error=feedforward_steady_error,
            steady_rate=feedforward_steady_rate,
            max_gain=feedforward_max_gain,
            gain_heating=feedforward_gain_heating,
            gain_cooling=feedforward_gain_cooling,
        )
        self.feedforward_enabled = bool(feedforward_enabled)
        self.feedforward_tolerance = max(0.0, float(feedforward_tolerance))
        self.lead_time_heating = max(0.0, float(lead_time_heating))
        self.lead_time_cooling = max(0.0, float(lead_time_cooling))
        self._last_temp_value: Optional[float] = None
        self._last_temp_time: Optional[float] = None

        # Protect shared configuration accessed by HTTP/UI threads
        self._config_lock = threading.Lock()

        # Manual override state (locked by default)
        self.manual_override_locked = True
        self.manual_override_enabled = False
        self.manual_direction = "OFF"
        self.manual_pwm = 0.0
        self.manual_direction_requested = "OFF"

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

    # Manual override controls
    def get_manual_status(self) -> Dict[str, Any]:
        with self._config_lock:
            locked = self.manual_override_locked
            enabled = self.manual_override_enabled
            direction = self.manual_direction
            pwm = self.manual_pwm
        control_source = "MANUAL" if enabled else ("PID" if self.pid_enabled else "IDLE")
        return {
            "locked": locked,
            "enabled": enabled,
            "direction": direction,
            "pwm": pwm,
            "control_source": control_source,
            "requested_direction": self.manual_direction_requested,
        }

    def unlock_manual_override(self) -> Dict[str, Any]:
        with self._config_lock:
            self.manual_override_locked = False
        logger.info("Manual override unlocked")
        return self.get_manual_status()

    def lock_manual_override(self) -> Dict[str, Any]:
        reset_output = False
        with self._config_lock:
            self.manual_override_locked = True
            if self.manual_override_enabled:
                reset_output = True
            self.manual_override_enabled = False
            self.manual_direction = "OFF"
            self.manual_pwm = 0.0
            self.manual_direction_requested = "OFF"

        if reset_output and self.arduino.serial and self.arduino.serial.is_open:
            try:
                self.arduino.set_pwm(0.0)
            except Exception as exc:
                logger.exception("Failed to reset PWM while locking manual override: %s", exc)

        logger.info("Manual override locked")
        return self.get_manual_status()

    def enable_manual_override(self) -> Dict[str, Any]:
        with self._config_lock:
            if self.manual_override_locked:
                raise ValueError("Manual override is locked")
            self.manual_override_enabled = True
            self.pid_enabled = False

        logger.info("Manual override ENABLED (PID suspended)")
        return self.get_manual_status()

    def disable_manual_override(self) -> Dict[str, Any]:
        with self._config_lock:
            was_enabled = self.manual_override_enabled
            self.manual_override_enabled = False

        if was_enabled and self.arduino.serial and self.arduino.serial.is_open:
            try:
                self.arduino.set_pwm(0.0)
            except Exception as exc:
                logger.exception("Failed to set PWM 0 while disabling manual override: %s", exc)

        logger.info("Manual override DISABLED")
        return self.get_manual_status()

    def set_manual_command(self, direction: str, pwm: float) -> Dict[str, Any]:
        if direction is None:
            raise ValueError("Direction is required for manual override")

        direction_token = direction.strip().upper()
        resolved_direction: str

        if direction_token in ("HIGH", "LOW", "OFF"):
            resolved_direction = direction_token
        elif direction_token in ("HEATING", "COOLING"):
            invert = self.arduino.get_invert_direction() if self.arduino else False
            if direction_token == "HEATING":
                resolved_direction = "LOW" if invert else "HIGH"
            else:
                resolved_direction = "HIGH" if invert else "LOW"
        else:
            raise ValueError(f"Invalid manual direction '{direction}'")

        pwm_value = max(0.0, min(float(pwm), self.pi_controller.get_pwm_max()))
        if resolved_direction == "OFF":
            pwm_value = 0.0

        with self._config_lock:
            if self.manual_override_locked:
                raise ValueError("Manual override is locked")
            self.manual_direction = resolved_direction
            self.manual_pwm = pwm_value
            self.manual_direction_requested = direction_token

        logger.info("Manual command updated (direction=%s, pwm=%.3f)", resolved_direction, pwm_value)
        return self.get_manual_status()

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
                
                # 4. Initialize control state
                pwm_total = 0.0
                mode = "OFF"
                pwm_feedback = 0.0
                pwm_ff = 0.0
                baseline_used = 0.0
                ff_mode: Optional[str] = None
                control_source = "PID"
                manual_direction_active: Optional[str] = None
                manual_pwm_target = 0.0
                manual_override_active = False
                manual_pwm_log: Optional[float] = None
                lead_applied = 0.0
                manual_requested_direction = "OFF"

                # 5. Snapshot configurable parameters for this iteration
                with self._config_lock:
                    ff_enabled = self.feedforward_enabled
                    ff_tolerance = self.feedforward_tolerance
                    lead_heat = self.lead_time_heating
                    lead_cool = self.lead_time_cooling
                    manual_override_active = self.manual_override_enabled
                    manual_direction_active = self.manual_direction
                    manual_pwm_target = self.manual_pwm
                    manual_requested_direction = self.manual_direction_requested

                # 6. Estimate temperature rate of change
                temp_rate: Optional[float] = None
                if obj_temp is not None:
                    if self._last_temp_time is not None and self._last_temp_value is not None:
                        dt_temp = max(0.0, loop_start - self._last_temp_time)
                        if dt_temp > 0:
                            temp_rate = (obj_temp - self._last_temp_value) / dt_temp
                    self._last_temp_value = obj_temp
                    self._last_temp_time = loop_start

                # 7. Manual override takes precedence
                if manual_override_active:
                    control_source = "MANUAL"
                    direction_to_use = (manual_direction_active or "OFF").upper()
                    pwm_target = max(0.0, min(manual_pwm_target, self.pi_controller.get_pwm_max()))
                    if direction_to_use == "OFF":
                        pwm_target = 0.0

                    if self.arduino.serial and self.arduino.serial.is_open:
                        if not self.arduino.apply_manual_output(direction_to_use, pwm_target):
                            logger.warning("Failed to apply manual override output")
                    else:
                        logger.debug("Arduino not connected, manual command buffered")

                    pwm_total = pwm_target
                    manual_pwm_log = pwm_target
                    manual_direction_active = direction_to_use
                    mode = "MANUAL"
                else:
                    # 7. Predict steady-state feed-forward contribution
                    if ff_enabled and room_temp is not None:
                        ff_mode, pwm_ff = self.feedforward.predict(
                            setpoint=setpoint,
                            room_temp=room_temp,
                            heating_cap=self.pi_controller.get_heating_pwm_cap(),
                            cooling_cap=self.pi_controller.get_pwm_max(),
                            tolerance=ff_tolerance,
                        )

                    # 8. Apply predictive lead compensation for measurement lag
                    effective_temp = obj_temp
                    if obj_temp is not None and temp_rate is not None:
                        lead_seconds = lead_heat if setpoint >= obj_temp else lead_cool
                        if lead_seconds > 0.0:
                            effective_temp = obj_temp + temp_rate * lead_seconds
                            lead_applied = lead_seconds

                    # 9. Run PI controller with feed-forward baseline
                    if self.pid_enabled and effective_temp is not None:
                        pwm_total, mode = self.pi_controller.compute(
                            current_temp=effective_temp,
                            baseline_pwm=pwm_ff,
                            baseline_mode=ff_mode,
                        )

                        if mode in ("HEATING", "COOLING"):
                            pwm_limit_active = (
                                self.pi_controller.get_heating_pwm_cap()
                                if mode == "HEATING"
                                else self.pi_controller.get_pwm_max()
                            )
                            if ff_mode == mode:
                                baseline_used = min(pwm_ff, pwm_limit_active)

                        if self.arduino.serial and self.arduino.serial.is_open:
                            if not self.arduino.set_mode(mode, pwm_total):
                                logger.warning("Failed to set Arduino mode/PWM")
                        else:
                            logger.debug("Arduino not connected, skipping control output")
                    elif not self.pid_enabled:
                        # PID disabled - hold outputs off
                        mode = "STANDBY"
                        if self.arduino.serial and self.arduino.serial.is_open:
                            self.arduino.set_mode("OFF", 0.0)
                        logger.debug("PID control disabled - PWM held at 0.0")
                        control_source = "IDLE"
                    else:
                        logger.warning("No objective temperature available - displaying ambient data only")
                        if ff_enabled and ff_mode in ("HEATING", "COOLING"):
                            if self.arduino.serial and self.arduino.serial.is_open:
                                if self.arduino.set_mode(ff_mode, pwm_ff):
                                    pwm_total = pwm_ff
                                    baseline_used = pwm_ff
                                    mode = ff_mode
                                    control_source = "FEEDFORWARD"
                                else:
                                    logger.warning("Failed to apply feed-forward only command")

                    manual_direction_active = None
                    manual_pwm_log = None

                if ff_mode != mode:
                    baseline_used = 0.0
                pwm_feedback = max(0.0, pwm_total - baseline_used)
                error_value = setpoint - obj_temp if obj_temp is not None else None

                if ff_enabled and not manual_override_active:
                    self.feedforward.update(
                        mode=mode if mode in ("HEATING", "COOLING") else None,
                        pwm=pwm_total,
                        setpoint=setpoint,
                        room_temp=room_temp,
                        error=error_value,
                        temp_rate=temp_rate,
                    )
                
                # 7. Log data (even if obj_temp is None)
                self.logger.log_data(
                    objective_temp=obj_temp,
                    setpoint=setpoint,
                    pwm=pwm_total,
                    mode=mode,
                    room_temp=room_temp,
                    humidity=humidity,
                    error=error_value,
                    pwm_feedforward=baseline_used,
                    pwm_feedforward_target=pwm_ff,
                    pwm_feedback=pwm_feedback,
                    feedforward_mode=ff_mode if baseline_used > 0 else None,
                    temp_rate=temp_rate,
                    lead_seconds=lead_applied if lead_applied > 0 else None,
                    control_source=control_source,
                    manual_override=manual_override_active,
                    manual_direction=manual_direction_active,
                    manual_pwm=manual_pwm_log if manual_pwm_log is not None else 0.0,
                    manual_requested_direction=manual_requested_direction,
                )
                
                # 8. Update data buffer for GUI (ALWAYS update so GUI shows something)
                data_point = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "objective_temp": obj_temp,
                    "setpoint": setpoint,
                    "pwm": pwm_total,
                    "mode": mode,
                    "room_temp": room_temp,
                    "humidity": humidity,
                    "pwm_feedforward": baseline_used,
                    "pwm_feedforward_target": pwm_ff,
                    "pwm_feedback": pwm_feedback,
                    "feedforward_mode": ff_mode if baseline_used > 0 else None,
                    "temp_rate": temp_rate,
                    "error": error_value,
                    "lead_seconds": lead_applied,
                    "control_source": control_source,
                    "manual_override": manual_override_active,
                    "manual_direction": manual_direction_active,
                    "manual_pwm": manual_pwm_log if manual_override_active else None,
                    "manual_requested_direction": manual_requested_direction,
                }
                
                with self.buffer_lock:
                    self.data_buffer.append(data_point)
                
                # 9. Log status
                logger.info(
                    "T_obj=%s, Setpoint=%.2f°C, PWM=%.3f, FF=%.3f, Mode=%s, Source=%s, ManualDir=%s, ManualReq=%s, T_room=%s, RH=%s",
                    f"{obj_temp:.2f}°C" if obj_temp is not None else "N/A",
                    setpoint,
                    pwm_total,
                    baseline_used,
                    mode,
                    control_source,
                    manual_direction_active if manual_override_active else "--",
                    manual_requested_direction if manual_override_active else "--",
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
                    "pwm_feedforward": 0.0,
                    "pwm_feedforward_target": 0.0,
                    "pwm_feedback": 0.0,
                    "feedforward_mode": None,
                    "temp_rate": None,
                    "error": None,
                    "lead_seconds": 0.0,
                    "control_source": "ERROR",
                    "manual_override": False,
                    "manual_direction": None,
                    "manual_pwm": None,
                    "manual_requested_direction": self.manual_direction_requested,
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
    
    def set_feedforward_enabled(self, enabled: bool) -> None:
        with self._config_lock:
            self.feedforward_enabled = bool(enabled)
        logger.info("Feed-forward %s", "ENABLED" if enabled else "DISABLED")

    def set_feedforward_tolerance(self, tolerance: float) -> None:
        tol = max(0.0, float(tolerance))
        with self._config_lock:
            self.feedforward_tolerance = tol
        logger.info("Feed-forward tolerance set to %.3f°C", tol)

    def set_lead_times(
        self,
        heating: Optional[float] = None,
        cooling: Optional[float] = None,
    ) -> None:
        with self._config_lock:
            if heating is not None:
                self.lead_time_heating = max(0.0, float(heating))
            if cooling is not None:
                self.lead_time_cooling = max(0.0, float(cooling))
        logger.info(
            "Predictive lead times set to heating=%.2fs cooling=%.2fs",
            self.lead_time_heating,
            self.lead_time_cooling,
        )

    def configure_feedforward(self, **kwargs: float) -> Dict[str, float]:
        params = self.feedforward.configure(**kwargs)
        logger.info("Feed-forward estimator parameters updated")
        return params

    def set_feedforward_gains(
        self,
        gain_heating: Optional[float] = None,
        gain_cooling: Optional[float] = None,
    ) -> None:
        self.feedforward.set_gains(gain_heating=gain_heating, gain_cooling=gain_cooling)
        params = self.feedforward.get_params()
        logger.info(
            "Feed-forward gains set to heating=%.4f cooling=%.4f",
            params["gain_heating"],
            params["gain_cooling"],
        )

    def reset_feedforward(self) -> None:
        self.feedforward.reset()
        logger.info("Feed-forward estimator reset")

    def enable_pid(self) -> None:
        """Enable PID control."""
        with self._config_lock:
            manual_active = self.manual_override_enabled
        if manual_active:
            self.disable_manual_override()
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
        with self._config_lock:
            ff_enabled = self.feedforward_enabled
            ff_tolerance = self.feedforward_tolerance
            lead_heat = self.lead_time_heating
            lead_cool = self.lead_time_cooling
            manual_locked = self.manual_override_locked
            manual_enabled = self.manual_override_enabled
            manual_direction = self.manual_direction
            manual_pwm = self.manual_pwm
            manual_requested = self.manual_direction_requested
        return {
            "setpoint": self.pi_controller.get_setpoint(),
            "kp": self.pi_controller.get_kp(),
            "ki": self.pi_controller.get_ki(),
            "kp_heating": self.pi_controller.get_kp_heating(),
            "kp_cooling": self.pi_controller.get_kp_cooling(),
            "ki_heating": self.pi_controller.get_ki_heating(),
            "ki_cooling": self.pi_controller.get_ki_cooling(),
            "deadband": self.pi_controller.get_deadband(),
            "deadband_heating": self.pi_controller.get_deadband_heating(),
            "deadband_cooling": self.pi_controller.get_deadband_cooling(),
            "mode_switch_delay": self.pi_controller.get_mode_switch_delay(),
            "heating_pwm_cap": self.pi_controller.get_heating_pwm_cap(),
            "pwm_max": self.pi_controller.get_pwm_max(),
            "near_setpoint_threshold": self.pi_controller.get_near_setpoint_threshold(),
            "control_interval": self.control_interval,
            "pid_enabled": self.pid_enabled,
            "ma_window": self.get_ma_window(),
            "invert_direction": self.arduino.get_invert_direction() if self.arduino else False,
            "feedforward_enabled": ff_enabled,
            "feedforward_tolerance": ff_tolerance,
            "feedforward": self.feedforward.get_params(),
            "lead_time_heating": lead_heat,
            "lead_time_cooling": lead_cool,
            "manual_override_locked": manual_locked,
            "manual_override_enabled": manual_enabled,
            "manual_direction": manual_direction,
            "manual_pwm": manual_pwm,
            "manual_requested_direction": manual_requested,
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
                    "pwm_feedforward": 0.0,
                    "pwm_feedforward_target": 0.0,
                    "pwm_feedback": 0.0,
                    "feedforward_mode": None,
                    "temp_rate": None,
                    "error": None,
                    "lead_seconds": 0.0,
                    "control_source": "IDLE",
                    "manual_override": False,
                    "manual_direction": None,
                    "manual_pwm": None,
                    "manual_requested_direction": "OFF",
                }
            
            # Add PID enabled status
            latest["pid_enabled"] = self.pid_enabled
            manual_status = self.get_manual_status()
            latest["control_source"] = latest.get("control_source", manual_status["control_source"])
            latest["manual_override"] = latest.get("manual_override", manual_status["enabled"])
            latest["manual_direction"] = latest.get("manual_direction", manual_status["direction"])
            latest["manual_pwm"] = (
                latest.get("manual_pwm")
                if latest.get("manual_override")
                else (manual_status["pwm"] if manual_status["enabled"] else None)
            )
            latest["manual_override_locked"] = manual_status["locked"]
            latest["manual_override_enabled"] = manual_status["enabled"]
            latest["manual_requested_direction"] = manual_status.get("requested_direction", latest.get("manual_requested_direction", "OFF"))
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
                                "pwm_feedforward": float(row["pwm_feedforward"]) if row.get("pwm_feedforward") else 0.0,
                                "pwm_feedforward_target": float(row["pwm_feedforward_target"]) if row.get("pwm_feedforward_target") else 0.0,
                                "pwm_feedback": float(row["pwm_feedback"]) if row.get("pwm_feedback") else 0.0,
                                "feedforward_mode": row.get("feedforward_mode") or None,
                                "temp_rate": float(row["temp_rate"]) if row.get("temp_rate") else None,
                                "error": float(row["error"]) if row.get("error") else None,
                                "lead_seconds": float(row["lead_seconds"]) if row.get("lead_seconds") else None,
                                "control_source": row.get("control_source") or None,
                                "manual_override": (row.get("manual_override") == "1"),
                                "manual_direction": row.get("manual_direction") or None,
                                "manual_pwm": float(row["manual_pwm"]) if row.get("manual_pwm") else None,
                                "manual_requested_direction": row.get("manual_requested_direction") or None,
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
        elif path == "/api/manual/status":
            self._serve_manual_status()
        elif path == "/api/manual/unlock":
            self._serve_manual_unlock()
        elif path == "/api/manual/lock":
            self._serve_manual_lock()
        elif path == "/api/manual/enable":
            self._serve_manual_enable()
        elif path == "/api/manual/disable":
            self._serve_manual_disable()
        elif path == "/api/manual/set":
            self._serve_manual_set(parsed.query)
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
            if "kp_heating" in params:
                new_kph = float(params["kp_heating"][0])
                self.controller.pi_controller.set_kp_heating(new_kph)
                updated["kp_heating"] = new_kph
            if "kp_cooling" in params:
                new_kpc = float(params["kp_cooling"][0])
                self.controller.pi_controller.set_kp_cooling(new_kpc)
                updated["kp_cooling"] = new_kpc
            
            if "ki" in params:
                new_ki = float(params["ki"][0])
                self.controller.pi_controller.set_ki(new_ki)
                updated["ki"] = new_ki
            if "ki_heating" in params:
                new_kih = float(params["ki_heating"][0])
                self.controller.pi_controller.set_ki_heating(new_kih)
                updated["ki_heating"] = new_kih
            if "ki_cooling" in params:
                new_kic = float(params["ki_cooling"][0])
                self.controller.pi_controller.set_ki_cooling(new_kic)
                updated["ki_cooling"] = new_kic
            
            if "deadband" in params:
                new_deadband = float(params["deadband"][0])
                self.controller.pi_controller.set_deadband(new_deadband)
                updated["deadband"] = new_deadband

            if "deadband_heating" in params:
                new_db_heat = float(params["deadband_heating"][0])
                self.controller.pi_controller.set_deadband_heating(new_db_heat)
                updated["deadband_heating"] = new_db_heat

            if "deadband_cooling" in params:
                new_db_cool = float(params["deadband_cooling"][0])
                self.controller.pi_controller.set_deadband_cooling(new_db_cool)
                updated["deadband_cooling"] = new_db_cool
            
            if "mode_switch_delay" in params:
                new_delay = float(params["mode_switch_delay"][0])
                self.controller.pi_controller.set_mode_switch_delay(new_delay)
                updated["mode_switch_delay"] = new_delay

            if "heating_pwm_cap" in params:
                new_cap = float(params["heating_pwm_cap"][0])
                self.controller.pi_controller.set_heating_pwm_cap(new_cap)
                updated["heating_pwm_cap"] = new_cap

            if "near_setpoint_threshold" in params:
                new_threshold = float(params["near_setpoint_threshold"][0])
                self.controller.pi_controller.set_near_setpoint_threshold(new_threshold)
                updated["near_setpoint_threshold"] = new_threshold

            if "control_interval" in params:
                new_interval = float(params["control_interval"][0])
                self.controller.set_control_interval(new_interval)
                updated["control_interval"] = new_interval

            if "ma_window" in params:
                new_ma = int(params["ma_window"][0])
                self.controller.set_ma_window(new_ma)
                updated["ma_window"] = new_ma

            if "feedforward_enabled" in params:
                ff_flag = params["feedforward_enabled"][0].strip().lower()
                ff_enabled = ff_flag in ("1", "true", "yes", "on")
                self.controller.set_feedforward_enabled(ff_enabled)
                updated["feedforward_enabled"] = ff_enabled

            ff_config_updates: Dict[str, float] = {}
            for key in (
                "feedforward_alpha",
                "feedforward_min_delta",
                "feedforward_min_pwm",
                "feedforward_steady_error",
                "feedforward_steady_rate",
                "feedforward_max_gain",
            ):
                if key in params:
                    value = float(params[key][0])
                    ff_config_updates[key.replace("feedforward_", "")] = value
                    updated[key] = value
            if ff_config_updates:
                self.controller.configure_feedforward(**ff_config_updates)

            if "feedforward_tolerance" in params:
                tol = float(params["feedforward_tolerance"][0])
                self.controller.set_feedforward_tolerance(tol)
                updated["feedforward_tolerance"] = tol

            gain_heating = gain_cooling = None
            if "feedforward_gain_heating" in params:
                gain_heating = float(params["feedforward_gain_heating"][0])
                updated["feedforward_gain_heating"] = gain_heating
            if "feedforward_gain_cooling" in params:
                gain_cooling = float(params["feedforward_gain_cooling"][0])
                updated["feedforward_gain_cooling"] = gain_cooling
            if gain_heating is not None or gain_cooling is not None:
                self.controller.set_feedforward_gains(gain_heating=gain_heating, gain_cooling=gain_cooling)

            if "feedforward_reset" in params:
                reset_flag = params["feedforward_reset"][0].strip().lower()
                if reset_flag in ("1", "true", "yes", "on"):
                    self.controller.reset_feedforward()
                    updated["feedforward_reset"] = True

            lead_heating = lead_cooling = None
            if "lead_time_heating" in params:
                lead_heating = float(params["lead_time_heating"][0])
                updated["lead_time_heating"] = lead_heating
            if "lead_time_cooling" in params:
                lead_cooling = float(params["lead_time_cooling"][0])
                updated["lead_time_cooling"] = lead_cooling
            if lead_heating is not None or lead_cooling is not None:
                self.controller.set_lead_times(heating=lead_heating, cooling=lead_cooling)

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

    def _serve_manual_status(self) -> None:
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        status = self.controller.get_manual_status()
        self._send_json(status)

    def _serve_manual_unlock(self) -> None:
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        status = self.controller.unlock_manual_override()
        status["message"] = "Manual override unlocked"
        self._send_json(status)

    def _serve_manual_lock(self) -> None:
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        status = self.controller.lock_manual_override()
        status["message"] = "Manual override locked"
        self._send_json(status)

    def _serve_manual_enable(self) -> None:
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        try:
            status = self.controller.enable_manual_override()
        except ValueError as exc:
            self.send_error(400, str(exc))
            return

        status["message"] = "Manual override enabled"
        self._send_json(status)

    def _serve_manual_disable(self) -> None:
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        status = self.controller.disable_manual_override()
        status["message"] = "Manual override disabled"
        self._send_json(status)

    def _serve_manual_set(self, query: str) -> None:
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        params = parse_qs(query)
        direction_param: Optional[str] = None
        for key in ("direction", "mode"):
            if key in params and params[key]:
                direction_param = params[key][0]
                break

        if direction_param is None:
            self.send_error(400, "Missing 'direction' parameter")
            return

        try:
            if "pwm" in params and params["pwm"]:
                pwm_value = float(params["pwm"][0])
            else:
                pwm_value = float(self.controller.get_manual_status().get("pwm", 0.0) or 0.0)
        except ValueError:
            self.send_error(400, "Invalid pwm value")
            return

        try:
            status = self.controller.set_manual_command(direction_param, pwm_value)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return

        status["message"] = "Manual command updated"
        self._send_json(status)
    
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

        .control-group select {
            padding: 10px 15px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 1em;
            width: 160px;
            transition: border-color 0.3s;
        }

        .control-group select:focus {
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

        .control-section {
            margin-top: 25px;
            padding-top: 20px;
            border-top: 1px solid #e9ecef;
        }

        .control-heading {
            margin-bottom: 10px;
            font-size: 1.1em;
            font-weight: 600;
            color: #444;
        }

        .control-hint {
            font-size: 0.85em;
            color: #777;
        }

        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 10px;
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
                <h3>Control Source</h3>
                <div class="value" id="control-source">PID</div>
            </div>
            <div class="status-item">
                <h3>Manual Direction</h3>
                <div class="value" id="manual-direction">--</div>
            </div>
            <div class="status-item">
                <h3>Manual PWM</h3>
                <div class="value" id="manual-pwm">--</div>
            </div>
            <div class="status-item">
                <h3>PWM Output</h3>
                <div class="value" id="pwm">--</div>
            </div>
            <div class="status-item">
                <h3>Feed-forward PWM</h3>
                <div class="value" id="pwm-feedforward">--</div>
            </div>
            <div class="status-item">
                <h3>PI Residual PWM</h3>
                <div class="value" id="pwm-feedback">--</div>
            </div>
            <div class="status-item">
                <h3>Feed-forward Mode</h3>
                <div class="value" id="ff-mode">--</div>
            </div>
            <div class="status-item">
                <h3>Temperature Rate</h3>
                <div class="value" id="temp-rate">--</div>
            </div>
            <div class="status-item">
                <h3>Lead Compensation</h3>
                <div class="value" id="lead-seconds">--</div>
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
                <input type="number" id="setpoint-input" step="0.1" min="18" max="35" value="25.0">
                <span class="unit">°C</span>
                <button onclick="updateSetpoint()">Update Setpoint</button>
            </div>
            
            <div class="control-group" style="margin-top: 15px;">
                <label for="kp-input">Kp (Proportional):</label>
                <input type="number" id="kp-input" step="0.001" min="0" max="1" value="0.045">
                
                <label for="ki-input" style="margin-left: 20px;">Ki (Integral):</label>
                <input type="number" id="ki-input" step="any" min="0" max="1" value="0.0045">
                
                <label for="interval-input" style="margin-left: 20px;">Interval (s):</label>
                <input type="number" id="interval-input" step="0.1" min="0.1" max="10" value="1.0">
                
                <label for="deadband-input" style="margin-left: 20px;">Deadband (°C):</label>
                <input type="number" id="deadband-input" step="0.05" min="0" max="2" value="0.15">

                <label for="mode-delay-input" style="margin-left: 20px;">Mode Delay (s):</label>
                <input type="number" id="mode-delay-input" step="1" min="0" max="120" value="18">

                <label for="ma-window-input" style="margin-left: 20px;">MA Window (samples):</label>
                <input type="number" id="ma-window-input" step="1" min="1" max="600" value="30">

                <label for="heating-cap-input" style="margin-left: 20px;">Heating PWM Cap:</label>
                <input type="number" id="heating-cap-input" step="0.01" min="0" max="0.4" value="0.20">

                <label for="invert-dir-input" style="margin-left: 20px;">Invert Direction:</label>
                <input type="checkbox" id="invert-dir-input">
            </div>

            <div class="control-section">
                <h3 class="control-heading">Per-Mode PI Gains</h3>
                <div class="control-group">
                    <label for="kp-heating-input">Kp Heating:</label>
                    <input type="number" id="kp-heating-input" step="0.001" min="0" max="1" value="0.038">

                    <label for="kp-cooling-input" style="margin-left: 20px;">Kp Cooling:</label>
                    <input type="number" id="kp-cooling-input" step="0.001" min="0" max="1" value="0.055">

                    <label for="ki-heating-input" style="margin-left: 20px;">Ki Heating:</label>
                    <input type="number" id="ki-heating-input" step="any" min="0" max="1" value="0.0035">

                    <label for="ki-cooling-input" style="margin-left: 20px;">Ki Cooling:</label>
                    <input type="number" id="ki-cooling-input" step="any" min="0" max="1" value="0.0055">
                </div>
            </div>

            <div class="control-section">
                <h3 class="control-heading">Feed-forward Compensation</h3>
                <div class="control-group checkbox-group">
                    <label for="feedforward-enabled-input">Enable Feed-forward:</label>
                    <input type="checkbox" id="feedforward-enabled-input" checked>
                    <span class="control-hint">Learns steady-state heat loss and pre-loads PWM.</span>
                </div>
                <div class="control-group" style="margin-top: 10px;">
                    <label for="feedforward-tolerance-input">Tolerance (°C):</label>
                    <input type="number" id="feedforward-tolerance-input" step="0.01" min="0" max="5" value="0.25">

                    <label for="feedforward-alpha-input" style="margin-left: 20px;">Learning α:</label>
                    <input type="number" id="feedforward-alpha-input" step="0.01" min="0" max="1" value="0.10">

                    <label for="feedforward-min-delta-input" style="margin-left: 20px;">Min ΔT (°C):</label>
                    <input type="number" id="feedforward-min-delta-input" step="0.05" min="0" max="10" value="0.30">

                    <label for="feedforward-min-pwm-input" style="margin-left: 20px;">Min PWM:</label>
                    <input type="number" id="feedforward-min-pwm-input" step="0.001" min="0" max="1" value="0.010">
                </div>
                <div class="control-group" style="margin-top: 10px;">
                    <label for="feedforward-steady-error-input">Steady Error (°C):</label>
                    <input type="number" id="feedforward-steady-error-input" step="0.01" min="0" max="2" value="0.25">

                    <label for="feedforward-steady-rate-input" style="margin-left: 20px;">Steady Rate (°C/s):</label>
                    <input type="number" id="feedforward-steady-rate-input" step="0.001" min="0" max="1" value="0.020">

                    <label for="feedforward-max-gain-input" style="margin-left: 20px;">Max Gain:</label>
                    <input type="number" id="feedforward-max-gain-input" step="0.1" min="0" max="50" value="6.0">

                    <label for="feedforward-gain-heating-input" style="margin-left: 20px;">Manual Gain Heating:</label>
                    <input type="number" id="feedforward-gain-heating-input" step="0.001" min="0" max="50" value="0.44">

                    <label for="feedforward-gain-cooling-input" style="margin-left: 20px;">Manual Gain Cooling:</label>
                    <input type="number" id="feedforward-gain-cooling-input" step="0.001" min="0" max="50" value="0.40">
                </div>

                <div class="control-group" style="margin-top: 10px;">
                    <label for="lead-time-heating-input">Lead Time Heating (s):</label>
                    <input type="number" id="lead-time-heating-input" step="0.1" min="0" max="60" value="3.5">

                    <label for="lead-time-cooling-input" style="margin-left: 20px;">Lead Time Cooling (s):</label>
                    <input type="number" id="lead-time-cooling-input" step="0.1" min="0" max="60" value="5.0">
                    <span class="control-hint">Predicts temperature by extrapolating current rate.</span>
                </div>
            </div>

            <div class="control-section" id="manual-override-section">
                <h3 class="control-heading">Manual Override (Locked)</h3>
                <div class="control-hint" style="margin-bottom: 10px;">Unlock to bypass PID and drive PWM/direction directly. Use with caution.</div>
                <div class="control-group">
                    <button id="manual-lock-btn" onclick="toggleManualLock()">🔒 Unlock Manual Override</button>
                    <span id="manual-lock-status" style="font-weight: 600; color: #c0392b;">LOCKED</span>
                    <button id="manual-enable-btn" onclick="toggleManualOverride()" disabled>Enable Manual Override</button>
                </div>
                <div class="control-group" style="margin-top: 10px;">
                    <label for="manual-mode-select">Manual Direction:</label>
                    <select id="manual-mode-select" disabled>
                        <option value="OFF">OFF</option>
                        <option value="HEATING">HEATING (logical)</option>
                        <option value="COOLING">COOLING (logical)</option>
                        <option value="HIGH">RAW HIGH</option>
                        <option value="LOW">RAW LOW</option>
                    </select>

                    <label for="manual-pwm-input" style="margin-left: 20px;">Manual PWM:</label>
                    <input type="number" id="manual-pwm-input" step="0.001" min="0" max="0.4" value="0.000" disabled>

                    <button id="manual-apply-btn" onclick="applyManualCommand()" disabled>Apply Manual Command</button>
                </div>
                <div class="control-hint" style="margin-top: 10px;">Logical directions respect the invert-direction flag; RAW options drive the H-bridge pin directly.</div>
            </div>

            <div class="control-group" style="margin-top: 20px; gap: 20px;">
                <button onclick="updateParams()">Update Parameters</button>
                <button onclick="resetFeedforward()" style="background: linear-gradient(135deg, #ff6b6b 0%, #ff4d4f 100%);">Reset Feed-forward Learning</button>
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
    let manualStatus = { locked: true, enabled: false, direction: "OFF", pwm: 0, requestedDirection: "OFF" };
        
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
                            label: 'PWM Total',
                            data: [],
                            borderColor: '#4ecdc4',
                            backgroundColor: 'rgba(78,205,196,0.1)',
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y',
                        },
                        {
                            label: 'Feed-forward PWM',
                            data: [],
                            borderColor: '#ffa94d',
                            backgroundColor: 'rgba(255,169,77,0.15)',
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y',
                        },
                        {
                            label: 'PI Residual PWM',
                            data: [],
                            borderColor: '#845ef7',
                            backgroundColor: 'rgba(132,94,247,0.15)',
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
        
        function describeManualDirection(direction) {
            if (!direction) {
                return '--';
            }
            const token = String(direction).toUpperCase();
            if (token === 'HIGH') return 'RAW HIGH';
            if (token === 'LOW') return 'RAW LOW';
            if (token === 'OFF') return 'OFF';
            return token;
        }

        function updateManualControls() {
            const lockBtn = document.getElementById('manual-lock-btn');
            const enableBtn = document.getElementById('manual-enable-btn');
            const modeSelect = document.getElementById('manual-mode-select');
            const pwmInput = document.getElementById('manual-pwm-input');
            const applyBtn = document.getElementById('manual-apply-btn');
            const lockStatus = document.getElementById('manual-lock-status');
            const heading = document.querySelector('#manual-override-section .control-heading');
            if (!lockBtn || !enableBtn || !modeSelect || !pwmInput || !applyBtn || !lockStatus) {
                return;
            }

            if (heading) {
                heading.textContent = manualStatus.locked ? 'Manual Override (Locked)' : 'Manual Override (Unlocked)';
            }

            if (manualStatus.locked) {
                lockBtn.textContent = '🔒 Unlock Manual Override';
                lockStatus.textContent = 'LOCKED';
                lockStatus.style.color = '#c0392b';
                enableBtn.disabled = true;
                modeSelect.disabled = true;
                pwmInput.disabled = true;
                applyBtn.disabled = true;
                enableBtn.style.background = 'linear-gradient(135deg, #bdc3c7 0%, #95a5a6 100%)';
            } else {
                lockBtn.textContent = '🔓 Lock Manual Override';
                lockStatus.textContent = manualStatus.enabled ? 'UNLOCKED • ACTIVE' : 'UNLOCKED';
                lockStatus.style.color = manualStatus.enabled ? '#28a745' : '#2c3e50';
                enableBtn.disabled = false;
                modeSelect.disabled = false;
                pwmInput.disabled = false;
                applyBtn.disabled = false;
                enableBtn.style.background = manualStatus.enabled
                    ? 'linear-gradient(135deg, #dc3545 0%, #c82333 100%)'
                    : 'linear-gradient(135deg, #f39c12 0%, #f1c40f 100%)';
            }

            enableBtn.textContent = manualStatus.enabled ? 'Disable Manual Override' : 'Enable Manual Override';
            enableBtn.style.color = '#fff';

            const options = [...modeSelect.options].map(opt => opt.value);
            let selectValue = manualStatus.requestedDirection ? manualStatus.requestedDirection.toUpperCase() : 'OFF';
            if (!options.includes(selectValue)) {
                const fallback = manualStatus.direction ? manualStatus.direction.toUpperCase() : 'OFF';
                selectValue = options.includes(fallback) ? fallback : 'OFF';
            }
            modeSelect.value = options.includes(selectValue) ? selectValue : 'OFF';

            const pwmValue = typeof manualStatus.pwm === 'number' ? manualStatus.pwm : 0;
            pwmInput.value = pwmValue.toFixed(3);
        }

        async function loadManualStatus() {
            try {
                const response = await fetch('/api/manual/status', { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const data = await response.json();
                manualStatus = {
                    locked: Boolean(data.locked),
                    enabled: Boolean(data.enabled),
                    direction: data.direction || 'OFF',
                    pwm: typeof data.pwm === 'number' ? data.pwm : 0,
                    requestedDirection: data.requested_direction || data.direction || 'OFF',
                };
                updateManualControls();
            } catch (error) {
                console.error('Error loading manual status:', error);
            }
        }

        async function toggleManualLock() {
            const endpoint = manualStatus.locked ? '/api/manual/unlock' : '/api/manual/lock';
            try {
                const response = await fetch(endpoint, { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const data = await response.json();
                manualStatus = {
                    locked: Boolean(data.locked),
                    enabled: Boolean(data.enabled),
                    direction: data.direction || manualStatus.direction,
                    pwm: typeof data.pwm === 'number' ? data.pwm : manualStatus.pwm,
                    requestedDirection: data.requested_direction || manualStatus.requestedDirection,
                };
                updateManualControls();
                if (data.message) {
                    console.log(data.message);
                }
            } catch (error) {
                console.error('Error toggling manual lock:', error);
                alert('Failed to toggle manual override lock');
            }
        }

        async function toggleManualOverride() {
            const endpoint = manualStatus.enabled ? '/api/manual/disable' : '/api/manual/enable';
            try {
                const response = await fetch(endpoint, { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const data = await response.json();
                manualStatus = {
                    locked: Boolean(data.locked),
                    enabled: Boolean(data.enabled),
                    direction: data.direction || manualStatus.direction,
                    pwm: typeof data.pwm === 'number' ? data.pwm : manualStatus.pwm,
                    requestedDirection: data.requested_direction || manualStatus.requestedDirection,
                };
                updateManualControls();
                if (data.message) {
                    alert(data.message);
                }
                await loadPIDStatus();
            } catch (error) {
                console.error('Error toggling manual override:', error);
                alert('Failed to toggle manual override');
            }
        }

        async function applyManualCommand() {
            const modeSelect = document.getElementById('manual-mode-select');
            const pwmInput = document.getElementById('manual-pwm-input');
            if (!modeSelect || !pwmInput) {
                return;
            }
            const direction = modeSelect.value;
            let pwmValue = parseFloat(pwmInput.value);
            if (Number.isNaN(pwmValue)) {
                alert('Invalid PWM value');
                return;
            }
            pwmValue = Math.min(Math.max(pwmValue, 0), 0.4);
            pwmInput.value = pwmValue.toFixed(3);

            const url = `/api/manual/set?direction=${encodeURIComponent(direction)}&pwm=${pwmValue}`;
            try {
                const response = await fetch(url, { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const data = await response.json();
                manualStatus = {
                    locked: Boolean(data.locked),
                    enabled: Boolean(data.enabled),
                    direction: data.direction || direction,
                    pwm: typeof data.pwm === 'number' ? data.pwm : pwmValue,
                    requestedDirection: data.requested_direction || direction,
                };
                updateManualControls();
                alert(data.message || 'Manual command applied');
            } catch (error) {
                console.error('Error applying manual command:', error);
                alert('Failed to apply manual command');
            }
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
                const modeClass = data.mode ? `mode-${data.mode.toLowerCase()}` : '';
                modeElement.textContent = data.mode || '--';
                modeElement.className = `value ${modeClass}`.trim();
                
                const totalPwm = typeof data.pwm === 'number' ? data.pwm : 0;
                document.getElementById('pwm').innerHTML = 
                    `${totalPwm.toFixed(3)}<span class="unit"></span>`;

                const ffApplied = typeof data.pwm_feedforward === 'number' ? data.pwm_feedforward : 0;
                const piResidual = typeof data.pwm_feedback === 'number' ? data.pwm_feedback : Math.max(0, totalPwm - ffApplied);
                document.getElementById('pwm-feedforward').innerHTML = `${ffApplied.toFixed(3)}<span class="unit"></span>`;
                document.getElementById('pwm-feedback').innerHTML = `${piResidual.toFixed(3)}<span class="unit"></span>`;

                const ffModeElement = document.getElementById('ff-mode');
                ffModeElement.textContent = data.feedforward_mode ? data.feedforward_mode : (ffApplied > 0 ? 'ACTIVE' : '--');

                const tempRateElement = document.getElementById('temp-rate');
                const rateVal = typeof data.temp_rate === 'number' ? data.temp_rate : null;
                tempRateElement.innerHTML = rateVal !== null ? `${rateVal.toFixed(3)}<span class="unit">°C/s</span>` : '--';

                const leadElement = document.getElementById('lead-seconds');
                const leadVal = typeof data.lead_seconds === 'number' ? data.lead_seconds : null;
                leadElement.innerHTML = leadVal !== null && leadVal > 0 ? `${leadVal.toFixed(2)}<span class="unit">s</span>` : '--';

                const controlSourceElement = document.getElementById('control-source');
                if (controlSourceElement) {
                    controlSourceElement.textContent = data.control_source || (data.pid_enabled ? 'PID' : 'IDLE');
                }

                const manualDirElement = document.getElementById('manual-direction');
                if (manualDirElement) {
                    if (data.manual_override) {
                        const requestedDir = (data.manual_requested_direction || data.manual_direction || '').toUpperCase();
                        const rawDir = (data.manual_direction || '').toUpperCase();
                        let label = describeManualDirection(requestedDir || rawDir || '');
                        const rawLabel = describeManualDirection(rawDir || '');
                        if (requestedDir && rawDir && requestedDir !== rawDir && rawDir !== '') {
                            label = `${label} (${rawLabel})`;
                        }
                        manualDirElement.textContent = label;
                    } else {
                        manualDirElement.textContent = '--';
                    }
                }

                const manualPwmElement = document.getElementById('manual-pwm');
                if (manualPwmElement) {
                    const manualPwmValue = typeof data.manual_pwm === 'number' ? data.manual_pwm : null;
                    if (data.manual_override && manualPwmValue !== null) {
                        manualPwmElement.innerHTML = `${manualPwmValue.toFixed(3)}<span class="unit"></span>`;
                    } else {
                        manualPwmElement.textContent = '--';
                    }
                }

                if (typeof data.manual_override_locked === 'boolean') {
                    manualStatus.locked = data.manual_override_locked;
                }
                if (typeof data.manual_override === 'boolean') {
                    manualStatus.enabled = data.manual_override;
                }
                if (typeof data.manual_direction === 'string') {
                    manualStatus.direction = data.manual_direction;
                }
                if (manualStatus.enabled && typeof data.manual_pwm === 'number') {
                    manualStatus.pwm = data.manual_pwm;
                } else if (!manualStatus.enabled) {
                    manualStatus.pwm = 0;
                }
                if (typeof data.manual_requested_direction === 'string') {
                    manualStatus.requestedDirection = data.manual_requested_direction;
                }
                updateManualControls();
                
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
            const totalPwm = typeof data.pwm === 'number' ? data.pwm : 0;
            const ffPwm = typeof data.pwm_feedforward === 'number' ? data.pwm_feedforward : 0;
            const residualPwm = typeof data.pwm_feedback === 'number' ? data.pwm_feedback : Math.max(0, totalPwm - ffPwm);

            controlChart.data.datasets[0].data.push({
                x: timestamp,
                y: totalPwm
            });

            controlChart.data.datasets[1].data.push({
                x: timestamp,
                y: ffPwm
            });

            controlChart.data.datasets[2].data.push({
                x: timestamp,
                y: residualPwm
            });
            
            // Mode as numeric: HEATING=1, OFF=0, COOLING=-1
            let modeValue = 0;
            if (data.mode === 'HEATING') modeValue = 1;
            else if (data.mode === 'COOLING') modeValue = -1;
            else if (data.mode === 'MANUAL') {
                if (data.manual_direction === 'HIGH') modeValue = 1;
                else if (data.manual_direction === 'LOW') modeValue = -1;
                else modeValue = 0;
            }
            
            controlChart.data.datasets[3].data.push({
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
            for (let i = 1; i <= 3; i += 1) {
                if (controlChart.data.datasets[i].data.length > MAX_DATA_POINTS) {
                    controlChart.data.datasets[i].data.shift();
                }
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
                    controlChart.data.datasets[2].data = [];
                    controlChart.data.datasets[3].data = [];
                    
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
                        
                        const totalHistoricPwm = typeof point.pwm === 'number' ? point.pwm : 0;
                        const historicFf = typeof point.pwm_feedforward === 'number' ? point.pwm_feedforward : 0;
                        const historicResidual = typeof point.pwm_feedback === 'number' ? point.pwm_feedback : Math.max(0, totalHistoricPwm - historicFf);

                        controlChart.data.datasets[0].data.push({
                            x: timestamp,
                            y: totalHistoricPwm
                        });

                        controlChart.data.datasets[1].data.push({
                            x: timestamp,
                            y: historicFf
                        });

                        controlChart.data.datasets[2].data.push({
                            x: timestamp,
                            y: historicResidual
                        });
                        
                        let modeValue = 0;
                        if (point.mode === 'HEATING') modeValue = 1;
                        else if (point.mode === 'COOLING') modeValue = -1;
                        else if (point.mode === 'MANUAL') {
                            if (point.manual_direction === 'HIGH') modeValue = 1;
                            else if (point.manual_direction === 'LOW') modeValue = -1;
                            else modeValue = 0;
                        }
                        
                        controlChart.data.datasets[3].data.push({
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
                const response = await fetch('/api/current', { cache: 'no-store' });
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
                const response = await fetch(`/api/setpoint?value=${value}`, { cache: 'no-store' });
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
                const kpHeating = parseFloat(document.getElementById('kp-heating-input')?.value || '');
                const kpCooling = parseFloat(document.getElementById('kp-cooling-input')?.value || '');
                const kiHeating = parseFloat(document.getElementById('ki-heating-input')?.value || '');
                const kiCooling = parseFloat(document.getElementById('ki-cooling-input')?.value || '');

                const feedforwardEnabled = document.getElementById('feedforward-enabled-input')?.checked ?? true;
                const ffTolerance = parseFloat(document.getElementById('feedforward-tolerance-input')?.value || '');
                const ffAlpha = parseFloat(document.getElementById('feedforward-alpha-input')?.value || '');
                const ffMinDelta = parseFloat(document.getElementById('feedforward-min-delta-input')?.value || '');
                const ffMinPwm = parseFloat(document.getElementById('feedforward-min-pwm-input')?.value || '');
                const ffSteadyError = parseFloat(document.getElementById('feedforward-steady-error-input')?.value || '');
                const ffSteadyRate = parseFloat(document.getElementById('feedforward-steady-rate-input')?.value || '');
                const ffMaxGain = parseFloat(document.getElementById('feedforward-max-gain-input')?.value || '');
                const ffGainHeating = parseFloat(document.getElementById('feedforward-gain-heating-input')?.value || '');
                const ffGainCooling = parseFloat(document.getElementById('feedforward-gain-cooling-input')?.value || '');
                const leadHeat = parseFloat(document.getElementById('lead-time-heating-input')?.value || '');
                const leadCool = parseFloat(document.getElementById('lead-time-cooling-input')?.value || '');

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
                if (!isNaN(kpHeating)) queryParts.push(`kp_heating=${kpHeating}`);
                if (!isNaN(kpCooling)) queryParts.push(`kp_cooling=${kpCooling}`);
                if (!isNaN(kiHeating)) queryParts.push(`ki_heating=${kiHeating}`);
                if (!isNaN(kiCooling)) queryParts.push(`ki_cooling=${kiCooling}`);

                queryParts.push(`feedforward_enabled=${feedforwardEnabled}`);
                if (!isNaN(ffTolerance)) queryParts.push(`feedforward_tolerance=${ffTolerance}`);
                if (!isNaN(ffAlpha)) queryParts.push(`feedforward_alpha=${ffAlpha}`);
                if (!isNaN(ffMinDelta)) queryParts.push(`feedforward_min_delta=${ffMinDelta}`);
                if (!isNaN(ffMinPwm)) queryParts.push(`feedforward_min_pwm=${ffMinPwm}`);
                if (!isNaN(ffSteadyError)) queryParts.push(`feedforward_steady_error=${ffSteadyError}`);
                if (!isNaN(ffSteadyRate)) queryParts.push(`feedforward_steady_rate=${ffSteadyRate}`);
                if (!isNaN(ffMaxGain)) queryParts.push(`feedforward_max_gain=${ffMaxGain}`);
                if (!isNaN(ffGainHeating)) queryParts.push(`feedforward_gain_heating=${ffGainHeating}`);
                if (!isNaN(ffGainCooling)) queryParts.push(`feedforward_gain_cooling=${ffGainCooling}`);
                if (!isNaN(leadHeat)) queryParts.push(`lead_time_heating=${leadHeat}`);
                if (!isNaN(leadCool)) queryParts.push(`lead_time_cooling=${leadCool}`);
                const response = await fetch(`/api/params?${queryParts.join('&')}`, { cache: 'no-store' });
                const data = await response.json();
                
                alert(data.message || 'Parameters updated');
            } catch (error) {
                console.error('Error updating parameters:', error);
                alert('Failed to update parameters');
            }
        }
        
        // Reset feed-forward estimator
        async function resetFeedforward() {
            const confirmReset = confirm('Reset feed-forward learning and clear estimated gains?');
            if (!confirmReset) {
                return;
            }

            try {
                const response = await fetch('/api/params?feedforward_reset=true', { cache: 'no-store' });
                const data = await response.json();

                alert(data.message || 'Feed-forward estimator reset');
                await loadParameters();
            } catch (error) {
                console.error('Error resetting feed-forward estimator:', error);
                alert('Failed to reset feed-forward estimator');
            }
        }

        // Load current parameters
        async function loadParameters() {
            try {
                const response = await fetch('/api/params', { cache: 'no-store' });
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

                if (typeof data.kp_heating === 'number') document.getElementById('kp-heating-input').value = String(data.kp_heating);
                if (typeof data.kp_cooling === 'number') document.getElementById('kp-cooling-input').value = String(data.kp_cooling);
                if (typeof data.ki_heating === 'number') document.getElementById('ki-heating-input').value = String(data.ki_heating);
                if (typeof data.ki_cooling === 'number') document.getElementById('ki-cooling-input').value = String(data.ki_cooling);

                document.getElementById('feedforward-enabled-input').checked = Boolean(data.feedforward_enabled);
                if (typeof data.feedforward_tolerance === 'number') document.getElementById('feedforward-tolerance-input').value = data.feedforward_tolerance.toFixed(3);

                const ff = data.feedforward || {};
                if (typeof ff.alpha === 'number') document.getElementById('feedforward-alpha-input').value = String(ff.alpha);
                if (typeof ff.min_delta === 'number') document.getElementById('feedforward-min-delta-input').value = String(ff.min_delta);
                if (typeof ff.min_pwm === 'number') document.getElementById('feedforward-min-pwm-input').value = String(ff.min_pwm);
                if (typeof ff.steady_error === 'number') document.getElementById('feedforward-steady-error-input').value = String(ff.steady_error);
                if (typeof ff.steady_rate === 'number') document.getElementById('feedforward-steady-rate-input').value = String(ff.steady_rate);
                if (typeof ff.max_gain === 'number') document.getElementById('feedforward-max-gain-input').value = String(ff.max_gain);
                if (typeof ff.gain_heating === 'number') document.getElementById('feedforward-gain-heating-input').value = String(ff.gain_heating);
                if (typeof ff.gain_cooling === 'number') document.getElementById('feedforward-gain-cooling-input').value = String(ff.gain_cooling);

                if (typeof data.lead_time_heating === 'number') document.getElementById('lead-time-heating-input').value = data.lead_time_heating.toFixed(2);
                if (typeof data.lead_time_cooling === 'number') document.getElementById('lead-time-cooling-input').value = data.lead_time_cooling.toFixed(2);

                if (typeof data.manual_pwm === 'number') {
                    const manualPwmInput = document.getElementById('manual-pwm-input');
                    if (manualPwmInput) {
                        manualPwmInput.value = data.manual_pwm.toFixed(3);
                    }
                }
                const manualModeSelect = document.getElementById('manual-mode-select');
                if (manualModeSelect) {
                    const optionValues = [...manualModeSelect.options].map(opt => opt.value);
                    let selectValue = null;
                    if (typeof data.manual_requested_direction === 'string') {
                        const candidate = data.manual_requested_direction.toUpperCase();
                        if (optionValues.includes(candidate)) {
                            selectValue = candidate;
                        }
                    }
                    if (!selectValue && typeof data.manual_direction === 'string') {
                        const candidate = data.manual_direction.toUpperCase();
                        if (optionValues.includes(candidate)) {
                            selectValue = candidate;
                        }
                    }
                    if (selectValue) {
                        manualModeSelect.value = selectValue;
                    }
                }
                if (typeof data.manual_override_locked === 'boolean') {
                    manualStatus.locked = data.manual_override_locked;
                }
                if (typeof data.manual_override_enabled === 'boolean') {
                    manualStatus.enabled = data.manual_override_enabled;
                }
                if (typeof data.manual_direction === 'string') {
                    manualStatus.direction = data.manual_direction;
                }
                if (typeof data.manual_pwm === 'number') {
                    manualStatus.pwm = data.manual_pwm;
                }
                if (typeof data.manual_requested_direction === 'string') {
                    manualStatus.requestedDirection = data.manual_requested_direction;
                }
                updateManualControls();
            } catch (error) {
                console.error('Error loading parameters:', error);
            }
        }
        
        // Load PID status and update button
        async function loadPIDStatus() {
            try {
                const response = await fetch('/api/pid/status', { cache: 'no-store' });
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
                const statusResponse = await fetch('/api/pid/status', { cache: 'no-store' });
                const statusData = await statusResponse.json();
                
                const endpoint = statusData.pid_enabled ? '/api/pid/disable' : '/api/pid/enable';
                const response = await fetch(endpoint, { cache: 'no-store' });
                const data = await response.json();
                
                alert(data.message || 'PID control toggled');
                
                // Update button state
                await loadPIDStatus();
                await loadManualStatus();
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

                console.log('Loading manual status...');
                await loadManualStatus();
                
                console.log('Loading historical data...');
                await loadHistoricalData();
                
                console.log('Starting periodic updates...');
                setInterval(fetchCurrentData, UPDATE_INTERVAL);
                setInterval(loadPIDStatus, UPDATE_INTERVAL);
                setInterval(loadManualStatus, UPDATE_INTERVAL * 3);
                
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
        default=25.0,
        help="Initial temperature setpoint in °C (default: 25.0)",
    )
    parser.add_argument(
        "--kp",
        type=float,
        default=0.045,
        help="Base proportional gain before per-mode tuning (default: 0.045)",
    )
    parser.add_argument(
        "--ki",
        type=float,
        default=0.0045,
        help="Base integral gain before per-mode tuning (default: 0.0045)",
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=0.15,
        help="Temperature deadband ±°C (default: 0.15)",
    )
    parser.add_argument(
        "--deadband-heating",
        type=float,
        default=None,
        help="Deadband used while heating (temperature below setpoint). Defaults to 40% of --deadband",
    )
    parser.add_argument(
        "--deadband-cooling",
        type=float,
        default=None,
        help="Deadband used while cooling (temperature above setpoint). Defaults to 10% of --deadband",
    )
    parser.add_argument(
        "--mode-switch-delay",
        type=float,
        default=18.0,
        help="Minimum seconds between heating/cooling mode switches (default: 18.0)",
    )
    parser.add_argument(
        "--heating-pwm-cap",
        type=float,
        default=0.20,
        help="Cap on PWM during HEATING (<= pwm_max, default: 0.20)",
    )
    parser.add_argument(
        "--ma-window",
        type=int,
        default=30,
        help="Moving average window (samples) for objective temperature (default: 30)",
    )
    parser.add_argument(
        "--invert-direction",
        action="store_true",
        help="Invert H-bridge direction mapping (heating uses LOW, cooling uses HIGH)",
    )
    parser.add_argument(
        "--control-interval",
        type=float,
        default=1.0,
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
    deadband_heating=args.deadband_heating,
    deadband_cooling=args.deadband_cooling,
        control_interval=args.control_interval,
        mode_switch_delay=args.mode_switch_delay,
        heating_pwm_cap=args.heating_pwm_cap,
        ma_window=args.ma_window,
    )
    # Apply PI extra and smoothing
    if args.heating_pwm_cap is not None:
        controller.pi_controller.set_heating_pwm_cap(args.heating_pwm_cap)
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
