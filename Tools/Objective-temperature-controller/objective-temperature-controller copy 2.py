"""Objective Temperature Controller with Web GUI.

This system:
1. Fetches objective temperature from remote telemetry server (10.0.63.195:9002) at 5 Hz
2. Applies 30-sample moving average for smooth readings (6-second window)
3. Uses PI controller (1 Hz) to maintain temperature within ±0.1°C (configurable)
4. Controls Arduino (COM4) with PWM (0-0.4) and heating/cooling mode (PIN7)
5. Prevents rapid mode switching (minimum 10 seconds between switches)
6. Provides HTTP server with GET endpoint for setpoint control (no-cache)
7. Web GUI showing: current temps, humidity, time-series plots, setpoint control
8. Maintains CSV log with data persistence and gap detection
9. Learns asymmetric heat-loss gains for feed-forward PWM compensation
10. Provides per-mode PI gains plus predictive lead settings via HTTP API

Architecture:
- Data fetch thread: 5 Hz (0.2s interval) with 30-sample moving average
- Control thread: 1 Hz (1.0s interval) using smoothed temperature readings
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
    """PI controller with deadband, trend-aware damping, ambient-aware feedforward, and heating/cooling mode switching."""
    
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
        cooling_pwm_cap: Optional[float] = None,
        near_setpoint_threshold: float = 1.0,
        overshoot_guard: float = 0.2,
        pwm_near_cap: Optional[float] = None,
        trend_brake_threshold: float = 0.0015,
        predictive_brake_band: Optional[float] = None,
        ambient_feedforward_gain: float = 0.015,
    ) -> None:
        """Initialize PI controller.
        
        Args:
            setpoint: Target temperature in °C
            kp: Proportional gain
            ki: Integral gain
            deadband: Temperature tolerance ±°C
            pwm_min: Minimum PWM value (0.0)
            pwm_max: Maximum PWM value (0.4)
            mode_switch_delay: Minimum seconds between heating/cooling mode switches
            heating_pwm_cap: Optional cap on PWM during HEATING mode
            cooling_pwm_cap: Optional cap on PWM during COOLING mode
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
        # Limit PWM magnitude during HEATING and COOLING for conservative control
        self.heating_pwm_cap = (
            float(heating_pwm_cap) if heating_pwm_cap is not None else pwm_max * 0.5
        )
        self.cooling_pwm_cap = (
            float(cooling_pwm_cap) if cooling_pwm_cap is not None else pwm_max
        )

        # Overshoot and near-setpoint shaping
        self.overshoot_guard = max(0.05, float(overshoot_guard))
        self.near_setpoint_threshold = max(self.overshoot_guard * 2.5, float(near_setpoint_threshold))
        self.pwm_near_cap = max(
            self.pwm_min,
            min(
                self.pwm_max,
                float(pwm_near_cap) if pwm_near_cap is not None else self.pwm_max * 0.18,
            ),
        )
        self.overshoot_pwm_cap = min(self.pwm_near_cap, self.pwm_max * 0.18)
        self.trend_brake_threshold = max(0.0, float(trend_brake_threshold))
        self.predictive_brake_band = max(
            0.05,
            float(predictive_brake_band) if predictive_brake_band is not None else self.overshoot_guard * 0.6,
        )
        self.trend_window_seconds = 10  # Longer window for smoother trend estimation
        self.integral_bleed_rate = 0.03  # Very slow decay to maintain steady-state offset compensation (was 0.15)
        self.min_active_output = 0.001
        self.trend_damping_gain = self.pwm_max * 1.2  # Reduced from 1.8 - less aggressive trend damping

        self.integral = 0.0
        self.last_error = 0.0
        self.last_time: Optional[float] = None
        
        self.current_mode: Optional[str] = None  # "HEATING" or "COOLING"
        self.last_mode_switch_time: Optional[float] = None
        
        # Temperature history for trend detection (used in near-setpoint control)
        self.temp_history: deque = deque(maxlen=240)
        self.temp_time_history: deque = deque(maxlen=240)

        # Ambient-aware feedforward compensation (legacy)
        self.ambient_feedforward_gain = max(0.0, float(ambient_feedforward_gain))
        self.last_ambient_temp: Optional[float] = None
        self.last_objective_temp: Optional[float] = None

        # Adaptive feedforward learning from steady-state PWM
        self.adaptive_ff_enabled: bool = True  # Enable adaptive learning by default
        self.learned_baseline_heating: float = 0.0  # Learned PWM for heating mode
        self.learned_baseline_cooling: float = 0.0  # Learned PWM for cooling mode
        self.learning_rate: float = 0.05  # EMA smoothing factor (0.01-0.2 typical)
        self.learning_error_threshold: float = 0.15  # Max error (°C) for learning
        self.learning_rate_threshold: float = 0.005  # Max temp rate (°C/s) for learning
        self.learning_min_time: float = 30.0  # Minimum seconds at setpoint before learning
        self.learning_pwm_min_threshold: float = 0.005  # Ignore PWM below this
        
        # Learning state tracking
        self._stable_start_time: Optional[float] = None
        self._last_learning_update: Optional[float] = None
        self._learning_samples_heating: int = 0
        self._learning_samples_cooling: int = 0

        # Calibration offsets for temperature sensors
        self.objective_temp_offset: float = 0.0
        self.ambient_temp_offset: float = 0.0

        self._lock = threading.Lock()
    
    def set_setpoint(self, setpoint: float) -> None:
        """Update temperature setpoint."""
        with self._lock:
            self.setpoint = max(10.0, min(float(setpoint), 40.0))  # Bound between 10-40°C
            self.integral = 0.0  # Reset integral when setpoint changes
            self.temp_history.clear()
            self.temp_time_history.clear()
            logger.info("Setpoint updated to %.2f°C", self.setpoint)
    
    def get_setpoint(self) -> float:
        """Get current setpoint."""
        with self._lock:
            return self.setpoint
    
    def set_kp(self, kp: float) -> None:
        """Update proportional gain."""
        with self._lock:
            self.kp = max(0.001, min(float(kp), 0.5))  # Bound between 0.001-0.5
            self.kp_heating = self.kp
            self.kp_cooling = self.kp
            logger.info("Kp updated to %.4f", self.kp)
    
    def get_kp(self) -> float:
        """Get current Kp."""
        with self._lock:
            return self.kp

    def set_kp_heating(self, kp: float) -> None:
        with self._lock:
            self.kp_heating = max(0.001, min(float(kp), 0.5))  # Bound between 0.001-0.5
            self.kp = self.kp_heating
            logger.info("Heating Kp updated to %.4f", self.kp_heating)

    def get_kp_heating(self) -> float:
        with self._lock:
            return self.kp_heating

    def set_kp_cooling(self, kp: float) -> None:
        with self._lock:
            self.kp_cooling = max(0.001, min(float(kp), 0.5))  # Bound between 0.001-0.5
            logger.info("Cooling Kp updated to %.4f", self.kp_cooling)

    def get_kp_cooling(self) -> float:
        with self._lock:
            return self.kp_cooling
    
    def set_ki(self, ki: float) -> None:
        """Update integral gain."""
        with self._lock:
            self.ki = max(0.0001, min(float(ki), 0.1))  # Bound between 0.0001-0.1
            self.ki_heating = self.ki
            self.ki_cooling = self.ki
            logger.info("Ki updated to %.4f", self.ki)
    
    def get_ki(self) -> float:
        """Get current Ki."""
        with self._lock:
            return self.ki

    def set_ki_heating(self, ki: float) -> None:
        with self._lock:
            self.ki_heating = max(0.0001, min(float(ki), 0.1))  # Bound between 0.0001-0.1
            self.ki = self.ki_heating
            logger.info("Heating Ki updated to %.4f", self.ki_heating)

    def get_ki_heating(self) -> float:
        with self._lock:
            return self.ki_heating

    def set_ki_cooling(self, ki: float) -> None:
        with self._lock:
            self.ki_cooling = max(0.0001, min(float(ki), 0.1))  # Bound between 0.0001-0.1
            logger.info("Cooling Ki updated to %.4f", self.ki_cooling)

    def get_ki_cooling(self) -> float:
        with self._lock:
            return self.ki_cooling
    
    def set_deadband(self, deadband: float) -> None:
        """Update deadband."""
        with self._lock:
            value = max(0.05, min(float(deadband), 2.0))  # Bound between 0.05-2.0°C
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
            self.deadband_heating = max(0.05, min(float(deadband), 2.0))  # Bound between 0.05-2.0°C
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
            self.deadband_cooling = max(0.05, min(float(deadband), 2.0))  # Bound between 0.05-2.0°C
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
            self.mode_switch_delay = max(5.0, min(float(delay), 120.0))  # Bound between 5-120 seconds
            logger.info("Mode switch delay updated to %.1f s", self.mode_switch_delay)

    def get_mode_switch_delay(self) -> float:
        """Get current mode switch delay in seconds."""
        with self._lock:
            return self.mode_switch_delay

    def set_heating_pwm_cap(self, cap: float) -> None:
        """Set maximum PWM allowed in HEATING mode (<= pwm_max)."""
        with self._lock:
            cap = max(0.01, min(float(cap), self.pwm_max))  # At least 0.01, no more than pwm_max
            self.heating_pwm_cap = cap
            logger.info("Heating PWM cap set to %.3f", self.heating_pwm_cap)

    def get_heating_pwm_cap(self) -> float:
        with self._lock:
            return self.heating_pwm_cap

    def set_cooling_pwm_cap(self, cap: float) -> None:
        """Set maximum PWM allowed in COOLING mode (<= pwm_max)."""
        with self._lock:
            cap = max(0.01, min(float(cap), self.pwm_max))  # At least 0.01, no more than pwm_max
            self.cooling_pwm_cap = cap
            logger.info("Cooling PWM cap set to %.3f", self.cooling_pwm_cap)

    def get_cooling_pwm_cap(self) -> float:
        with self._lock:
            return self.cooling_pwm_cap

    def get_pwm_max(self) -> float:
        with self._lock:
            return self.pwm_max

    def get_pwm_min(self) -> float:
        with self._lock:
            return self.pwm_min

    def set_near_setpoint_threshold(self, threshold: float) -> None:
        """Set the threshold for near-setpoint control mode."""
        with self._lock:
            bounded_threshold = max(0.2, min(float(threshold), 5.0))  # Bound between 0.2-5.0°C
            self.near_setpoint_threshold = max(self.overshoot_guard * 2.0, bounded_threshold)
            logger.info("Near-setpoint threshold updated to %.2f°C", self.near_setpoint_threshold)

    def get_near_setpoint_threshold(self) -> float:
        """Get current near-setpoint threshold."""
        with self._lock:
            return self.near_setpoint_threshold

    def set_ambient_feedforward_gain(self, gain: float) -> None:
        """Set the ambient feedforward gain (PWM per °C temperature difference from ambient)."""
        with self._lock:
            self.ambient_feedforward_gain = max(0.0, float(gain))
            logger.info("Ambient feedforward gain set to %.6f", self.ambient_feedforward_gain)

    def get_ambient_feedforward_gain(self) -> float:
        """Get current ambient feedforward gain."""
        with self._lock:
            return self.ambient_feedforward_gain

    def set_objective_temp_offset(self, offset: float) -> None:
        """Set calibration offset for objective temperature sensor (added to measured value)."""
        with self._lock:
            self.objective_temp_offset = float(offset)
            logger.info("Objective temperature offset set to %.3f°C", self.objective_temp_offset)

    def get_objective_temp_offset(self) -> float:
        """Get current objective temperature calibration offset."""
        with self._lock:
            return self.objective_temp_offset

    def set_ambient_temp_offset(self, offset: float) -> None:
        """Set calibration offset for ambient temperature sensor (added to measured value)."""
        with self._lock:
            self.ambient_temp_offset = float(offset)
            logger.info("Ambient temperature offset set to %.3f°C", self.ambient_temp_offset)

    def get_ambient_temp_offset(self) -> float:
        """Get current ambient temperature calibration offset."""
        with self._lock:
            return self.ambient_temp_offset

    def enable_adaptive_feedforward(self) -> None:
        """Enable adaptive feedforward learning."""
        with self._lock:
            self.adaptive_ff_enabled = True
            logger.info("Adaptive feedforward learning ENABLED")

    def disable_adaptive_feedforward(self) -> None:
        """Disable adaptive feedforward learning."""
        with self._lock:
            self.adaptive_ff_enabled = False
            logger.info("Adaptive feedforward learning DISABLED")

    def is_adaptive_feedforward_enabled(self) -> bool:
        """Check if adaptive feedforward is enabled."""
        with self._lock:
            return self.adaptive_ff_enabled

    def reset_learned_baselines(self) -> None:
        """Reset learned baseline PWM values to zero."""
        with self._lock:
            self.learned_baseline_heating = 0.0
            self.learned_baseline_cooling = 0.0
            self._learning_samples_heating = 0
            self._learning_samples_cooling = 0
            self._stable_start_time = None
            logger.info("Learned baseline PWM values reset to zero")

    def get_learned_baselines(self) -> Dict[str, float]:
        """Get current learned baseline PWM values."""
        with self._lock:
            return {
                "heating": self.learned_baseline_heating,
                "cooling": self.learned_baseline_cooling,
                "samples_heating": self._learning_samples_heating,
                "samples_cooling": self._learning_samples_cooling,
            }

    def set_learning_rate(self, rate: float) -> None:
        """Set the learning rate for adaptive feedforward (0.01-0.2 typical)."""
        with self._lock:
            self.learning_rate = max(0.001, min(float(rate), 0.5))
            logger.info("Adaptive FF learning rate set to %.4f", self.learning_rate)

    def get_learning_rate(self) -> float:
        """Get current learning rate."""
        with self._lock:
            return self.learning_rate

    def set_learning_error_threshold(self, threshold: float) -> None:
        """Set max error (°C) for learning to occur."""
        with self._lock:
            self.learning_error_threshold = max(0.05, min(float(threshold), 1.0))
            logger.info("Learning error threshold set to %.3f°C", self.learning_error_threshold)

    def get_learning_error_threshold(self) -> float:
        """Get current learning error threshold."""
        with self._lock:
            return self.learning_error_threshold

    def set_learning_rate_threshold(self, threshold: float) -> None:
        """Set max temperature rate (°C/s) for learning to occur."""
        with self._lock:
            self.learning_rate_threshold = max(0.0001, min(float(threshold), 0.1))
            logger.info("Learning rate threshold set to %.5f°C/s", self.learning_rate_threshold)

    def get_learning_rate_threshold(self) -> float:
        """Get current learning rate threshold."""
        with self._lock:
            return self.learning_rate_threshold

    def set_learning_min_time(self, time_sec: float) -> None:
        """Set minimum seconds at setpoint before learning begins."""
        with self._lock:
            self.learning_min_time = max(5.0, min(float(time_sec), 300.0))
            logger.info("Learning minimum time set to %.1f seconds", self.learning_min_time)

    def get_learning_min_time(self) -> float:
        """Get current learning minimum time."""
        with self._lock:
            return self.learning_min_time

    def compute_ambient_baseline(
        self, 
        objective_temp: Optional[float], 
        ambient_temp: Optional[float]
    ) -> Tuple[float, Optional[str]]:
        """Compute baseline PWM to counteract thermal drift toward ambient temperature.
        
        This provides feed-forward compensation within the deadband to prevent
        temperature drift toward room temperature.
        
        Args:
            objective_temp: Current objective temperature in °C
            ambient_temp: Current ambient/room temperature in °C
            
        Returns:
            Tuple of (baseline_pwm, baseline_mode) where:
            - baseline_pwm: PWM value to counteract thermal losses
            - baseline_mode: "HEATING" if temp < setpoint, "COOLING" if temp > setpoint, or None
        """
        with self._lock:
            # Store for later reference
            if objective_temp is not None:
                self.last_objective_temp = objective_temp
            if ambient_temp is not None:
                self.last_ambient_temp = ambient_temp
            
            # Need both temperatures and a valid feedforward gain
            if (objective_temp is None or ambient_temp is None or 
                self.ambient_feedforward_gain <= 0.0):
                return 0.0, None
            
            # Temperature difference drives heat transfer
            # Positive delta_T means objective is warmer than ambient (heat loss → need heating)
            # Negative delta_T means objective is cooler than ambient (heat gain → need cooling)
            delta_T = objective_temp - ambient_temp
            
            # Determine which direction we're fighting
            error = self.setpoint - objective_temp
            
            # If we're above setpoint and it's warmer than ambient, we don't need to fight heat loss
            # If we're below setpoint and it's cooler than ambient, we don't need to fight heat gain
            # Only apply feedforward when thermal drift would move us away from setpoint
            
            if error > 0:
                # Need heating: only apply if objective < setpoint
                # Feedforward should counteract heat loss to cooler ambient
                if delta_T > 0:
                    # Objective warmer than ambient → losing heat → need heating feedforward
                    baseline_pwm = self.ambient_feedforward_gain * abs(delta_T)
                    baseline_pwm = min(baseline_pwm, self.pwm_max * 0.3)  # Cap at 30% of max
                    return baseline_pwm, "HEATING"
                else:
                    # Objective cooler than ambient → gaining heat from environment
                    # No feedforward needed (environment is helping us reach setpoint)
                    return 0.0, None
            else:
                # Need cooling: only apply if objective > setpoint
                # Feedforward should counteract heat gain from warmer ambient
                if delta_T < 0:
                    # Objective cooler than ambient → gaining heat → need cooling feedforward
                    baseline_pwm = self.ambient_feedforward_gain * abs(delta_T)
                    baseline_pwm = min(baseline_pwm, self.pwm_max * 0.3)  # Cap at 30% of max
                    return baseline_pwm, "COOLING"
                else:
                    # Objective warmer than ambient → losing heat to environment
                    # No feedforward needed (environment is helping us reach setpoint)
                    return 0.0, None
    
    def reset(self) -> None:
        """Reset controller state."""
        with self._lock:
            self.integral = 0.0
            self.last_error = 0.0
            self.last_time = None
            self.temp_history.clear()
            self.temp_time_history.clear()
            self.last_ambient_temp = None
            self.last_objective_temp = None
            logger.info("PI controller reset")
    
    def _pwm_limit_for_mode(self, mode: str) -> float:
        if mode == "HEATING":
            return self.heating_pwm_cap
        elif mode == "COOLING":
            return self.cooling_pwm_cap
        return self.pwm_max

    def _update_adaptive_learning(
        self, 
        current_time: float,
        error: float,
        temp_trend: Optional[float],
        pwm_value: float,
        mode: str
    ) -> None:
        """Update adaptive feedforward learning based on steady-state observations.
        
        When the system is stable near setpoint (small error, low temp rate), the current
        PWM represents the steady-state output needed to compensate for all thermal loads.
        We learn this value and use it as feedforward.
        
        Args:
            current_time: Current timestamp
            error: Temperature error (setpoint - current)
            temp_trend: Temperature rate of change (°C/s)
            pwm_value: Current PWM output
            mode: Current operating mode ("HEATING" or "COOLING")
        """
        if not self.adaptive_ff_enabled:
            return
        
        if mode not in ("HEATING", "COOLING"):
            self._stable_start_time = None
            return
        
        # Check if conditions are suitable for learning
        abs_error = abs(error)
        abs_trend = abs(temp_trend) if temp_trend is not None else float('inf')
        
        is_stable = (
            abs_error <= self.learning_error_threshold and
            abs_trend <= self.learning_rate_threshold and
            pwm_value >= self.learning_pwm_min_threshold
        )
        
        if not is_stable:
            # Not stable - reset timer
            self._stable_start_time = None
            return
        
        # Start or continue stability timer
        if self._stable_start_time is None:
            self._stable_start_time = current_time
            return
        
        # Check if we've been stable long enough
        stable_duration = current_time - self._stable_start_time
        if stable_duration < self.learning_min_time:
            return
        
        # Conditions met - update learned baseline using exponential moving average
        if mode == "HEATING":
            old_value = self.learned_baseline_heating
            self.learned_baseline_heating = (
                (1.0 - self.learning_rate) * self.learned_baseline_heating +
                self.learning_rate * pwm_value
            )
            self._learning_samples_heating += 1
            logger.info(
                "Adaptive FF learning [HEATING]: PWM %.4f → baseline %.4f→%.4f (n=%d, stable=%.1fs)",
                pwm_value,
                old_value,
                self.learned_baseline_heating,
                self._learning_samples_heating,
                stable_duration
            )
        else:  # COOLING
            old_value = self.learned_baseline_cooling
            self.learned_baseline_cooling = (
                (1.0 - self.learning_rate) * self.learned_baseline_cooling +
                self.learning_rate * pwm_value
            )
            self._learning_samples_cooling += 1
            logger.info(
                "Adaptive FF learning [COOLING]: PWM %.4f → baseline %.4f→%.4f (n=%d, stable=%.1fs)",
                pwm_value,
                old_value,
                self.learned_baseline_cooling,
                self._learning_samples_cooling,
                stable_duration
            )
        
        self._last_learning_update = current_time

    def _get_adaptive_baseline(self, mode: str) -> float:
        """Get learned baseline PWM for the given mode.
        
        Args:
            mode: Operating mode ("HEATING" or "COOLING")
            
        Returns:
            Learned baseline PWM value
        """
        if not self.adaptive_ff_enabled:
            return 0.0
        
        if mode == "HEATING":
            return self.learned_baseline_heating
        elif mode == "COOLING":
            return self.learned_baseline_cooling
        return 0.0

    def _calculate_temp_trend(self) -> Optional[float]:
        """Calculate temperature trend (°C/s) from recent history.
        
        Returns:
            Temperature rate of change in °C/s, or None if insufficient data
        """
        if len(self.temp_history) < 3:
            return None
        
        # Use linear regression for more robust trend estimation
        temps = list(self.temp_history)
        times_raw = list(self.temp_time_history)

        if not times_raw:
            return None

        t0 = times_raw[0]
        times = [t - t0 for t in times_raw]
        span = times[-1] - times[0]
        if span < 1.0:
            return None

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
        """Compute PWM output and heating/cooling mode with aggressive overshoot suppression."""

        with self._lock:
            current_time = time.time()

            # Update temperature history for trend detection and prune stale samples
            self.temp_history.append(float(current_temp))
            self.temp_time_history.append(current_time)
            while (
                self.temp_time_history
                and current_time - self.temp_time_history[0] > self.trend_window_seconds
            ):
                self.temp_time_history.popleft()
                self.temp_history.popleft()

            # Sanitize baseline inputs (ambient-based feedforward from external caller)
            ambient_baseline_pwm = max(self.pwm_min, min(float(baseline_pwm), self.pwm_max))
            if ambient_baseline_pwm <= self.pwm_min + 1e-9:
                ambient_baseline_pwm = 0.0
            if ambient_baseline_pwm == 0.0 or baseline_mode not in ("HEATING", "COOLING"):
                baseline_mode = None

            # Error between setpoint and measurement
            error = self.setpoint - current_temp
            abs_error = abs(error)
            active_deadband = self.deadband_heating if error >= 0 else self.deadband_cooling
            within_deadband = abs_error <= active_deadband
            
            # Determine preferred mode for adaptive baseline
            preferred_mode_for_adaptive = "HEATING" if error > 0 else "COOLING"
            
            # Get adaptive learned baseline for the preferred mode
            adaptive_baseline_pwm = self._get_adaptive_baseline(preferred_mode_for_adaptive)
            
            # Combine ambient and adaptive baselines (use max to avoid conflicting directions)
            baseline_pwm = max(ambient_baseline_pwm, adaptive_baseline_pwm)
            
            # If adaptive baseline is dominant, use its mode preference
            if adaptive_baseline_pwm > ambient_baseline_pwm:
                baseline_mode = preferred_mode_for_adaptive if adaptive_baseline_pwm > 0 else None

            # Reject feed-forward that fights the required direction
            if baseline_mode == "HEATING" and error < -self.deadband_cooling:
                logger.debug("Discarding heating feed-forward (err=%.3f°C requires cooling)", error)
                baseline_mode = None
                baseline_pwm = 0.0
            elif baseline_mode == "COOLING" and error > self.deadband_heating:
                logger.debug("Discarding cooling feed-forward (err=%.3f°C requires heating)", error)
                baseline_mode = None
                baseline_pwm = 0.0

            preferred_mode = (
                baseline_mode if baseline_mode is not None else ("HEATING" if error > 0 else "COOLING")
            )

            # --------------------------------------------------------------------
            # Deadband handling (coast with gentle integral bleed)
            # --------------------------------------------------------------------
            if within_deadband:
                decay_dt = 0.0 if self.last_time is None else max(0.0, current_time - self.last_time)
                if decay_dt > 0.0 and self.integral > 0.0:
                    bleed = self.integral_bleed_rate * decay_dt
                    self.integral = max(0.0, self.integral - bleed)

                self.last_time = current_time
                self.last_error = error

                if baseline_pwm > 0.0 and baseline_mode in ("HEATING", "COOLING"):
                    target_mode = baseline_mode
                    if self.current_mode is not None and target_mode != self.current_mode:
                        if self.last_mode_switch_time is not None:
                            elapsed = current_time - self.last_mode_switch_time
                            if elapsed < self.mode_switch_delay:
                                logger.debug(
                                    "Mode switch delayed by %.1fs (limit %.1fs)", elapsed, self.mode_switch_delay
                                )
                                return (
                                    0.0,
                                    self.current_mode if self.current_mode in ("HEATING", "COOLING") else "OFF",
                                )

                    if target_mode != self.current_mode:
                        logger.info("Mode switching: %s → %s", self.current_mode, target_mode)
                        self.current_mode = target_mode
                        self.last_mode_switch_time = current_time
                        self.integral = 0.0

                    if self.current_mode not in ("HEATING", "COOLING"):
                        self.current_mode = "OFF"
                        return 0.0, "OFF"

                    pwm_limit = self._pwm_limit_for_mode(self.current_mode)
                    pwm_value = min(baseline_pwm, pwm_limit)
                    return pwm_value, self.current_mode

                self.current_mode = "OFF"
                return 0.0, "OFF"

            # --------------------------------------------------------------------
            # Determine mode and timing
            # --------------------------------------------------------------------
            dt = 0.0 if self.last_time is None else max(0.0, current_time - self.last_time)
            self.last_time = current_time

            desired_mode = preferred_mode
            if self.current_mode is not None and desired_mode != self.current_mode:
                if self.last_mode_switch_time is not None:
                    elapsed = current_time - self.last_mode_switch_time
                    if elapsed < self.mode_switch_delay:
                        logger.debug(
                            "Mode switch delayed by %.1fs (limit %.1fs)", elapsed, self.mode_switch_delay
                        )
                        if baseline_mode != self.current_mode:
                            baseline_pwm = 0.0
                        desired_mode = self.current_mode

            if desired_mode != self.current_mode:
                logger.info("Mode switching: %s → %s", self.current_mode, desired_mode)
                self.current_mode = desired_mode
                self.last_mode_switch_time = current_time
                self.integral = 0.0

            if self.current_mode not in ("HEATING", "COOLING"):
                logger.error("Invalid mode after update: %s", self.current_mode)
                self.current_mode = "OFF"
                self.last_error = error
                return 0.0, "OFF"

            if self.current_mode == "HEATING":
                current_kp = self.kp_heating
                current_ki = self.ki_heating
            else:
                current_kp = self.kp_cooling
                current_ki = self.ki_cooling

            pwm_limit = self._pwm_limit_for_mode(self.current_mode)
            baseline_pwm = min(baseline_pwm, pwm_limit)

            # --------------------------------------------------------------------
            # Dynamic damping near setpoint
            # --------------------------------------------------------------------
            trend = self._calculate_temp_trend()
            effective_trend = 0.0
            approaching = False
            moving_away = False
            if trend is not None:
                direction = 1.0 if self.current_mode == "HEATING" else -1.0
                effective_trend = trend * direction
                # Approaching means temperature is moving toward setpoint
                approaching = effective_trend > self.trend_brake_threshold
                # Moving away means temperature is moving away from setpoint
                moving_away = effective_trend < -self.trend_brake_threshold

            effective_error = max(0.0, abs_error - active_deadband)
            # Only bleed integral if we're actually within deadband AND not moving away
            if effective_error <= 0.0 and dt > 0.0 and self.integral > 0.0:
                # Don't bleed if temperature is moving away from setpoint
                if not moving_away:
                    bleed = self.integral_bleed_rate * dt
                    self.integral = max(0.0, self.integral - bleed)
                effective_error = 0.0

            kp_scale = 1.0
            ki_scale = 1.0

            # If moving away from setpoint, boost gains to counter the trend
            if moving_away and effective_error > 0.0:
                # Boost proportional and integral gains when temperature is moving wrong direction
                trend_boost = min(3.0, 1.5 + abs(effective_trend) / 0.005)  # Up to 3x boost
                kp_scale *= trend_boost
                ki_scale *= trend_boost
                logger.debug(
                    "Temperature moving AWAY from setpoint (trend=%.4f°C/s), boosting gains by %.2fx",
                    trend,
                    trend_boost
                )
                # Don't apply any damping zones when moving away
            elif abs_error <= self.overshoot_guard and approaching:
                # OVERSHOOT GUARD: Very close to setpoint and approaching - prevent overshoot
                # This takes priority over near-setpoint zone when very close
                guard_fraction = max(0.0, abs_error / self.overshoot_guard)
                guard_scale = max(0.3, guard_fraction)  # Minimum 30% to avoid over-damping
                kp_scale *= guard_scale
                ki_scale *= guard_scale
                pwm_limit = min(
                    pwm_limit,
                    max(
                        self.pwm_min,
                        self.overshoot_pwm_cap * max(0.4, guard_scale),
                    ),
                )
                # DON'T scale the integral here - let the reduced gains and PWM limit do the work
                # This preserves integral for quick recovery if temperature reverses

                # Only apply emergency brake if VERY close and approaching VERY fast
                if abs_error <= self.predictive_brake_band and effective_trend > 0.008:
                    # Very close to setpoint and approaching fast - apply strong brake
                    # Scale gains way down but keep some integral
                    kp_scale *= 0.15
                    ki_scale *= 0.15
                    pwm_limit = self.min_active_output * 5
                    logger.debug("Emergency brake: very close (%.3f°C) and fast approach (%.4f°C/s)", abs_error, effective_trend)
                    # Don't return early - let the control calculation proceed with reduced gains
            elif self.near_setpoint_threshold > active_deadband:
                # NEAR-SETPOINT ZONE: Gentle damping when getting close but not in overshoot guard
                slow_span = max(1e-6, self.near_setpoint_threshold - active_deadband)
                zone_fraction = min(1.0, effective_error / slow_span)
                if zone_fraction < 1.0:
                    zone_scale = max(0.20, zone_fraction ** 1.5)  # Minimum 20%, less aggressive curve
                    kp_scale *= zone_scale
                    ki_scale *= zone_scale
                    pwm_limit = min(
                        pwm_limit,
                        max(
                            self.pwm_near_cap,
                            self.pwm_near_cap + (pwm_limit - self.pwm_near_cap) * zone_fraction,
                        ),
                    )
                    # Scale integral when approaching to prevent overshoot buildup
                    if approaching:
                        self.integral *= max(0.6, zone_fraction)  # Preserve at least 60% of integral

            current_kp *= kp_scale
            current_ki *= ki_scale

            # --------------------------------------------------------------------
            # PI with saturation-aware anti-windup
            # --------------------------------------------------------------------
            integral_candidate = self.integral
            if dt > 0.0 and current_ki > 0.0 and effective_error > 0.0:
                # Accumulate integral faster when moving away from setpoint
                integral_gain_boost = 1.5 if moving_away else 1.0
                integral_candidate += effective_error * dt * integral_gain_boost

            p_term = current_kp * effective_error
            i_term_candidate = current_ki * integral_candidate if current_ki > 0.0 else 0.0
            raw_feedback = p_term + i_term_candidate
            max_feedback = max(0.0, pwm_limit - baseline_pwm)

            if raw_feedback > max_feedback + 1e-9:
                feedback = max_feedback
                if current_ki > 0.0:
                    allowed_i = max(0.0, max_feedback - p_term)
                    self.integral = allowed_i / current_ki
                else:
                    self.integral = 0.0
            else:
                feedback = raw_feedback
                self.integral = max(0.0, integral_candidate)

            # Only apply trend brake if approaching AND not moving away
            if approaching and not moving_away and feedback > 0.0 and trend is not None:
                brake = effective_trend * self.trend_damping_gain
                if brake > 0.0:
                    feedback = max(0.0, feedback - brake)
                    if current_ki > 0.0:
                        allowed_i = max(0.0, feedback - p_term)
                        self.integral = allowed_i / current_ki
                    else:
                        self.integral = 0.0

            pwm_output = baseline_pwm + feedback
            pwm_value = max(self.pwm_min, min(pwm_output, pwm_limit))
            
            # Only zero output if PWM is truly minimal AND we're not moving away from setpoint
            if pwm_value <= self.min_active_output:
                if moving_away:
                    # Keep a minimal PWM to fight the trend, don't zero integral
                    pwm_value = max(self.min_active_output * 2, pwm_value)
                    logger.debug("Keeping minimal PWM (%.4f) despite low output - moving away from setpoint", pwm_value)
                else:
                    # Safe to zero output when stable or approaching
                    pwm_value = 0.0
                    self.integral = 0.0

            self.last_error = error
            
            # Update adaptive feedforward learning
            self._update_adaptive_learning(
                current_time=current_time,
                error=error,
                temp_trend=trend,
                pwm_value=pwm_value,
                mode=self.current_mode
            )

            logger.debug(
                "PI+: err=%.3f°C (deadband %.3f°C), trend=%.4f°C/s, P=%.4f, I=%.4f, FF_amb=%.4f, FF_adapt=%.4f, pwm=%.4f, mode=%s",
                error,
                active_deadband,
                trend if trend is not None else 0.0,
                current_kp * effective_error,
                current_ki * self.integral if current_ki > 0.0 else 0.0,
                ambient_baseline_pwm,
                adaptive_baseline_pwm,
                pwm_value,
                self.current_mode,
            )

            return pwm_value, self.current_mode


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
    
    def __init__(self, base_url: str, timeout: float = 0.5):
        """Initialize telemetry client.
        
        Args:
            base_url: Base URL of telemetry server (e.g., "http://10.0.63.195:9002")
            timeout: Request timeout in seconds (default: 0.5s for fast polling)
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
            "pwm_feedback",
            "temp_rate",
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
        pwm_feedback: float = 0.0,
        temp_rate: Optional[float] = None,
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
                "pwm_feedback": f"{pwm_feedback:.4f}",
                "temp_rate": f"{temp_rate:.5f}" if temp_rate is not None else "",
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
        kp: float = 0.08,
        ki: float = 0.012,
        kp_heating: Optional[float] = None,
        ki_heating: Optional[float] = None,
        kp_cooling: Optional[float] = None,
        ki_cooling: Optional[float] = None,
        deadband: float = 0.10,
        deadband_heating: Optional[float] = None,
        deadband_cooling: Optional[float] = None,
        control_interval: float = 0.2,
        mode_switch_delay: float = 15.0,
        heating_pwm_cap: Optional[float] = None,
        cooling_pwm_cap: Optional[float] = None,
        pwm_max: float = 0.4,
        ma_window: int = 10,
        ambient_feedforward_gain: float = 0.015,
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
            heating_pwm_cap: Optional cap on PWM during HEATING mode
            cooling_pwm_cap: Optional cap on PWM during COOLING mode
        """
        self.arduino_port = arduino_port
        self.telemetry_url = telemetry_url
        self.log_dir = log_dir
        self.control_interval = control_interval
        
        # Use symmetric deadbands by default for more predictable control
        if deadband_heating is None:
            deadband_heating = deadband
        if deadband_cooling is None:
            deadband_cooling = deadband
        
        # Use base gains for mode-specific gains if not specified
        if kp_heating is None:
            kp_heating = kp
        if ki_heating is None:
            ki_heating = ki
        if kp_cooling is None:
            kp_cooling = kp
        if ki_cooling is None:
            ki_cooling = ki
        
        # Set default PWM caps if not specified
        if heating_pwm_cap is None:
            heating_pwm_cap = pwm_max
        if cooling_pwm_cap is None:
            cooling_pwm_cap = pwm_max

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
            cooling_pwm_cap=cooling_pwm_cap,
            near_setpoint_threshold=max(0.8, deadband * 5.0),  # Start gentle damping earlier for smoother approach
            overshoot_guard=0.15,  # Reduced to allow closer approach before aggressive damping
            pwm_near_cap=0.15,  # Moderate cap (15%) - enough to reach setpoint but gentle to minimize overshoot
            trend_brake_threshold=0.004,  # Only brake on fast approaches (>0.004°C/s) to allow slow steady convergence
            predictive_brake_band=0.05,  # Very tight band (0.05°C) - only emergency brake if overshooting close to setpoint
            ambient_feedforward_gain=ambient_feedforward_gain,
        )
        
        self.arduino = ArduinoController(port=arduino_port)
        self.telemetry = TelemetryClient(base_url=telemetry_url)
        self.logger = DataLogger(log_dir=log_dir)
        
        # Control loop state
        self.running = False
        self.control_thread: Optional[threading.Thread] = None
        self.data_fetch_thread: Optional[threading.Thread] = None
        self.pid_enabled = False  # PID control is disabled by default
        
        # Data buffer for GUI (keep last 1000 points)
        self.data_buffer: deque = deque(maxlen=1000)
        self.buffer_lock = threading.Lock()

        # Moving average smoothing for objective temperature
        # Fetch at 5 Hz with 30-sample moving average for smooth readings
        self.data_fetch_interval: float = 0.2  # 5 Hz = 1/5 = 0.2 seconds
        self.ma_window: int = 30  # Fixed at 30 samples for 6-second smoothing window
        self._ma_buffer: deque = deque(maxlen=self.ma_window)
        self._raw_temp_buffer: deque = deque(maxlen=self.ma_window)
        self._smoothed_temp: Optional[float] = None
        self._smoothed_temp_lock = threading.Lock()

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
        """Set moving average window (number of samples).
        
        Note: With 5 Hz data fetching, 30 samples = 6 second smoothing window.
        """
        w = max(1, int(window))
        if w != self.ma_window:
            self.ma_window = w
            # Update the raw buffer maxlen
            old_data = list(self._raw_temp_buffer)
            self._raw_temp_buffer = deque(old_data[-w:], maxlen=w)
            logger.info(
                "Moving average window updated to %d samples (%.1f second window at 5 Hz)",
                self.ma_window,
                self.ma_window * self.data_fetch_interval
            )

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
            pid_still_enabled = self.pid_enabled

        if pid_still_enabled:
            logger.info("Manual override ENABLED (will take precedence over PID)")
        else:
            logger.info("Manual override ENABLED")
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
        
        # Start threads
        self.running = True
        
        # Start data fetch thread (5 Hz)
        self.data_fetch_thread = threading.Thread(target=self._data_fetch_loop, daemon=True)
        self.data_fetch_thread.start()
        logger.info("Data fetch thread started (5 Hz with 30-sample moving average)")
        
        # Start control loop thread (1 Hz)
        self.control_thread = threading.Thread(target=self._control_loop, daemon=True)
        self.control_thread.start()
        logger.info("Control thread started (1 second interval)")
        
        logger.info("Temperature controller started")
    
    def stop(self) -> None:
        """Stop the temperature controller."""
        if not self.running:
            return
        
        logger.info("Stopping temperature controller...")
        self.running = False
        
        # Wait for data fetch thread
        if self.data_fetch_thread:
            self.data_fetch_thread.join(timeout=5.0)
        
        # Wait for control thread
        if self.control_thread:
            self.control_thread.join(timeout=5.0)
        
        # Disconnect Arduino
        self.arduino.disconnect()
        
        # Close logger
        self.logger.close()
        
        logger.info("Temperature controller stopped")
    
    def _data_fetch_loop(self) -> None:
        """High-frequency data fetch loop (5 Hz) with moving average smoothing."""
        logger.info("Data fetch loop started (5 Hz)")
        
        while self.running:
            try:
                loop_start = time.time()
                
                # Fetch raw objective temperature
                raw_temp = self.telemetry.get_objective_temperature()
                
                if raw_temp is not None:
                    # Apply calibration offset
                    calibrated_temp = raw_temp + self.pi_controller.get_objective_temp_offset()
                    
                    # Add to raw buffer
                    self._raw_temp_buffer.append(float(calibrated_temp))
                    
                    # Compute moving average
                    if len(self._raw_temp_buffer) > 0:
                        smoothed = sum(self._raw_temp_buffer) / len(self._raw_temp_buffer)
                        
                        # Update smoothed temperature (thread-safe)
                        with self._smoothed_temp_lock:
                            self._smoothed_temp = smoothed
                        
                        logger.debug(
                            "Data fetch: raw=%.3f°C, smoothed=%.3f°C (n=%d samples)",
                            calibrated_temp,
                            smoothed,
                            len(self._raw_temp_buffer)
                        )
                else:
                    logger.debug("Data fetch: No temperature data available")
                
                # Sleep for data fetch interval (5 Hz = 0.2s)
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.data_fetch_interval - elapsed)
                time.sleep(sleep_time)
                
            except Exception as exc:
                logger.exception("Error in data fetch loop: %s", exc)
                time.sleep(self.data_fetch_interval)
        
        logger.info("Data fetch loop stopped")
    
    def _control_loop(self) -> None:
        """Main control loop (runs in separate thread)."""
        logger.info("Control loop started")
        
        while self.running:
            try:
                loop_start = time.time()
                
                # 1. Get smoothed objective temperature from data fetch thread
                with self._smoothed_temp_lock:
                    obj_temp = self._smoothed_temp
                
                # 2. Get ambient data and apply calibration offset
                room_temp_raw, humidity = self.telemetry.get_ambient_data()
                room_temp = None
                if room_temp_raw is not None:
                    room_temp = room_temp_raw + self.pi_controller.get_ambient_temp_offset()
                
                # 3. Get setpoint (always available)
                setpoint = self.pi_controller.get_setpoint()
                
                # 4. Initialize control state
                pwm_total = 0.0
                mode = "OFF"
                pwm_feedback = 0.0
                control_source = "PID"
                manual_direction_active: Optional[str] = None
                manual_pwm_target = 0.0
                manual_override_active = False
                manual_pwm_log: Optional[float] = None
                manual_requested_direction = "OFF"

                # 5. Snapshot configurable parameters for this iteration
                with self._config_lock:
                    manual_override_active = self.manual_override_enabled
                    manual_direction_active = self.manual_direction
                    manual_pwm_target = self.manual_pwm
                    manual_requested_direction = self.manual_direction_requested

                # 6. Estimate temperature rate of change (for logging only)
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
                    # 8. Run PI controller with ambient-aware feedforward compensation
                    if self.pid_enabled and obj_temp is not None:
                        # Compute baseline PWM to counteract thermal drift toward ambient
                        baseline_pwm, baseline_mode = self.pi_controller.compute_ambient_baseline(
                            objective_temp=obj_temp,
                            ambient_temp=room_temp
                        )
                        
                        # Log feedforward calculation
                        if baseline_pwm > 0.0:
                            logger.debug(
                                "Ambient feedforward: baseline_pwm=%.4f, mode=%s, obj_temp=%.2f, room_temp=%s",
                                baseline_pwm,
                                baseline_mode,
                                obj_temp,
                                f"{room_temp:.2f}" if room_temp is not None else "N/A"
                            )
                        
                        pwm_total, mode = self.pi_controller.compute(
                            current_temp=obj_temp,
                            baseline_pwm=baseline_pwm,
                            baseline_mode=baseline_mode,
                        )

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
                        logger.warning("No objective temperature available")

                    manual_direction_active = None
                    manual_pwm_log = None

                pwm_feedback = pwm_total
                error_value = setpoint - obj_temp if obj_temp is not None else None
                
                # 7. Log data (even if obj_temp is None)
                self.logger.log_data(
                    objective_temp=obj_temp,
                    setpoint=setpoint,
                    pwm=pwm_total,
                    mode=mode,
                    room_temp=room_temp,
                    humidity=humidity,
                    error=error_value,
                    pwm_feedback=pwm_feedback,
                    temp_rate=temp_rate,
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
                    "pwm_feedback": pwm_feedback,
                    "temp_rate": temp_rate,
                    "error": error_value,
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
                    "T_obj=%s, Setpoint=%.2f°C, PWM=%.3f, Mode=%s, Source=%s, ManualDir=%s, ManualReq=%s, T_room=%s, RH=%s",
                    f"{obj_temp:.2f}°C" if obj_temp is not None else "N/A",
                    setpoint,
                    pwm_total,
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
                    "pwm_feedback": 0.0,
                    "temp_rate": None,
                    "error": None,
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
    
    def enable_pid(self) -> None:
        """Enable PID control."""
        with self._config_lock:
            manual_active = self.manual_override_enabled
        if manual_active:
            logger.info("PID control ENABLED - Manual override is active and will take precedence")
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
            "cooling_pwm_cap": self.pi_controller.get_cooling_pwm_cap(),
            "pwm_max": self.pi_controller.get_pwm_max(),
            "near_setpoint_threshold": self.pi_controller.get_near_setpoint_threshold(),
            "ambient_feedforward_gain": self.pi_controller.get_ambient_feedforward_gain(),
            "objective_temp_offset": self.pi_controller.get_objective_temp_offset(),
            "ambient_temp_offset": self.pi_controller.get_ambient_temp_offset(),
            "control_interval": self.control_interval,
            "pid_enabled": self.pid_enabled,
            "ma_window": self.get_ma_window(),
            "invert_direction": self.arduino.get_invert_direction() if self.arduino else False,
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
                    "pwm_feedback": 0.0,
                    "temp_rate": None,
                    "error": None,
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
                                "pwm_feedback": float(row["pwm_feedback"]) if row.get("pwm_feedback") else 0.0,
                                "temp_rate": float(row["temp_rate"]) if row.get("temp_rate") else None,
                                "error": float(row["error"]) if row.get("error") else None,
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
        elif path == "/api/adaptive/status":
            self._serve_adaptive_status()
        elif path == "/api/adaptive/enable":
            self._serve_adaptive_enable()
        elif path == "/api/adaptive/disable":
            self._serve_adaptive_disable()
        elif path == "/api/adaptive/reset":
            self._serve_adaptive_reset()
        elif path == "/api/adaptive/params":
            self._serve_adaptive_params(parsed.query)
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

            if "cooling_pwm_cap" in params:
                new_cap = float(params["cooling_pwm_cap"][0])
                self.controller.pi_controller.set_cooling_pwm_cap(new_cap)
                updated["cooling_pwm_cap"] = new_cap

            if "near_setpoint_threshold" in params:
                new_threshold = float(params["near_setpoint_threshold"][0])
                self.controller.pi_controller.set_near_setpoint_threshold(new_threshold)
                updated["near_setpoint_threshold"] = new_threshold

            if "ambient_feedforward_gain" in params:
                new_gain = float(params["ambient_feedforward_gain"][0])
                self.controller.pi_controller.set_ambient_feedforward_gain(new_gain)
                updated["ambient_feedforward_gain"] = new_gain

            if "objective_temp_offset" in params:
                new_offset = float(params["objective_temp_offset"][0])
                self.controller.pi_controller.set_objective_temp_offset(new_offset)
                updated["objective_temp_offset"] = new_offset

            if "ambient_temp_offset" in params:
                new_offset = float(params["ambient_temp_offset"][0])
                self.controller.pi_controller.set_ambient_temp_offset(new_offset)
                updated["ambient_temp_offset"] = new_offset

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
    
    def _serve_adaptive_status(self) -> None:
        """Get adaptive feedforward status and learned baselines."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        enabled = self.controller.pi_controller.is_adaptive_feedforward_enabled()
        baselines = self.controller.pi_controller.get_learned_baselines()
        
        self._send_json({
            "enabled": enabled,
            "learned_baseline_heating": baselines["heating"],
            "learned_baseline_cooling": baselines["cooling"],
            "samples_heating": baselines["samples_heating"],
            "samples_cooling": baselines["samples_cooling"],
            "learning_rate": self.controller.pi_controller.get_learning_rate(),
            "learning_error_threshold": self.controller.pi_controller.get_learning_error_threshold(),
            "learning_rate_threshold": self.controller.pi_controller.get_learning_rate_threshold(),
            "learning_min_time": self.controller.pi_controller.get_learning_min_time(),
        })
    
    def _serve_adaptive_enable(self) -> None:
        """Enable adaptive feedforward learning."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        self.controller.pi_controller.enable_adaptive_feedforward()
        self._send_json({"message": "Adaptive feedforward enabled", "enabled": True})
    
    def _serve_adaptive_disable(self) -> None:
        """Disable adaptive feedforward learning."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        self.controller.pi_controller.disable_adaptive_feedforward()
        self._send_json({"message": "Adaptive feedforward disabled", "enabled": False})
    
    def _serve_adaptive_reset(self) -> None:
        """Reset learned baseline values."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        self.controller.pi_controller.reset_learned_baselines()
        self._send_json({"message": "Learned baselines reset to zero"})
    
    def _serve_adaptive_params(self, query: str) -> None:
        """Set adaptive feedforward learning parameters."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return
        
        params = parse_qs(query)
        updated = {}
        
        try:
            if "learning_rate" in params:
                value = float(params["learning_rate"][0])
                self.controller.pi_controller.set_learning_rate(value)
                updated["learning_rate"] = value
            
            if "error_threshold" in params:
                value = float(params["error_threshold"][0])
                self.controller.pi_controller.set_learning_error_threshold(value)
                updated["error_threshold"] = value
            
            if "rate_threshold" in params:
                value = float(params["rate_threshold"][0])
                self.controller.pi_controller.set_learning_rate_threshold(value)
                updated["rate_threshold"] = value
            
            if "min_time" in params:
                value = float(params["min_time"][0])
                self.controller.pi_controller.set_learning_min_time(value)
                updated["min_time"] = value
            
            self._send_json({"message": "Learning parameters updated", "updated": updated})
        
        except (ValueError, IndexError) as exc:
            self.send_error(400, f"Invalid parameter: {exc}")
    
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
                <h3>PI Output</h3>
                <div class="value" id="pwm-feedback">--</div>
            </div>
            <div class="status-item">
                <h3>Temperature Rate</h3>
                <div class="value" id="temp-rate">--</div>
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
                <button onclick="updateParams()" style="margin-left: 30px;">Update Parameters</button>
            </div>

            <div class="control-section" style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #e9ecef;">
                <h3 class="control-heading">🔧 Temperature Sensor Calibration</h3>
                <div class="control-hint" style="margin-bottom: 10px;">Add or subtract values to correct for sensor calibration errors</div>
                <div class="control-group">
                    <label for="obj-offset-input">Objective Temp Offset:</label>
                    <input type="number" id="obj-offset-input" step="0.01" min="-10" max="10" value="0.00">
                    <span class="unit">°C</span>
                    
                    <label for="amb-offset-input" style="margin-left: 30px;">Ambient Temp Offset:</label>
                    <input type="number" id="amb-offset-input" step="0.01" min="-10" max="10" value="0.00">
                    <span class="unit">°C</span>
                    
                    <button onclick="applyOffsets()" style="margin-left: 20px;">Apply Offsets</button>
                </div>
            </div>
            
            <div class="control-group" style="margin-top: 15px;">
                <label for="kp-input">Kp (Proportional):</label>
                <input type="number" id="kp-input" step="0.001" min="0" max="1" value="0.045">
                
                <label for="ki-input" style="margin-left: 20px;">Ki (Integral):</label>
                <input type="number" id="ki-input" step="any" min="0" max="1" value="0.0045">
                
                <label for="interval-input" style="margin-left: 20px;">Interval (s):</label>
                <input type="number" id="interval-input" step="0.05" min="0.1" max="10" value="0.2">
                
                <label for="deadband-input" style="margin-left: 20px;">Deadband (°C):</label>
                <input type="number" id="deadband-input" step="0.05" min="0" max="2" value="0.15">

                <label for="mode-delay-input" style="margin-left: 20px;">Mode Delay (s):</label>
                <input type="number" id="mode-delay-input" step="1" min="0" max="120" value="18">

                <label for="ma-window-input" style="margin-left: 20px;">MA Window (samples):</label>
                <input type="number" id="ma-window-input" step="1" min="1" max="600" value="10">

                <label for="heating-cap-input" style="margin-left: 20px;">Heating PWM Cap:</label>
                <input type="number" id="heating-cap-input" step="0.01" min="0" max="0.4" value="0.20">

                <label for="cooling-cap-input" style="margin-left: 20px;">Cooling PWM Cap:</label>
                <input type="number" id="cooling-cap-input" step="0.01" min="0" max="0.4" value="0.20">

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

            <div class="control-section">
                <h3 class="control-heading">🤖 Adaptive Feedforward Learning</h3>
                <div class="control-hint" style="margin-bottom: 10px;">Automatically learns steady-state PWM needed to maintain setpoint by observing stable temperature conditions.</div>
                <div class="control-group">
                    <button id="adaptive-toggle-btn" onclick="toggleAdaptiveFF()">Disable Learning</button>
                    <span id="adaptive-status" style="font-weight: 600; color: #27ae60; margin-left: 10px;">ENABLED</span>
                    <button onclick="resetLearnedBaselines()" style="margin-left: 20px;">Reset Learned Values</button>
                </div>
                <div class="control-group" style="margin-top: 10px;">
                    <label>Learned Heating PWM:</label>
                    <span id="learned-heating-pwm" style="font-family: monospace; font-weight: bold; margin-right: 20px;">--</span>
                    <span id="learned-heating-samples" style="color: #666; font-size: 0.9em;">(0 samples)</span>
                    
                    <label style="margin-left: 30px;">Learned Cooling PWM:</label>
                    <span id="learned-cooling-pwm" style="font-family: monospace; font-weight: bold; margin-right: 20px;">--</span>
                    <span id="learned-cooling-samples" style="color: #666; font-size: 0.9em;">(0 samples)</span>
                </div>
                <div class="control-group" style="margin-top: 10px;">
                    <label for="learning-rate-input">Learning Rate:</label>
                    <input type="number" id="learning-rate-input" step="0.01" min="0.001" max="0.5" value="0.05">
                    
                    <label for="learning-error-thresh-input" style="margin-left: 20px;">Error Threshold (°C):</label>
                    <input type="number" id="learning-error-thresh-input" step="0.01" min="0.05" max="1.0" value="0.15">
                    
                    <label for="learning-rate-thresh-input" style="margin-left: 20px;">Rate Threshold (°C/s):</label>
                    <input type="number" id="learning-rate-thresh-input" step="0.001" min="0.0001" max="0.1" value="0.005">
                    
                    <label for="learning-min-time-input" style="margin-left: 20px;">Min Stable Time (s):</label>
                    <input type="number" id="learning-min-time-input" step="5" min="5" max="300" value="30">
                    
                    <button onclick="updateLearningParams()" style="margin-left: 20px;">Update Learning Params</button>
                </div>
                <div class="control-hint" style="margin-top: 10px;">Learning only occurs when temp is stable (small error & low rate) for the minimum time. Higher learning rate = faster adaptation but more noise.</div>
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
                            label: 'PWM Output',
                            data: [],
                            borderColor: '#4ecdc4',
                            backgroundColor: 'rgba(78,205,196,0.1)',
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y',
                        },
                        {
                            label: 'PI Output',
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

            // Only update select/input values if user is not actively editing them
            const modeSelectHasFocus = document.activeElement === modeSelect;
            const pwmInputHasFocus = document.activeElement === pwmInput;
            const controlsAreEnabled = !modeSelect.disabled && !pwmInput.disabled;

            // If controls are enabled (unlocked), preserve whatever the user has entered
            // Only overwrite from manualStatus if controls are disabled OR inputs are empty
            if (!modeSelectHasFocus) {
                const currentValue = modeSelect.value;
                const shouldPreserveInput = controlsAreEnabled && currentValue && currentValue !== '';
                
                if (!shouldPreserveInput) {
                    const options = [...modeSelect.options].map(opt => opt.value);
                    let selectValue = manualStatus.requestedDirection ? manualStatus.requestedDirection.toUpperCase() : 'OFF';
                    if (!options.includes(selectValue)) {
                        const fallback = manualStatus.direction ? manualStatus.direction.toUpperCase() : 'OFF';
                        selectValue = options.includes(fallback) ? fallback : 'OFF';
                    }
                    modeSelect.value = options.includes(selectValue) ? selectValue : 'OFF';
                }
            }

            if (!pwmInputHasFocus) {
                const currentValue = pwmInput.value;
                const shouldPreserveInput = controlsAreEnabled && currentValue && currentValue !== '' && currentValue !== '0.000';
                
                if (!shouldPreserveInput) {
                    const pwmValue = typeof manualStatus.pwm === 'number' ? manualStatus.pwm : 0;
                    pwmInput.value = pwmValue.toFixed(3);
                }
            }
        }

        async function loadManualStatus() {
            try {
                const response = await fetch('/api/manual/status', { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const data = await response.json();
                
                // Check if user is actively editing the manual controls
                const modeSelect = document.getElementById('manual-mode-select');
                const pwmInput = document.getElementById('manual-pwm-input');
                const userIsEditing = (document.activeElement === modeSelect) || (document.activeElement === pwmInput);
                const controlsAreEnabled = !modeSelect?.disabled && !pwmInput?.disabled;
                
                // Always update lock/enable status (critical for safety)
                manualStatus.locked = Boolean(data.locked);
                manualStatus.enabled = Boolean(data.enabled);
                
                // Only update direction/pwm from server if:
                // 1. User is NOT actively editing, AND
                // 2. Either the controls are disabled (locked) OR manual override is actually enabled
                // This preserves user's draft input when they're preparing to apply changes
                if (!userIsEditing && (!controlsAreEnabled || manualStatus.enabled)) {
                    manualStatus.direction = data.direction || 'OFF';
                    manualStatus.pwm = typeof data.pwm === 'number' ? data.pwm : 0;
                    manualStatus.requestedDirection = data.requested_direction || data.direction || 'OFF';
                }
                
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

                const piOutput = typeof data.pwm_feedback === 'number' ? data.pwm_feedback : totalPwm;
                document.getElementById('pwm-feedback').innerHTML = `${piOutput.toFixed(3)}<span class="unit"></span>`;

                const tempRateElement = document.getElementById('temp-rate');
                const rateVal = typeof data.temp_rate === 'number' ? data.temp_rate : null;
                tempRateElement.innerHTML = rateVal !== null ? `${rateVal.toFixed(3)}<span class="unit">°C/s</span>` : '--';

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

                // Update lock/enable status (critical for safety)
                if (typeof data.manual_override_locked === 'boolean') {
                    manualStatus.locked = data.manual_override_locked;
                }
                if (typeof data.manual_override === 'boolean') {
                    manualStatus.enabled = data.manual_override;
                }
                
                // Don't update direction/pwm from periodic status updates - only from explicit user actions
                // This prevents the server's stale/default values from overwriting the user's input
                // The inputs will only be updated when:
                // - User explicitly clicks Apply/Enable/Disable (those handlers update manualStatus)
                // - loadManualStatus() runs (which also preserves user input during editing)
                
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
            const piPwm = typeof data.pwm_feedback === 'number' ? data.pwm_feedback : totalPwm;

            controlChart.data.datasets[0].data.push({
                x: timestamp,
                y: totalPwm
            });

            controlChart.data.datasets[1].data.push({
                x: timestamp,
                y: piPwm
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
            
            controlChart.data.datasets[2].data.push({
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
            controlChart.data.datasets.forEach(dataset => {
                if (dataset.data.length > MAX_DATA_POINTS) {
                    dataset.data.shift();
                }
            });
            
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
                    controlChart.data.datasets.forEach(dataset => {
                        dataset.data = [];
                    });
                    
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
                        const historicPi = typeof point.pwm_feedback === 'number' ? point.pwm_feedback : totalHistoricPwm;

                        controlChart.data.datasets[0].data.push({
                            x: timestamp,
                            y: totalHistoricPwm
                        });

                        controlChart.data.datasets[1].data.push({
                            x: timestamp,
                            y: historicPi
                        });
                        
                        let modeValue = 0;
                        if (point.mode === 'HEATING') modeValue = 1;
                        else if (point.mode === 'COOLING') modeValue = -1;
                        else if (point.mode === 'MANUAL') {
                            if (point.manual_direction === 'HIGH') modeValue = 1;
                            else if (point.manual_direction === 'LOW') modeValue = -1;
                            else modeValue = 0;
                        }
                        
                        controlChart.data.datasets[2].data.push({
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
        
        // Apply temperature calibration offsets
        async function applyOffsets() {
            const objOffset = parseFloat(document.getElementById('obj-offset-input').value) || 0;
            const ambOffset = parseFloat(document.getElementById('amb-offset-input').value) || 0;
            
            try {
                const response = await fetch(
                    `/api/params?objective_temp_offset=${objOffset}&ambient_temp_offset=${ambOffset}`, 
                    { cache: 'no-store' }
                );
                const data = await response.json();
                
                if (response.ok) {
                    alert(`Temperature offsets applied:\nObjective: ${objOffset.toFixed(3)}°C\nAmbient: ${ambOffset.toFixed(3)}°C`);
                } else {
                    alert('Failed to apply temperature offsets');
                }
            } catch (error) {
                console.error('Error applying offsets:', error);
                alert('Failed to apply temperature offsets');
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
            const objOffset = parseFloat(document.getElementById('obj-offset-input').value) || 0;
            const ambOffset = parseFloat(document.getElementById('amb-offset-input').value) || 0;
            
            if (isNaN(kp) || isNaN(ki) || isNaN(interval) || isNaN(deadband) || isNaN(modeDelay) || isNaN(maWindow)) {
                alert('Invalid parameter values');
                return;
            }
            
            try {
                const heatingCap = parseFloat(document.getElementById('heating-cap-input')?.value || '');
                const coolingCap = parseFloat(document.getElementById('cooling-cap-input')?.value || '');
                const kpHeating = parseFloat(document.getElementById('kp-heating-input')?.value || '');
                const kpCooling = parseFloat(document.getElementById('kp-cooling-input')?.value || '');
                const kiHeating = parseFloat(document.getElementById('ki-heating-input')?.value || '');
                const kiCooling = parseFloat(document.getElementById('ki-cooling-input')?.value || '');

                const queryParts = [
                    `kp=${kp}`,
                    `ki=${ki}`,
                    `control_interval=${interval}`,
                    `deadband=${deadband}`,
                    `mode_switch_delay=${modeDelay}`,
                    `ma_window=${maWindow}`,
                    `invert_direction=${invertDir}`,
                    `objective_temp_offset=${objOffset}`,
                    `ambient_temp_offset=${ambOffset}`,
                ];
                if (!isNaN(heatingCap)) queryParts.push(`heating_pwm_cap=${heatingCap}`);
                if (!isNaN(coolingCap)) queryParts.push(`cooling_pwm_cap=${coolingCap}`);
                if (!isNaN(kpHeating)) queryParts.push(`kp_heating=${kpHeating}`);
                if (!isNaN(kpCooling)) queryParts.push(`kp_cooling=${kpCooling}`);
                if (!isNaN(kiHeating)) queryParts.push(`ki_heating=${kiHeating}`);
                if (!isNaN(kiCooling)) queryParts.push(`ki_cooling=${kiCooling}`);
                const response = await fetch(`/api/params?${queryParts.join('&')}`, { cache: 'no-store' });
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
                if (typeof data.cooling_pwm_cap === 'number') {
                    const el = document.getElementById('cooling-cap-input');
                    if (el) el.value = data.cooling_pwm_cap.toFixed(3);
                }

                if (typeof data.kp_heating === 'number') document.getElementById('kp-heating-input').value = String(data.kp_heating);
                if (typeof data.kp_cooling === 'number') document.getElementById('kp-cooling-input').value = String(data.kp_cooling);
                if (typeof data.ki_heating === 'number') document.getElementById('ki-heating-input').value = String(data.ki_heating);
                if (typeof data.ki_cooling === 'number') document.getElementById('ki-cooling-input').value = String(data.ki_cooling);
                
                // Load calibration offsets
                if (typeof data.objective_temp_offset === 'number') {
                    document.getElementById('obj-offset-input').value = data.objective_temp_offset.toFixed(3);
                }
                if (typeof data.ambient_temp_offset === 'number') {
                    document.getElementById('amb-offset-input').value = data.ambient_temp_offset.toFixed(3);
                }

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
        
        // Load adaptive feedforward status
        async function loadAdaptiveStatus() {
            try {
                const response = await fetch('/api/adaptive/status', { cache: 'no-store' });
                const data = await response.json();
                
                const toggleBtn = document.getElementById('adaptive-toggle-btn');
                const statusSpan = document.getElementById('adaptive-status');
                
                if (data.enabled) {
                    toggleBtn.textContent = 'Disable Learning';
                    statusSpan.textContent = 'ENABLED';
                    statusSpan.style.color = '#27ae60';
                } else {
                    toggleBtn.textContent = 'Enable Learning';
                    statusSpan.textContent = 'DISABLED';
                    statusSpan.style.color = '#c0392b';
                }
                
                // Update learned baselines
                document.getElementById('learned-heating-pwm').textContent = data.learned_baseline_heating.toFixed(4);
                document.getElementById('learned-heating-samples').textContent = `(${data.samples_heating} samples)`;
                document.getElementById('learned-cooling-pwm').textContent = data.learned_baseline_cooling.toFixed(4);
                document.getElementById('learned-cooling-samples').textContent = `(${data.samples_cooling} samples)`;
                
                // Update input fields
                document.getElementById('learning-rate-input').value = data.learning_rate;
                document.getElementById('learning-error-thresh-input').value = data.learning_error_threshold;
                document.getElementById('learning-rate-thresh-input').value = data.learning_rate_threshold;
                document.getElementById('learning-min-time-input').value = data.learning_min_time;
                
            } catch (error) {
                console.error('Error loading adaptive status:', error);
            }
        }
        
        // Toggle adaptive feedforward
        async function toggleAdaptiveFF() {
            try {
                const statusResponse = await fetch('/api/adaptive/status', { cache: 'no-store' });
                const statusData = await statusResponse.json();
                
                const endpoint = statusData.enabled ? '/api/adaptive/disable' : '/api/adaptive/enable';
                const response = await fetch(endpoint, { cache: 'no-store' });
                const data = await response.json();
                
                alert(data.message || 'Adaptive feedforward toggled');
                await loadAdaptiveStatus();
            } catch (error) {
                console.error('Error toggling adaptive FF:', error);
                alert('Failed to toggle adaptive feedforward');
            }
        }
        
        // Reset learned baselines
        async function resetLearnedBaselines() {
            if (!confirm('Reset all learned baseline values to zero?')) {
                return;
            }
            
            try {
                const response = await fetch('/api/adaptive/reset', { cache: 'no-store' });
                const data = await response.json();
                
                alert(data.message || 'Learned baselines reset');
                await loadAdaptiveStatus();
            } catch (error) {
                console.error('Error resetting baselines:', error);
                alert('Failed to reset learned baselines');
            }
        }
        
        // Update learning parameters
        async function updateLearningParams() {
            const learningRate = document.getElementById('learning-rate-input').value;
            const errorThresh = document.getElementById('learning-error-thresh-input').value;
            const rateThresh = document.getElementById('learning-rate-thresh-input').value;
            const minTime = document.getElementById('learning-min-time-input').value;
            
            try {
                const url = `/api/adaptive/params?learning_rate=${learningRate}&error_threshold=${errorThresh}&rate_threshold=${rateThresh}&min_time=${minTime}`;
                const response = await fetch(url, { cache: 'no-store' });
                const data = await response.json();
                
                alert(data.message || 'Learning parameters updated');
                await loadAdaptiveStatus();
            } catch (error) {
                console.error('Error updating learning params:', error);
                alert('Failed to update learning parameters');
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
                
                console.log('Loading adaptive feedforward status...');
                await loadAdaptiveStatus();
                
                console.log('Loading historical data...');
                await loadHistoricalData();
                
                console.log('Starting periodic updates...');
                setInterval(fetchCurrentData, UPDATE_INTERVAL);
                setInterval(loadPIDStatus, UPDATE_INTERVAL);
                setInterval(loadManualStatus, UPDATE_INTERVAL * 3);
                setInterval(loadAdaptiveStatus, UPDATE_INTERVAL * 5);
                
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
        default=0.015,
        help="Base proportional gain before per-mode tuning (default: 0.015, conservative for two-stage heating)",
    )
    parser.add_argument(
        "--ki",
        type=float,
        default=0.0030,
        help="Base integral gain before per-mode tuning (default: 0.0030, conservative for two-stage heating)",
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=0.2,
        help="Temperature deadband ±°C (default: 0.2, allows small steady-state error to minimize oscillation)",
    )
    parser.add_argument(
        "--deadband-heating",
        type=float,
        default=None,
        help="Deadband used while heating (temperature below setpoint). Defaults to 70%% of --deadband",
    )
    parser.add_argument(
        "--deadband-cooling",
        type=float,
        default=None,
        help="Deadband used while cooling (temperature above setpoint). Defaults to 70%% of --deadband",
    )
    parser.add_argument(
        "--mode-switch-delay",
        type=float,
        default=30.0,
        help="Minimum seconds between heating/cooling mode switches (default: 30.0, prevents rapid reversals)",
    )
    parser.add_argument(
        "--heating-pwm-cap",
        type=float,
        default=0.15,
        help="Cap on PWM during HEATING (<= pwm_max, default: 0.15, conservative to prevent overshoot)",
    )
    parser.add_argument(
        "--cooling-pwm-cap",
        type=float,
        default=0.15,
        help="Cap on PWM during COOLING (<= pwm_max, default: 0.15, conservative to prevent overshoot)",
    )
    parser.add_argument(
        "--ma-window",
        type=int,
        default=10,
        help="Moving average window (samples) for objective temperature. At 5Hz rate, 10 samples = 2s average (default: 10)",
    )
    parser.add_argument(
        "--ambient-feedforward-gain",
        type=float,
        default=0.015,
        help="Feedforward gain for thermal drift compensation (PWM per °C difference from ambient, default: 0.015)",
    )
    parser.add_argument(
        "--invert-direction",
        action="store_true",
        help="Invert H-bridge direction mapping (heating uses LOW, cooling uses HIGH)",
    )
    parser.add_argument(
        "--control-interval",
        type=float,
        default=0.2,
        help="Control loop interval in seconds (default: 0.2, matches Arduino sampling rate)",
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
        cooling_pwm_cap=args.cooling_pwm_cap,
        ma_window=args.ma_window,
        ambient_feedforward_gain=args.ambient_feedforward_gain,
    )
    # Apply PWM caps if specified
    if args.heating_pwm_cap is not None:
        controller.pi_controller.set_heating_pwm_cap(args.heating_pwm_cap)
    if args.cooling_pwm_cap is not None:
        controller.pi_controller.set_cooling_pwm_cap(args.cooling_pwm_cap)
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
