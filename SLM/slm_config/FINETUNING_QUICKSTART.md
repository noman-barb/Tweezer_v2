# Auto Fine-Tuning Quick Start Guide

## 🎯 What is Auto Fine-Tuning?

Auto Fine-Tuning automatically improves your SLM calibration by:
1. Randomly placing optical traps across your sample
2. Measuring where particles actually get trapped
3. Computing an optimized calibration based on the errors
4. Saving the result for immediate use

**Result**: Better alignment between where you click and where traps appear!

## ⚡ Quick Start (5 Minutes)

### Step 1: Prepare Your System
- ✅ Connect to **Image Server** (tracking must be ON)
- ✅ Connect to **SLM**
- ✅ Load your current **best calibration** config
- ✅ Have **particles in your sample** (at least a few visible)

### Step 2: Open the Fine-Tuning Panel
- Scroll down in the **SLM POINT CONTROL** window
- Look for the gold-colored **AUTO FINE-TUNING** section

### Step 3: Start Fine-Tuning
1. Set **Sample Count** (try 12 for first run)
2. Set **Edge Margin** (50 pixels is usually good)
3. Keep **Show Visualization** checked
4. Click **START**

### Step 4: Watch the Magic! ✨
You'll see:
- 🎯 **Cyan pulsing crosshair** = where trap is being placed
- 🟢 **Green pulsing circle** = detected particle location
- **Yellow lines** = error vectors showing misalignment
- **Progress bar** filling up
- **Status**: "Running: 5/12" etc.

### Step 5: Use Your Improved Calibration
1. Wait for "Completed! Check configs above."
2. In the dropdown, select the newest config (top of list)
   - Format: `finetuning_20250105_143022`
3. Look at the **metadata** to see improvement:
   ```
   Created: 2025-01-05 14:30
   Base: default
   Samples: 12
   RMS Error: 8.45px → 2.13px
   Improvement: 74.8%
   ```
4. Click **Load Selected**
5. Done! Your calibration is now much better!

## 📊 What Do The Numbers Mean?

### RMS Error
- **< 3 pixels**: Excellent! 🎉
- **3-8 pixels**: Good, usable ✅
- **> 8 pixels**: Needs more work ⚠️

### Improvement Percentage
- **> 50%**: Great improvement! 🌟
- **20-50%**: Decent improvement ✓
- **< 20%**: Base calibration may need adjustment

## 🎨 Visual Guide

### What You'll See During Fine-Tuning

```
┌─────────────────────────────────────┐
│   [Blue Rectangle = Sample Area]    │
│                                      │
│    ⊕ ← Cyan pulsing = Current trap  │
│    │                                 │
│    ↓ Yellow line = Error            │
│    ● ← Green pulsing = Particle     │
│                                      │
│  Past samples:                       │
│    + ● (Green = good, <5px error)   │
│    + ● (Yellow = medium, 5-15px)    │
│    + ● (Red = bad, >15px)           │
│                                      │
└─────────────────────────────────────┘
```

## ⚙️ Settings Guide

### Sample Count
| Value | When to Use |
|-------|-------------|
| 3-5   | Quick test, rough calibration |
| 10-15 | Normal use (recommended) |
| 20-30 | High precision needed |
| 30+   | Diminishing returns, takes longer |

**Tip**: Start with 12. If results look good but you want perfection, run again with 20.

### Edge Margin
| Value | When to Use |
|-------|-------------|
| 30-50px | Good particle tracking everywhere |
| 50-100px | Standard (recommended) |
| 100-150px | Particles hard to detect near edges |
| 150-200px | Very small stable region |

**Tip**: Larger margins are safer but reduce calibration coverage.

## 🔄 Iterative Improvement Workflow

Want the absolute best calibration? Do this:

1. **First Pass**: Run with 12 samples
   - Should improve by ~50-70%
   
2. **Second Pass**: 
   - Load the new config
   - Run again with 20 samples
   - Should improve another ~20-40%
   
3. **Third Pass** (optional):
   - Load the newest config
   - Run with 30 samples
   - Marginal improvement, but can hit ~95% accuracy

Each pass refines the previous result!

## ❌ Troubleshooting

### "No particle found" Many Times
**Problem**: Tracker not detecting particles  
**Fix**:
- Check **Tracking Parameters** window
- Lower `detection_threshold`
- Increase trap `intensity`
- Make sure particles are actually in sample!

### Improvement < 20%
**Problem**: Base calibration is way off  
**Fix**:
- Manually adjust your base config first
- Use 3-point manual calibration
- Check SLM focus/alignment
- Verify camera and SLM are synced

### Process Gets Stuck
**Problem**: Not capturing particles  
**Fix**:
- Click **STOP**
- Increase trap intensity
- Check SLM connection
- Try again with different parameters

### High Variation (Some Red, Some Green)
**Problem**: Inhomogeneous calibration  
**Fix**:
- Increase sample count (20-30)
- Reduce edge margin (sample more area)
- May indicate optical distortion

## 💡 Pro Tips

### Best Practices
1. **Start from a reasonable base** - garbage in, garbage out!
2. **Have particles distributed** across the field of view
3. **Run when sample is stable** - no drift, good focus
4. **Keep visualization on** first time to understand process
5. **Save your old config** before loading new one (just in case)

### When to Re-Run
- After adjusting optics
- Sample changes (different particles, medium)
- SLM moved or realigned
- Calibration drift over time (monthly maintenance)
- Want to squeeze out a bit more accuracy

### Naming Convention
Configs are auto-named with datetime:
```
finetuning_YYYYMMDD_HHMMSS
finetuning_20250105_143022  ← Jan 5, 2025 at 2:30:22 PM
```
Newest = top of dropdown list

## 🎓 Understanding the Process

### What Happens Under the Hood

1. **Random Sampling**
   - System picks random (x,y) positions
   - Avoids edges (uses your margin setting)
   - Each position independent

2. **Trap & Detect**
   - Place optical trap at target position
   - Wait for particle to be captured (1.5 seconds)
   - Find nearest tracked particle
   - Measure error (distance from target)

3. **Math Magic** 🧮
   - Collect all target → particle pairs
   - Solve for best affine transformation
   - Convert to calibration parameters
   - Calculate improvement statistics

4. **Auto-Save**
   - Save as new config with datetime
   - Keep original base config unchanged
   - Ready to load immediately

### Why It Works
The affine transformation accounts for:
- **Translation** (offset)
- **Rotation** (angular misalignment)
- **Scale** (size differences)
- **Shear** (skew/distortion)

By sampling across the field of view, the optimization finds the transformation that minimizes overall error.

## 📞 Need Help?

### Check These First
1. Error logs in console
2. All connections active (Image, SLM)
3. Tracking is working (see particles in overlay)
4. Base calibration loaded

### Common Questions

**Q: How long does it take?**  
A: ~2.5 seconds per sample. 12 samples ≈ 30 seconds.

**Q: Can I run multiple times?**  
A: Yes! Load the result and run again for refinement.

**Q: Will it overwrite my manual config?**  
A: No! Fine-tuned configs are separate.

**Q: Can I delete bad results?**  
A: Yes, use the Delete button (with confirmation).

**Q: What if I stop mid-process?**  
A: No config is saved. Safe to stop anytime.

## 🚀 Ready to Start!

1. Check your connections ✅
2. Load a base config ✅
3. Click START ✅
4. Watch the magic happen! ✨

**Your calibration will thank you!** 🎉

---

*For detailed technical information, see `FINETUNING_README.md`*
