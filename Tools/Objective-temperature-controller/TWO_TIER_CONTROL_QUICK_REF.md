# Two-Tier Control Quick Reference

## The Problem
❌ **Persistent oscillations** around setpoint regardless of tuning

## The Solution
✅ **Two control strategies** based on distance from setpoint

---

## Control Modes

| Distance from Setpoint | Mode | Behavior | Gains |
|------------------------|------|----------|-------|
| **> 1.0°C** | Far Mode | Aggressive control | 100% |
| **≤ 1.0°C** | Near Mode | Trend-aware, gentle | 50-100% |

---

## Near Mode Intelligence

When within 1°C of setpoint, the controller predicts temperature 5 seconds ahead:

```
📈 Trend Analysis (last 10 samples)
  ↓
🔮 Predict temperature 5s ahead
  ↓
  ┌─────────────────────────────────────────┐
  │ Predicted to enter deadband?             │
  │   YES → Coast (PWM = 0)                  │
  │   NO → Approaching setpoint?             │
  │         YES → Reduce gains 50%           │
  │         NO → Use 70-100% gains           │
  └─────────────────────────────────────────┘
```

---

## Key Parameter

### `near_setpoint_threshold` (default: 1.0°C)

**What it does:**
- Boundary between aggressive and gentle control
- Error ≤ threshold → Use trend-aware control
- Error > threshold → Use full power control

**How to adjust:**

```bash
# Make more conservative (engage gentle mode earlier)
GET /api/params?near_setpoint_threshold=1.5

# Make more aggressive (use full power longer)
GET /api/params?near_setpoint_threshold=0.7
```

---

## Expected Results

### Before Two-Tier Control
```
Setpoint: 25.0°C

Time  Temp    Mode      PWM
----- ------- --------- -----
10s   24.5°C  HEATING   0.20
20s   25.3°C  COOLING   0.15  ← Overshoot!
30s   24.8°C  HEATING   0.18
40s   25.2°C  COOLING   0.12  ← Oscillating!
50s   24.9°C  HEATING   0.10
...
```

### After Two-Tier Control
```
Setpoint: 25.0°C

Time  Temp    Mode      PWM   Note
----- ------- --------- ----- ------------------
10s   23.5°C  HEATING   0.25  Far mode (1.5°C)
20s   24.3°C  HEATING   0.20  Near mode (0.7°C)
25s   24.7°C  HEATING   0.10  Gentle (pred 24.95°C)
28s   24.9°C  OFF       0.00  Coasting (pred 25.05°C)
32s   25.0°C  OFF       0.00  ✓ At setpoint
35s   25.0°C  OFF       0.00  ✓ Stable!
```

---

## Debug Log Examples

```
[INFO] Far from setpoint mode: err=1.850°C (aggressive control)
[INFO] Near setpoint mode: err=0.720°C, trend=0.0385°C/s
[DEBUG] Gentle approach: reducing gains by 50% (pred_err=0.527°C)
[DEBUG] Coasting: predicted to reach setpoint (pred_err=0.082°C)
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Still oscillating | Increase `near_setpoint_threshold` to 1.5-2.0°C |
| Too slow to settle | Decrease `near_setpoint_threshold` to 0.5-0.7°C |
| Overshooting | Increase threshold, check trend calculation working |
| Not reaching setpoint | Check deadband settings, may need higher gains |

---

## How It Works - Simple Analogy

**Driving a car to a stop sign:**

🚗 **Far Mode (>1°C):** 
- "I'm far away" → full throttle
- Goal: Get there fast

🚗 **Near Mode (≤1°C):**
- "I'm getting close" → check my speed
- If moving fast toward stop → ease off throttle early
- If not moving toward stop → maintain speed
- **Result:** Smooth stop, no bouncing!

The temperature controller does the same thing with heating/cooling!

---

## Technical Details

**Trend Calculation:**
- Linear regression on last 10 temperature samples
- Returns rate in °C/s
- Robust against noise

**Prediction:**
- `predicted_temp = current_temp + trend × 5.0`
- 5-second lookahead window
- Compares predicted error vs current error

**Gain Reduction:**
- Coast: 0% gains (PWM = 0)
- Gentle: 50% gains
- Conservative: 70% gains
- Normal: 100% gains

---

## API Endpoints

```bash
# Get all parameters (includes near_setpoint_threshold)
GET /api/params

# Set near-setpoint threshold
GET /api/params?near_setpoint_threshold=1.2

# Check PID status
GET /api/pid/status

# Enable/disable PID
GET /api/pid/enable
GET /api/pid/disable
```

---

## Summary

✨ **Key Innovation:** Look ahead 5 seconds using temperature trend
🎯 **Result:** Stop heating/cooling before overshooting
📊 **Evidence:** Check logs for "Coasting" and "Gentle approach" messages
⚙️ **Tuning:** Adjust `near_setpoint_threshold` based on system response
