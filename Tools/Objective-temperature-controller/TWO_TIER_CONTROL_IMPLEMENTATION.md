# Two-Tier Temperature Control Implementation

## Problem Statement
The temperature controller was experiencing persistent oscillations regardless of tuning parameters. The system would overshoot and oscillate around the setpoint.

## Solution: Two-Tier Control Strategy

The new implementation uses **two distinct control modes** based on proximity to setpoint:

### Tier 1: Far From Setpoint (>1°C error)
- **Aggressive control** to reach setpoint quickly
- Uses full PI gains (Kp and Ki)
- Standard control behavior
- Goal: Get close to setpoint as fast as possible

### Tier 2: Near Setpoint (≤1°C error)
- **Gentle, trend-aware control** to prevent overshoot
- Analyzes temperature trend using linear regression on last 10 samples
- Predicts temperature 5 seconds ahead
- Adaptive gain reduction based on prediction:
  - **Coasting**: If predicted to enter deadband → turn off completely (PWM = 0)
  - **Gentle approach**: If approaching setpoint → reduce gains by 50%
  - **Normal control**: If moving away from setpoint → use 100% gains
  - **Conservative default**: No trend data available → use 70% gains

## Key Features

### 1. Temperature Trend Detection
- Maintains history of last 10 temperature readings with timestamps
- Uses **linear regression** for robust trend estimation (°C/s)
- More accurate than simple difference calculations
- Filters out noise through statistical approach

### 2. Predictive Control
- Looks ahead 5 seconds based on current temperature trend
- Anticipates when temperature will reach deadband
- Prevents overshoot by reducing/stopping control effort early

### 3. Adaptive Gain Reduction
The controller dynamically adjusts PI gains based on the situation:
```
If near setpoint AND approaching setpoint:
    If predicted to enter deadband soon:
        → Turn off (coast to setpoint)
    Else:
        → Reduce gains to 50%
Else if near setpoint AND not approaching:
    → Use full gains (100%)
Else if far from setpoint:
    → Use full gains (100%)
```

## New Parameters

### `near_setpoint_threshold` (default: 1.0°C)
- Threshold that divides "near" vs "far" control modes
- When `|error| ≤ threshold`: Use gentle, trend-aware control
- When `|error| > threshold`: Use aggressive control
- Configurable via API: `/api/params?near_setpoint_threshold=X`

## Implementation Details

### Modified Classes

#### `PIController`
**New attributes:**
- `near_setpoint_threshold`: The 1°C threshold (configurable)
- `temp_history`: Deque of last 10 temperature readings
- `temp_time_history`: Deque of corresponding timestamps

**New methods:**
- `_calculate_temp_trend()`: Computes temperature rate using linear regression
- `set_near_setpoint_threshold(threshold)`: Update threshold
- `get_near_setpoint_threshold()`: Get current threshold

**Modified methods:**
- `compute()`: Now implements two-tier control logic
- `reset()`: Clears temperature history

### Control Flow

```python
1. Update temperature history (last 10 samples)
2. Calculate error = setpoint - current_temp
3. Determine if near_setpoint (|error| ≤ 1°C)

IF near_setpoint:
    IF within_deadband:
        → Use feed-forward only or turn off
    ELSE:
        Calculate temp_trend (linear regression)
        Predict temp 5 seconds ahead
        
        IF predicted to reach deadband:
            → Coast (PWM = 0)
        ELSE IF approaching setpoint:
            → Reduce gains to 50%
        ELSE:
            → Use 70% gains (conservative)

ELSE (far from setpoint):
    → Use full 100% gains

4. Apply selected gains to standard PI control
5. Output PWM and mode
```

## Expected Behavior

### Approaching Setpoint From Below (Heating)
1. **Far mode** (>1°C below): Aggressive heating with full PWM
2. **Near mode** (≤1°C below): 
   - If heating trend continues → reduce PWM gradually
   - If temperature rising fast → coast earlier
   - Prevents overshoot into cooling territory

### Approaching Setpoint From Above (Cooling)
1. **Far mode** (>1°C above): Aggressive cooling with full PWM
2. **Near mode** (≤1°C above):
   - If cooling trend continues → reduce PWM gradually
   - If temperature dropping fast → coast earlier
   - Prevents overshoot into heating territory

### At Setpoint
- Controller enters OFF mode or uses only feed-forward
- No oscillation because:
  - Trend detection prevents late braking
  - Predictive control stops control effort early
  - Reduced gains prevent aggressive corrections

## Configuration & Tuning

### Via HTTP API

```bash
# Set the near-setpoint threshold to 0.8°C
GET /api/params?near_setpoint_threshold=0.8

# Get current parameters (includes near_setpoint_threshold)
GET /api/params
```

### Tuning Recommendations

**If still oscillating:**
- Increase `near_setpoint_threshold` (try 1.5 or 2.0°C)
- This engages gentle control earlier
- More conservative approach

**If too slow to reach setpoint:**
- Decrease `near_setpoint_threshold` (try 0.5 or 0.7°C)
- Maintains aggressive control longer
- Faster response

**Fine-tuning the gentle mode:**
- Adjust base `kp` and `ki` values
- The 50% reduction in near-setpoint mode will scale accordingly
- Can also tune per-mode gains (`kp_heating`, `kp_cooling`, etc.)

## Logging & Debugging

The controller now logs additional debug information:
- `"Near setpoint mode: err=X.XXX°C, trend=X.XXXX°C/s"` - Entered gentle control
- `"Far from setpoint mode: err=X.XXX°C (aggressive control)"` - Using full gains
- `"Coasting: predicted to reach setpoint (pred_err=X.XXX°C)"` - Coasting to setpoint
- `"Gentle approach: reducing gains by 50% (pred_err=X.XXX°C)"` - Using reduced gains
- `"Not approaching setpoint: using normal gains (pred_err=X.XXX°C)"` - Full gains despite being near

## Benefits

1. **Eliminates overshoot** - Predictive control stops heating/cooling early
2. **Reduces oscillations** - Trend-aware control prevents aggressive corrections
3. **Faster settling** - Coasting behavior allows smooth approach to setpoint
4. **Maintains performance** - Far-mode still uses aggressive control for fast response
5. **Adaptive** - Automatically adjusts based on system dynamics
6. **Robust** - Linear regression filters noise in trend calculation

## Testing Recommendations

1. **Start with defaults** - The 1.0°C threshold should work well
2. **Monitor logs** - Check when controller switches between modes
3. **Observe predictions** - Watch predicted_error values in debug logs
4. **Adjust threshold** - Fine-tune based on your system's thermal mass
5. **Test step changes** - Try large setpoint changes to verify both modes work

## Future Enhancements

Possible improvements:
- Make the 5-second prediction window configurable
- Add hysteresis to mode switching (e.g., 0.9°C to enter, 1.1°C to exit)
- Tune gain reduction factor (currently 50%, could be configurable)
- Add separate thresholds for heating vs cooling
- Implement adaptive threshold based on system performance
