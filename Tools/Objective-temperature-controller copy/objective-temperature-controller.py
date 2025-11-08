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
import mimetypes
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import re
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
    near_setpoint_threshold: float = 0.4,
    overshoot_guard: float = 0.15,
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
        self.near_setpoint_threshold = max(self.overshoot_guard * 2.0, float(near_setpoint_threshold))
        default_near_cap = (
            float(pwm_near_cap)
            if pwm_near_cap is not None
            else min(self.pwm_max * 0.45, self.heating_pwm_cap * 0.9)
        )
        self.pwm_near_cap = max(self.pwm_min, min(self.pwm_max, default_near_cap))
        self.pwm_near_cap = min(self.pwm_near_cap, self.heating_pwm_cap, self.cooling_pwm_cap)
        self.overshoot_pwm_cap = min(self.pwm_near_cap, self.pwm_max * 0.25)
        self.trend_brake_threshold = max(0.0, float(trend_brake_threshold))
        self.predictive_brake_band = max(
            0.05,
            float(predictive_brake_band) if predictive_brake_band is not None else self.overshoot_guard * 0.6,
        )
        self.trend_window_seconds = 10  # Longer window for smoother trend estimation
        self.integral_bleed_rate = 0.05  # Slightly faster decay near setpoint to reduce residual overshoot
        self.min_active_output = 0.001
        self.trend_damping_gain = self.pwm_max * 1.2  # Reduced from 1.8 - less aggressive trend damping

        self.integral = 0.0
        self.last_error = 0.0
        self.last_time: Optional[float] = None
        self._last_details: Dict[str, Any] = {}
        
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
    
    def get_last_details(self) -> Dict[str, Any]:
        """Return a shallow copy of the most recent compute diagnostics."""
        with self._lock:
            return dict(self._last_details)

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
            self.pwm_near_cap = min(self.pwm_near_cap, self.heating_pwm_cap, self.cooling_pwm_cap)
            self.overshoot_pwm_cap = min(self.pwm_near_cap, self.pwm_max * 0.25)
            logger.info("Heating PWM cap set to %.3f", self.heating_pwm_cap)

    def get_heating_pwm_cap(self) -> float:
        with self._lock:
            return self.heating_pwm_cap

    def set_cooling_pwm_cap(self, cap: float) -> None:
        """Set maximum PWM allowed in COOLING mode (<= pwm_max)."""
        with self._lock:
            cap = max(0.01, min(float(cap), self.pwm_max))  # At least 0.01, no more than pwm_max
            self.cooling_pwm_cap = cap
            self.pwm_near_cap = min(self.pwm_near_cap, self.heating_pwm_cap, self.cooling_pwm_cap)
            self.overshoot_pwm_cap = min(self.pwm_near_cap, self.pwm_max * 0.25)
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

    def set_overshoot_guard(self, guard: float) -> None:
        """Set overshoot guard region (°C) controlling aggressive braking."""
        with self._lock:
            guard_value = max(0.05, min(float(guard), 1.5))
            self.overshoot_guard = guard_value
            # Ensure thresholds remain consistent
            self.near_setpoint_threshold = max(self.overshoot_guard * 2.0, self.near_setpoint_threshold)
            self.predictive_brake_band = max(0.02, min(self.predictive_brake_band, self.overshoot_guard))
            self.overshoot_pwm_cap = min(self.pwm_near_cap, self.pwm_max * 0.25)
            logger.info("Overshoot guard updated to %.3f°C", self.overshoot_guard)

    def get_overshoot_guard(self) -> float:
        with self._lock:
            return self.overshoot_guard

    def set_pwm_near_cap(self, cap: float) -> None:
        """Set PWM cap used when approaching setpoint."""
        with self._lock:
            value = max(self.pwm_min, min(float(cap), self.pwm_max))
            value = min(value, self.heating_pwm_cap, self.cooling_pwm_cap)
            self.pwm_near_cap = value
            self.overshoot_pwm_cap = min(self.pwm_near_cap, self.pwm_max * 0.25)
            logger.info("Near-setpoint PWM cap set to %.4f", self.pwm_near_cap)

    def get_pwm_near_cap(self) -> float:
        with self._lock:
            return self.pwm_near_cap

    def set_ambient_feedforward_gain(self, gain: float) -> None:
        """Set the ambient feedforward gain (PWM per °C temperature difference from ambient)."""
        with self._lock:
            self.ambient_feedforward_gain = max(0.0, float(gain))
            logger.info("Ambient feedforward gain set to %.6f", self.ambient_feedforward_gain)

    def get_ambient_feedforward_gain(self) -> float:
        """Get current ambient feedforward gain."""
        with self._lock:
            return self.ambient_feedforward_gain

    def set_predictive_brake_band(self, band: float) -> None:
        """Set the predictive brake radius near setpoint (°C)."""
        with self._lock:
            band_value = max(0.01, min(float(band), 1.0))
            self.predictive_brake_band = min(band_value, self.overshoot_guard)
            logger.info("Predictive brake band set to %.3f°C", self.predictive_brake_band)

    def get_predictive_brake_band(self) -> float:
        with self._lock:
            return self.predictive_brake_band

    def set_trend_brake_threshold(self, threshold: float) -> None:
        """Set the minimum trend magnitude (°C/s) before gain boosting engages."""
        with self._lock:
            value = max(0.0, min(float(threshold), 0.02))
            self.trend_brake_threshold = value
            logger.info("Trend brake threshold set to %.5f°C/s", self.trend_brake_threshold)

    def get_trend_brake_threshold(self) -> float:
        with self._lock:
            return self.trend_brake_threshold

    def set_integral_bleed_rate(self, rate: float) -> None:
        """Set integral bleed rate (1/s) used inside deadband."""
        with self._lock:
            value = max(0.0, min(float(rate), 1.0))
            self.integral_bleed_rate = value
            logger.info("Integral bleed rate set to %.4f 1/s", self.integral_bleed_rate)

    def get_integral_bleed_rate(self) -> float:
        with self._lock:
            return self.integral_bleed_rate

    def set_trend_damping_gain(self, gain: float) -> None:
        """Set multiplier converting effective trend into PWM brake."""
        with self._lock:
            value = max(0.0, min(float(gain), self.pwm_max * 5.0))
            self.trend_damping_gain = value
            logger.info("Trend damping gain set to %.5f", self.trend_damping_gain)

    def get_trend_damping_gain(self) -> float:
        with self._lock:
            return self.trend_damping_gain

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
    ) -> Tuple[float, str, Dict[str, Any]]:
        """Compute PWM output and heating/cooling mode with diagnostic breakdown."""

        with self._lock:
            current_time = time.time()

            # Update temperature history for trend detection
            self.temp_history.append(float(current_temp))
            self.temp_time_history.append(current_time)
            while (
                self.temp_time_history
                and current_time - self.temp_time_history[0] > self.trend_window_seconds
            ):
                self.temp_time_history.popleft()
                self.temp_history.popleft()

            # Sanitize baseline inputs (ambient feedforward)
            ambient_baseline_pwm = max(self.pwm_min, min(float(baseline_pwm), self.pwm_max))
            if ambient_baseline_pwm <= self.pwm_min + 1e-9:
                ambient_baseline_pwm = 0.0
            if ambient_baseline_pwm == 0.0 or baseline_mode not in ("HEATING", "COOLING"):
                baseline_mode = None

            # Error metrics
            error = self.setpoint - current_temp
            abs_error = abs(error)
            active_deadband = self.deadband_heating if error >= 0 else self.deadband_cooling
            within_deadband = abs_error <= active_deadband

            # Adaptive baseline preference
            preferred_mode_for_adaptive = "HEATING" if error > 0 else "COOLING"
            adaptive_baseline_pwm = self._get_adaptive_baseline(preferred_mode_for_adaptive)

            combined_baseline_pwm = max(ambient_baseline_pwm, adaptive_baseline_pwm)
            if adaptive_baseline_pwm > ambient_baseline_pwm:
                baseline_mode = preferred_mode_for_adaptive if adaptive_baseline_pwm > 0 else None

            # Reject feed-forward opposing desired direction
            if baseline_mode == "HEATING" and error < -self.deadband_cooling:
                logger.debug("Discarding heating feed-forward (err=%.3f°C requires cooling)", error)
                baseline_mode = None
                combined_baseline_pwm = 0.0
            elif baseline_mode == "COOLING" and error > self.deadband_heating:
                logger.debug("Discarding cooling feed-forward (err=%.3f°C requires heating)", error)
                baseline_mode = None
                combined_baseline_pwm = 0.0

            preferred_mode = (
                baseline_mode if baseline_mode is not None else ("HEATING" if error > 0 else "COOLING")
            )

            details: Dict[str, Any] = {
                "timestamp": current_time,
                "error": error,
                "abs_error": abs_error,
                "deadband": active_deadband,
                "within_deadband": within_deadband,
                "ambient_ff_pwm": ambient_baseline_pwm,
                "adaptive_ff_pwm": adaptive_baseline_pwm,
                "combined_baseline_pwm": combined_baseline_pwm,
                "baseline_mode": baseline_mode,
                "preferred_mode": preferred_mode,
                "mode": self.current_mode,
                "p_term": 0.0,
                "i_term": 0.0,
                "feedback_pwm": 0.0,
                "pwm_output": 0.0,
                "pwm_limit": self.pwm_max,
                "trend": None,
                "trend_effective": None,
                "approaching": False,
                "moving_away": False,
                "kp_effective": 0.0,
                "ki_effective": 0.0,
                "kp_scale": 1.0,
                "ki_scale": 1.0,
                "integral_state": self.integral,
                "notes": "",
            }

            # ------------------------------------------------------------------
            # Deadband handling
            # ------------------------------------------------------------------
            if within_deadband:
                decay_dt = 0.0 if self.last_time is None else max(0.0, current_time - self.last_time)
                if decay_dt > 0.0 and self.integral > 0.0:
                    bleed = self.integral_bleed_rate * decay_dt
                    self.integral = max(0.0, self.integral - bleed)

                self.last_time = current_time
                self.last_error = error
                details["integral_state"] = self.integral

                if combined_baseline_pwm > 0.0 and baseline_mode in ("HEATING", "COOLING"):
                    target_mode = baseline_mode
                    if self.current_mode is not None and target_mode != self.current_mode:
                        if self.last_mode_switch_time is not None:
                            elapsed = current_time - self.last_mode_switch_time
                            if elapsed < self.mode_switch_delay:
                                logger.debug(
                                    "Mode switch delayed by %.1fs (limit %.1fs)", elapsed, self.mode_switch_delay
                                )
                                held_mode = (
                                    self.current_mode
                                    if self.current_mode in ("HEATING", "COOLING")
                                    else "OFF"
                                )
                                details.update(
                                    {
                                        "mode": held_mode,
                                        "pwm_output": 0.0,
                                        "pwm_limit": self._pwm_limit_for_mode(held_mode)
                                        if held_mode != "OFF"
                                        else self.pwm_max,
                                        "notes": "Mode switch delayed by safety timer",
                                    }
                                )
                                self._last_details = details
                                return 0.0, held_mode, details

                    if target_mode != self.current_mode:
                        logger.info("Mode switching: %s → %s", self.current_mode, target_mode)
                        self.current_mode = target_mode
                        self.last_mode_switch_time = current_time
                        self.integral = 0.0

                    if self.current_mode not in ("HEATING", "COOLING"):
                        self.current_mode = "OFF"
                        details.update({"mode": "OFF", "pwm_output": 0.0, "notes": "Invalid mode in deadband"})
                        self._last_details = details
                        return 0.0, "OFF", details

                    pwm_limit = self._pwm_limit_for_mode(self.current_mode)
                    pwm_value = min(combined_baseline_pwm, pwm_limit)
                    details.update(
                        {
                            "mode": self.current_mode,
                            "pwm_limit": pwm_limit,
                            "pwm_output": pwm_value,
                            "feedback_pwm": 0.0,
                            "integral_state": self.integral,
                            "notes": "Holding feed-forward within deadband",
                        }
                    )
                    self._last_details = details
                    return pwm_value, self.current_mode, details

                self.current_mode = "OFF"
                details.update({"mode": "OFF", "pwm_output": 0.0, "notes": "Within deadband"})
                self._last_details = details
                return 0.0, "OFF", details

            # ------------------------------------------------------------------
            # Determine mode and timing outside deadband
            # ------------------------------------------------------------------
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
                            combined_baseline_pwm = 0.0
                        desired_mode = self.current_mode
                        details["notes"] = "Mode switch delayed by safety timer"

            if desired_mode != self.current_mode:
                logger.info("Mode switching: %s → %s", self.current_mode, desired_mode)
                self.current_mode = desired_mode
                self.last_mode_switch_time = current_time
                self.integral = 0.0

            if self.current_mode not in ("HEATING", "COOLING"):
                logger.error("Invalid mode after update: %s", self.current_mode)
                self.current_mode = "OFF"
                self.last_error = error
                details.update({"mode": "OFF", "pwm_output": 0.0, "notes": "Invalid active mode"})
                self._last_details = details
                return 0.0, "OFF", details

            if self.current_mode == "HEATING":
                current_kp = self.kp_heating
                current_ki = self.ki_heating
            else:
                current_kp = self.kp_cooling
                current_ki = self.ki_cooling

            pwm_limit = self._pwm_limit_for_mode(self.current_mode)
            combined_baseline_pwm = min(combined_baseline_pwm, pwm_limit)
            details.update(
                {
                    "mode": self.current_mode,
                    "pwm_limit": pwm_limit,
                    "combined_baseline_pwm": combined_baseline_pwm,
                }
            )

            # ------------------------------------------------------------------
            # Dynamic damping near setpoint
            # ------------------------------------------------------------------
            trend = self._calculate_temp_trend()
            effective_trend: Optional[float] = None
            approaching = False
            moving_away = False
            if trend is not None:
                direction = 1.0 if self.current_mode == "HEATING" else -1.0
                effective_trend = trend * direction
                approaching = effective_trend > self.trend_brake_threshold
                moving_away = effective_trend < -self.trend_brake_threshold

            effective_error = max(0.0, abs_error - active_deadband)
            if effective_error <= 0.0 and dt > 0.0 and self.integral > 0.0:
                if not moving_away:
                    bleed = self.integral_bleed_rate * dt
                    self.integral = max(0.0, self.integral - bleed)
                effective_error = 0.0

            kp_scale = 1.0
            ki_scale = 1.0

            if moving_away and effective_error > 0.0:
                trend_boost = min(3.0, 1.5 + abs(effective_trend or 0.0) / 0.005)
                kp_scale *= trend_boost
                ki_scale *= trend_boost
                details["notes"] = "Boosting gains to counter adverse trend"
            elif abs_error <= self.overshoot_guard and approaching:
                guard_fraction = max(0.0, abs_error / self.overshoot_guard)
                guard_scale = max(0.3, guard_fraction)
                kp_scale *= guard_scale
                ki_scale *= guard_scale
                pwm_limit = min(
                    pwm_limit,
                    max(
                        self.pwm_min,
                        self.overshoot_pwm_cap * max(0.4, guard_scale),
                    ),
                )
                details["notes"] = "Overshoot guard active"

                if abs_error <= self.predictive_brake_band and (effective_trend or 0.0) > 0.008:
                    kp_scale *= 0.15
                    ki_scale *= 0.15
                    pwm_limit = self.min_active_output * 5
                    details["notes"] = "Predictive brake engaged"
            elif self.near_setpoint_threshold > active_deadband:
                slow_span = max(1e-6, self.near_setpoint_threshold - active_deadband)
                zone_fraction = min(1.0, effective_error / slow_span)
                if zone_fraction < 1.0:
                    zone_scale = max(0.20, zone_fraction ** 1.5)
                    kp_scale *= zone_scale
                    ki_scale *= zone_scale
                    pwm_floor = max(self.pwm_near_cap, combined_baseline_pwm)
                    pwm_limit = min(
                        pwm_limit,
                        max(
                            pwm_floor,
                            pwm_floor + (pwm_limit - pwm_floor) * zone_fraction,
                        ),
                    )
                    if approaching:
                        self.integral *= max(0.6, zone_fraction)
                    details["notes"] = "Near-setpoint damping"

            current_kp *= kp_scale
            current_ki *= ki_scale

            # ------------------------------------------------------------------
            # PI control with anti-windup
            # ------------------------------------------------------------------
            integral_candidate = self.integral
            if dt > 0.0 and current_ki > 0.0 and effective_error > 0.0:
                integral_gain_boost = 1.5 if moving_away else 1.0
                integral_candidate += effective_error * dt * integral_gain_boost

            p_term = current_kp * effective_error
            i_term_candidate = current_ki * integral_candidate if current_ki > 0.0 else 0.0
            raw_feedback = p_term + i_term_candidate
            max_feedback = max(0.0, pwm_limit - combined_baseline_pwm)

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

            if approaching and not moving_away and feedback > 0.0 and trend is not None:
                brake = (effective_trend or 0.0) * self.trend_damping_gain
                if brake > 0.0:
                    feedback = max(0.0, feedback - brake)
                    if current_ki > 0.0:
                        allowed_i = max(0.0, feedback - p_term)
                        self.integral = allowed_i / current_ki
                    else:
                        self.integral = 0.0
                    details["notes"] = "Trend brake reducing output"

            pwm_output = combined_baseline_pwm + feedback
            pwm_value = max(self.pwm_min, min(pwm_output, pwm_limit))

            if pwm_value <= self.min_active_output:
                if moving_away:
                    pwm_value = max(self.min_active_output * 2, pwm_value)
                    details["notes"] = "Holding minimal PWM to oppose drift"
                else:
                    pwm_value = 0.0
                    self.integral = 0.0
                    details["notes"] = "Output zeroed - stable"

            self.last_error = error

            # Update adaptive learning
            self._update_adaptive_learning(
                current_time=current_time,
                error=error,
                temp_trend=trend,
                pwm_value=pwm_value,
                mode=self.current_mode,
            )

            p_output = p_term
            i_output = current_ki * self.integral if current_ki > 0.0 else 0.0
            details.update(
                {
                    "mode": self.current_mode,
                    "p_term": p_output,
                    "i_term": i_output,
                    "feedback_pwm": feedback,
                    "pwm_output": pwm_value,
                    "pwm_limit": pwm_limit,
                    "trend": trend,
                    "trend_effective": effective_trend,
                    "approaching": approaching,
                    "moving_away": moving_away,
                    "kp_effective": current_kp,
                    "ki_effective": current_ki,
                    "kp_scale": kp_scale,
                    "ki_scale": ki_scale,
                    "integral_state": self.integral,
                    "combined_baseline_pwm": combined_baseline_pwm,
                }
            )

            logger.debug(
                "PI+: err=%.3f°C (deadband %.3f°C), trend=%.4f°C/s, P=%.4f, I=%.4f, FF_amb=%.4f, FF_adapt=%.4f, pwm=%.4f, mode=%s",
                error,
                active_deadband,
                trend if trend is not None else 0.0,
                p_output,
                i_output,
                ambient_baseline_pwm,
                adaptive_baseline_pwm,
                pwm_value,
                self.current_mode,
            )

            self._last_details = details
            return pwm_value, self.current_mode, details


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
            "ambient_ff_pwm",
            "adaptive_ff_pwm",
            "combined_baseline_pwm",
            "p_term",
            "i_term",
            "feedback_component_pwm",
            "pwm_limit",
            "trend",
            "trend_effective",
            "kp_effective",
            "ki_effective",
            "kp_scale",
            "ki_scale",
            "integral_state",
            "controller_notes",
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
        controller_details: Optional[Dict[str, Any]] = None,
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
                "ambient_ff_pwm": "",
                "adaptive_ff_pwm": "",
                "combined_baseline_pwm": "",
                "p_term": "",
                "i_term": "",
                "feedback_component_pwm": "",
                "pwm_limit": "",
                "trend": "",
                "trend_effective": "",
                "kp_effective": "",
                "ki_effective": "",
                "kp_scale": "",
                "ki_scale": "",
                "integral_state": "",
                "controller_notes": "",
            }

            if controller_details:
                row["ambient_ff_pwm"] = f"{controller_details.get('ambient_ff_pwm', 0.0):.4f}"
                row["adaptive_ff_pwm"] = f"{controller_details.get('adaptive_ff_pwm', 0.0):.4f}"
                row["combined_baseline_pwm"] = f"{controller_details.get('combined_baseline_pwm', 0.0):.4f}"
                row["p_term"] = f"{controller_details.get('p_term', 0.0):.5f}"
                row["i_term"] = f"{controller_details.get('i_term', 0.0):.5f}"
                row["feedback_component_pwm"] = f"{controller_details.get('feedback_pwm', 0.0):.4f}"
                row["pwm_limit"] = f"{controller_details.get('pwm_limit', 0.0):.4f}"
                trend_val = controller_details.get('trend')
                row["trend"] = f"{trend_val:.6f}" if isinstance(trend_val, (int, float)) and trend_val is not None else ""
                eff_trend_val = controller_details.get('trend_effective')
                row["trend_effective"] = (
                    f"{eff_trend_val:.6f}" if isinstance(eff_trend_val, (int, float)) and eff_trend_val is not None else ""
                )
                row["kp_effective"] = f"{controller_details.get('kp_effective', 0.0):.5f}"
                row["ki_effective"] = f"{controller_details.get('ki_effective', 0.0):.5f}"
                row["kp_scale"] = f"{controller_details.get('kp_scale', 0.0):.3f}"
                row["ki_scale"] = f"{controller_details.get('ki_scale', 0.0):.3f}"
                row["integral_state"] = f"{controller_details.get('integral_state', 0.0):.6f}"
                row["controller_notes"] = str(controller_details.get('notes', ''))
            
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

    _PRESET_PARAM_KEYS = (
        "setpoint",
        "kp",
        "ki",
        "kp_heating",
        "kp_cooling",
        "ki_heating",
        "ki_cooling",
        "deadband",
        "deadband_heating",
        "deadband_cooling",
        "mode_switch_delay",
        "heating_pwm_cap",
        "cooling_pwm_cap",
        "near_setpoint_threshold",
        "overshoot_guard",
        "pwm_near_cap",
        "predictive_brake_band",
        "trend_brake_threshold",
        "integral_bleed_rate",
        "trend_damping_gain",
        "ambient_feedforward_gain",
        "objective_temp_offset",
        "ambient_temp_offset",
        "control_interval",
        "ma_window",
        "invert_direction",
    )
    _PRESET_FILE_EXTENSION = ".json"
    
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

        # Preset storage for saving/loading controller configurations
        self.presets_dir = self.log_dir / "controller_presets"
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self._presets_lock = threading.Lock()
        
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

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        """Convert a loose value into a boolean."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
        return bool(value)

    def _normalize_preset_name(self, name: str) -> Tuple[str, str]:
        """Return filesystem-safe preset name and display name."""
        display_name = (name or "").strip()
        if not display_name:
            raise ValueError("Preset name cannot be empty")

        normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", display_name)
        normalized = normalized.strip("_")
        if not normalized:
            normalized = re.sub(r"[^A-Za-z0-9]+", "", display_name)
        normalized = normalized[:64]
        if not normalized:
            raise ValueError("Preset name must include at least one alphanumeric character")

        return normalized, display_name

    def _preset_path(self, normalized_name: str) -> Path:
        """Create a preset path for a normalized name."""
        filename = f"{normalized_name}{self._PRESET_FILE_EXTENSION}"
        return self.presets_dir / filename

    def list_presets(self) -> List[Dict[str, Any]]:
        """Return available controller presets (metadata only)."""
        presets: List[Dict[str, Any]] = []
        if not self.presets_dir.exists():
            return presets

        with self._presets_lock:
            preset_files = sorted(self.presets_dir.glob(f"*{self._PRESET_FILE_EXTENSION}"))

        for preset_path in preset_files:
            try:
                with open(preset_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                name = data.get("name") or preset_path.stem
                display = data.get("display_name") or name
                presets.append(
                    {
                        "name": name,
                        "display_name": display,
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "path": str(preset_path),
                    }
                )
            except Exception as exc:
                logger.warning("Failed to load preset %s: %s", preset_path, exc)

        presets.sort(key=lambda item: (item.get("display_name") or item["name"]).lower())
        return presets

    def save_preset(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Persist the current controller configuration under a preset name."""
        normalized, display = self._normalize_preset_name(name)
        preset_path = self._preset_path(normalized)

        current_params = self.get_controller_params()
        preset_params = {key: current_params.get(key) for key in self._PRESET_PARAM_KEYS if key in current_params}
        timestamp = datetime.now(timezone.utc).isoformat()

        existing_created = timestamp
        with self._presets_lock:
            if preset_path.exists():
                try:
                    with open(preset_path, "r", encoding="utf-8") as handle:
                        existing = json.load(handle)
                    existing_created = existing.get("created_at", existing_created)
                except Exception as exc:
                    logger.warning("Failed to read existing preset '%s' before overwrite: %s", preset_path, exc)

            record: Dict[str, Any] = {
                "name": normalized,
                "display_name": display,
                "created_at": existing_created,
                "updated_at": timestamp,
                "params": preset_params,
            }
            if metadata is not None:
                record["metadata"] = metadata

            with open(preset_path, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, sort_keys=True)

        logger.info("Saved controller preset '%s' (%s)", display, preset_path.name)

        result = dict(record)
        result["path"] = str(preset_path)
        return result

    def load_preset(self, name: str, apply: bool = True) -> Dict[str, Any]:
        """Load a preset by name and optionally apply it to the controller."""
        normalized, display = self._normalize_preset_name(name)
        preset_path = self._preset_path(normalized)

        with self._presets_lock:
            if not preset_path.exists():
                raise FileNotFoundError(f"Preset '{display}' not found")
            with open(preset_path, "r", encoding="utf-8") as handle:
                record = json.load(handle)

        params = record.get("params") or {}
        applied: Dict[str, Any] = {}
        if apply and params:
            applied = self._apply_preset_params(params)

        response = {
            "name": record.get("name", normalized),
            "display_name": record.get("display_name", display),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "params": params,
            "metadata": record.get("metadata"),
            "path": str(preset_path),
            "applied": applied,
        }
        return response

    def delete_preset(self, name: str) -> bool:
        """Delete a saved preset. Returns True if it existed."""
        normalized, _ = self._normalize_preset_name(name)
        preset_path = self._preset_path(normalized)

        with self._presets_lock:
            if not preset_path.exists():
                return False
            preset_path.unlink()

        logger.info("Deleted controller preset '%s'", name)
        return True

    def _apply_preset_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Apply preset parameters using the appropriate setters."""
        applied: Dict[str, Any] = {}

        for key in self._PRESET_PARAM_KEYS:
            if key not in params:
                continue

            value = params[key]
            try:
                if key == "setpoint":
                    self.set_setpoint(float(value))
                    applied[key] = self.get_setpoint()
                elif key == "kp":
                    self.pi_controller.set_kp(float(value))
                    applied[key] = self.pi_controller.get_kp()
                elif key == "ki":
                    self.pi_controller.set_ki(float(value))
                    applied[key] = self.pi_controller.get_ki()
                elif key == "kp_heating":
                    self.pi_controller.set_kp_heating(float(value))
                    applied[key] = self.pi_controller.get_kp_heating()
                elif key == "kp_cooling":
                    self.pi_controller.set_kp_cooling(float(value))
                    applied[key] = self.pi_controller.get_kp_cooling()
                elif key == "ki_heating":
                    self.pi_controller.set_ki_heating(float(value))
                    applied[key] = self.pi_controller.get_ki_heating()
                elif key == "ki_cooling":
                    self.pi_controller.set_ki_cooling(float(value))
                    applied[key] = self.pi_controller.get_ki_cooling()
                elif key == "deadband":
                    self.pi_controller.set_deadband(float(value))
                    applied[key] = self.pi_controller.get_deadband()
                elif key == "deadband_heating":
                    self.pi_controller.set_deadband_heating(float(value))
                    applied[key] = self.pi_controller.get_deadband_heating()
                elif key == "deadband_cooling":
                    self.pi_controller.set_deadband_cooling(float(value))
                    applied[key] = self.pi_controller.get_deadband_cooling()
                elif key == "mode_switch_delay":
                    self.pi_controller.set_mode_switch_delay(float(value))
                    applied[key] = self.pi_controller.get_mode_switch_delay()
                elif key == "heating_pwm_cap":
                    self.pi_controller.set_heating_pwm_cap(float(value))
                    applied[key] = self.pi_controller.get_heating_pwm_cap()
                elif key == "cooling_pwm_cap":
                    self.pi_controller.set_cooling_pwm_cap(float(value))
                    applied[key] = self.pi_controller.get_cooling_pwm_cap()
                elif key == "near_setpoint_threshold":
                    self.pi_controller.set_near_setpoint_threshold(float(value))
                    applied[key] = self.pi_controller.get_near_setpoint_threshold()
                elif key == "overshoot_guard":
                    self.pi_controller.set_overshoot_guard(float(value))
                    applied[key] = self.pi_controller.get_overshoot_guard()
                elif key == "pwm_near_cap":
                    self.pi_controller.set_pwm_near_cap(float(value))
                    applied[key] = self.pi_controller.get_pwm_near_cap()
                elif key == "predictive_brake_band":
                    self.pi_controller.set_predictive_brake_band(float(value))
                    applied[key] = self.pi_controller.get_predictive_brake_band()
                elif key == "trend_brake_threshold":
                    self.pi_controller.set_trend_brake_threshold(float(value))
                    applied[key] = self.pi_controller.get_trend_brake_threshold()
                elif key == "integral_bleed_rate":
                    self.pi_controller.set_integral_bleed_rate(float(value))
                    applied[key] = self.pi_controller.get_integral_bleed_rate()
                elif key == "trend_damping_gain":
                    self.pi_controller.set_trend_damping_gain(float(value))
                    applied[key] = self.pi_controller.get_trend_damping_gain()
                elif key == "ambient_feedforward_gain":
                    self.pi_controller.set_ambient_feedforward_gain(float(value))
                    applied[key] = self.pi_controller.get_ambient_feedforward_gain()
                elif key == "objective_temp_offset":
                    self.pi_controller.set_objective_temp_offset(float(value))
                    applied[key] = self.pi_controller.get_objective_temp_offset()
                elif key == "ambient_temp_offset":
                    self.pi_controller.set_ambient_temp_offset(float(value))
                    applied[key] = self.pi_controller.get_ambient_temp_offset()
                elif key == "control_interval":
                    self.set_control_interval(float(value))
                    applied[key] = self.get_control_interval()
                elif key == "ma_window":
                    self.set_ma_window(int(value))
                    applied[key] = self.get_ma_window()
                elif key == "invert_direction":
                    invert_value = self._coerce_bool(value)
                    self.arduino.set_invert_direction(invert_value)
                    applied[key] = self.arduino.get_invert_direction()
            except Exception as exc:
                logger.warning("Failed to apply preset parameter '%s': %s", key, exc)

        return applied

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
                compute_details: Optional[Dict[str, Any]] = None
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
                        
                        pwm_total, mode, compute_details = self.pi_controller.compute(
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

                if compute_details is not None:
                    pwm_feedback = float(compute_details.get("feedback_pwm", pwm_total))
                else:
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
                    controller_details=compute_details,
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
                    "pi_details": compute_details,
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
            "overshoot_guard": self.pi_controller.get_overshoot_guard(),
            "pwm_near_cap": self.pi_controller.get_pwm_near_cap(),
            "predictive_brake_band": self.pi_controller.get_predictive_brake_band(),
            "trend_brake_threshold": self.pi_controller.get_trend_brake_threshold(),
            "integral_bleed_rate": self.pi_controller.get_integral_bleed_rate(),
            "trend_damping_gain": self.pi_controller.get_trend_damping_gain(),
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
            "presets": self.list_presets(),
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
                    "pi_details": None,
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
                                "ambient_ff_pwm": float(row["ambient_ff_pwm"]) if row.get("ambient_ff_pwm") else None,
                                "adaptive_ff_pwm": float(row["adaptive_ff_pwm"]) if row.get("adaptive_ff_pwm") else None,
                                "combined_baseline_pwm": float(row["combined_baseline_pwm"]) if row.get("combined_baseline_pwm") else None,
                                "p_term": float(row["p_term"]) if row.get("p_term") else None,
                                "i_term": float(row["i_term"]) if row.get("i_term") else None,
                                "feedback_component_pwm": float(row["feedback_component_pwm"]) if row.get("feedback_component_pwm") else None,
                                "pwm_limit": float(row["pwm_limit"]) if row.get("pwm_limit") else None,
                                "trend": float(row["trend"]) if row.get("trend") else None,
                                "trend_effective": float(row["trend_effective"]) if row.get("trend_effective") else None,
                                "kp_effective": float(row["kp_effective"]) if row.get("kp_effective") else None,
                                "ki_effective": float(row["ki_effective"]) if row.get("ki_effective") else None,
                                "kp_scale": float(row["kp_scale"]) if row.get("kp_scale") else None,
                                "ki_scale": float(row["ki_scale"]) if row.get("ki_scale") else None,
                                "integral_state": float(row["integral_state"]) if row.get("integral_state") else None,
                                "controller_notes": row.get("controller_notes") or None,
                            }
                            if data_point["ambient_ff_pwm"] is not None or data_point["adaptive_ff_pwm"] is not None:
                                data_point["pi_details"] = {
                                    "ambient_ff_pwm": data_point["ambient_ff_pwm"],
                                    "adaptive_ff_pwm": data_point["adaptive_ff_pwm"],
                                    "combined_baseline_pwm": data_point["combined_baseline_pwm"],
                                    "p_term": data_point["p_term"],
                                    "i_term": data_point["i_term"],
                                    "feedback_pwm": data_point["feedback_component_pwm"],
                                    "pwm_limit": data_point["pwm_limit"],
                                    "trend": data_point["trend"],
                                    "trend_effective": data_point["trend_effective"],
                                    "kp_effective": data_point["kp_effective"],
                                    "ki_effective": data_point["ki_effective"],
                                    "kp_scale": data_point["kp_scale"],
                                    "ki_scale": data_point["ki_scale"],
                                    "integral_state": data_point["integral_state"],
                                    "notes": data_point["controller_notes"],
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
    STATIC_ROOT: Path = Path(__file__).parent / "web"
    INDEX_FILE: Path = STATIC_ROOT / "index.html"
    controller: Optional[TemperatureController] = None
    
    def log_message(self, format: str, *args: Any) -> None:
        """Override to use our logger."""
        logger.info("%s - %s", self.address_string(), format % args)
    
    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path in {"/", "/gui", "/index.html"}:
            self._serve_gui()
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
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
        elif path == "/api/presets":
            self._serve_presets()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/presets/save":
            self._serve_preset_save()
        elif path == "/api/presets/load":
            self._serve_preset_load()
        elif path == "/api/presets/delete":
            self._serve_preset_delete()
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
    
    def _send_error_json(self, message: str, status: int = 400) -> None:
        """Send standardized error response."""
        payload = {"error": message, "message": message}
        self._send_json(payload, status=status)

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        """Best-effort conversion to boolean."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            token = value.strip().lower()
            if token in {"1", "true", "t", "yes", "y", "on"}:
                return True
            if token in {"0", "false", "f", "no", "n", "off"}:
                return False
        return default if value == "" else bool(value)

    def _read_json_body(self) -> Dict[str, Any]:
        """Read request body as JSON object."""
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        if length <= 0:
            return {}

        raw = self.rfile.read(length)
        if not raw:
            return {}

        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON payload: {exc.msg}") from exc

        if not isinstance(payload, dict):
            raise ValueError("JSON payload must be an object")
        return payload

    def _serve_static(self, relative_path: str) -> None:
        """Serve a static asset from the web directory."""
        resource = (relative_path or "").strip().lstrip("/")
        if not resource:
            resource = "index.html"

        try:
            requested = Path(resource)
        except Exception:
            self.send_error(400, "Invalid static path")
            return

        if requested.is_absolute() or any(part == ".." for part in requested.parts):
            self.send_error(400, "Invalid static path")
            return

        static_root = self.STATIC_ROOT.resolve()
        file_path = (static_root / requested).resolve()

        if not str(file_path).startswith(str(static_root)):
            self.send_error(403, "Forbidden")
            return

        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "Not Found")
            return

        try:
            with open(file_path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            logger.error("Failed to read static file %s: %s", file_path, exc)
            self.send_error(500, "Failed to read static file")
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        cache_control = "no-cache, no-store, must-revalidate"
        if not content_type.startswith("text/") and content_type not in {"application/javascript", "application/json"}:
            cache_control = "public, max-age=3600"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def _serve_presets(self) -> None:
        """Return list of saved presets."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        try:
            presets = self.controller.list_presets()
        except Exception as exc:
            logger.exception("Failed to list presets: %s", exc)
            self.send_error(500, "Failed to list presets")
            return

        self._send_json({"presets": presets, "count": len(presets)})

    def _serve_preset_save(self) -> None:
        """Persist current controller setup as a preset."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(str(exc), status=400)
            return

        name = str(payload.get("name") or "").strip()
        metadata = payload.get("metadata")

        if not name:
            self._send_error_json("Preset name is required", status=400)
            return

        if metadata is not None and not isinstance(metadata, dict):
            self._send_error_json("Preset metadata must be an object", status=400)
            return

        try:
            saved = self.controller.save_preset(name, metadata=metadata)
        except ValueError as exc:
            self._send_error_json(str(exc), status=400)
            return
        except Exception as exc:
            logger.exception("Failed to save preset '%s': %s", name, exc)
            self._send_error_json("Failed to save preset", status=500)
            return

        presets = self.controller.list_presets()
        display = saved.get("display_name") or saved.get("name") or name
        message = f"Preset '{display}' saved"
        self._send_json({
            "message": message,
            "preset": saved,
            "presets": presets,
            "count": len(presets),
        })

    def _serve_preset_load(self) -> None:
        """Load a preset and optionally apply it."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(str(exc), status=400)
            return

        name = str(payload.get("name") or "").strip()
        if not name:
            self._send_error_json("Preset name is required", status=400)
            return

        apply_value = self._to_bool(payload.get("apply"), default=True)

        try:
            preset = self.controller.load_preset(name, apply=apply_value)
        except FileNotFoundError as exc:
            self._send_error_json(str(exc), status=404)
            return
        except ValueError as exc:
            self._send_error_json(str(exc), status=400)
            return
        except Exception as exc:
            logger.exception("Failed to load preset '%s': %s", name, exc)
            self._send_error_json("Failed to load preset", status=500)
            return

        presets = self.controller.list_presets()
        display = preset.get("display_name") or preset.get("name") or name
        message = f"Preset '{display}' loaded"
        if apply_value:
            message += " and applied"

        self._send_json({
            "message": message,
            "preset": preset,
            "presets": presets,
            "count": len(presets),
        })

    def _serve_preset_delete(self) -> None:
        """Delete a saved preset."""
        if not self.controller:
            self.send_error(500, "Controller not initialized")
            return

        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(str(exc), status=400)
            return

        name = str(payload.get("name") or "").strip()
        if not name:
            self._send_error_json("Preset name is required", status=400)
            return

        try:
            deleted = self.controller.delete_preset(name)
        except Exception as exc:
            logger.exception("Failed to delete preset '%s': %s", name, exc)
            self._send_error_json("Failed to delete preset", status=500)
            return

        if not deleted:
            self._send_error_json(f"Preset '{name}' not found", status=404)
            return

        presets = self.controller.list_presets()
        self._send_json({
            "message": f"Preset '{name}' deleted",
            "presets": presets,
            "count": len(presets),
        })

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

            if "overshoot_guard" in params:
                new_guard = float(params["overshoot_guard"][0])
                self.controller.pi_controller.set_overshoot_guard(new_guard)
                updated["overshoot_guard"] = new_guard

            if "pwm_near_cap" in params:
                new_near_cap = float(params["pwm_near_cap"][0])
                self.controller.pi_controller.set_pwm_near_cap(new_near_cap)
                updated["pwm_near_cap"] = new_near_cap

            if "predictive_brake_band" in params:
                new_band = float(params["predictive_brake_band"][0])
                self.controller.pi_controller.set_predictive_brake_band(new_band)
                updated["predictive_brake_band"] = new_band

            if "trend_brake_threshold" in params:
                new_trend = float(params["trend_brake_threshold"][0])
                self.controller.pi_controller.set_trend_brake_threshold(new_trend)
                updated["trend_brake_threshold"] = new_trend

            if "integral_bleed_rate" in params:
                new_bleed = float(params["integral_bleed_rate"][0])
                self.controller.pi_controller.set_integral_bleed_rate(new_bleed)
                updated["integral_bleed_rate"] = new_bleed

            if "trend_damping_gain" in params:
                new_gain = float(params["trend_damping_gain"][0])
                self.controller.pi_controller.set_trend_damping_gain(new_gain)
                updated["trend_damping_gain"] = new_gain

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
        if not self.INDEX_FILE.exists():
            logger.error("GUI index file missing: %s", self.INDEX_FILE)
            self.send_error(500, "GUI not available")
            return

        self._serve_static("index.html")


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
