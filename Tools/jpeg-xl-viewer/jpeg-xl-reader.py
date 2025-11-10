#!/usr/bin/env python3
"""
Matplotlib JPEG-XL grayscale viewer with index slider.

- Decodes .jxl via Pillow + pillow-jxl-plugin (preferred). Falls back to imageio if available.
- Assumes grayscale data, often 12-bit stored in uint16.
- Mapping modes:
    * hi12   : common camera layout (12-bit in high bits) -> display with value >> 8  [DEFAULT]
    * lo12   : 12-bit in low bits -> value >> 4
    * minmax : per-image min-max stretch
    * auto   : heuristic pick between hi12 and lo12 (fallback: minmax)
- One slider for image index. Left/Right arrows also step.
- Title shows filename and raw stats.

Install:
    pip install pillow pillow-jxl-plugin matplotlib numpy
    # If pillow-jxl-plugin wheel is not available, install Rust toolchain (rustup),
    # or use: conda-forge imageio imagecodecs (and it'll fall back to imageio).

Usage:
    python jxl_matplotlib_viewer.py --directory <dir> [--mode hi12|minmax|lo12|auto] [--autoplay] [--fps 10]
    
    Press SPACE to toggle autoplay on/off
    Press LEFT/RIGHT arrows (or j/k) to step through images manually
"""

from __future__ import annotations
import argparse
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider, Button, TextBox

# Try Pillow + plugin first
PIL_AVAILABLE = False
try:
    import pillow_jxl  # registers plugin with Pillow
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# Optional fallback: imageio (conda-forge imagecodecs must include JXL)
IMGIO_AVAILABLE = False
try:
    import imageio.v3 as iio
    IMGIO_AVAILABLE = True
except Exception:
    IMGIO_AVAILABLE = False


def decode_jxl(path: Path) -> np.ndarray:
    """
    Return grayscale uint16 image.
    Tries Pillow (with pillow-jxl-plugin) first, then imageio.
    """
    if PIL_AVAILABLE:
        im = Image.open(str(path))
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            arr16 = np.array(im, dtype=np.uint16)
            if arr16.ndim == 3:  # in case of stray channels, convert to L
                arr16 = np.array(im.convert("I;16"), dtype=np.uint16)
            return arr16
        elif im.mode == "L":
            # upscale to 16-bit space for uniform pipeline
            arr8 = np.array(im, dtype=np.uint8)
            return (arr8.astype(np.uint16) << 8)
        else:
            # convert to 16-bit grayscale
            return np.array(im.convert("I;16"), dtype=np.uint16)

    if IMGIO_AVAILABLE:
        arr = iio.imread(str(path))
        if arr.ndim == 3:
            # convert to luma if RGB snuck in
            arr = np.mean(arr, axis=2)
        arr = np.asarray(arr)
        if arr.dtype == np.uint8:
            return (arr.astype(np.uint16) << 8)
        if arr.dtype != np.uint16:
            return arr.astype(np.uint16)
        return arr

    raise RuntimeError(
        "No decoder available. Install either:\n"
        "  pip install pillow pillow-jxl-plugin\n"
        "or\n"
        "  conda install -c conda-forge imageio imagecodecs"
    )


def map_u16_to_u8(img_u16: np.ndarray, mode: str) -> np.ndarray:
    """
    Map uint16 grayscale to uint8 for display.
    modes: 'hi12', 'lo12', 'minmax', 'auto'
    """
    assert img_u16.dtype == np.uint16 and img_u16.ndim == 2

    if mode == "hi12":
        return (img_u16 >> 8).astype(np.uint8)
    if mode == "lo12":
        return (img_u16 >> 4).astype(np.uint8)
    if mode == "minmax":
        vmin, vmax = int(img_u16.min()), int(img_u16.max())
        if vmax <= vmin:
            return np.zeros_like(img_u16, dtype=np.uint8)
        norm = (img_u16.astype(np.float32) - vmin) / (vmax - vmin)
        return (norm * 255.0).astype(np.uint8)

    # auto: if low 4 bits are ~all zero -> hi12 else lo12; fallback: minmax
    low4_nonzero_frac = float(((img_u16 & 0xF) != 0).sum()) / img_u16.size
    if low4_nonzero_frac < 0.05:
        return (img_u16 >> 8).astype(np.uint8)
    else:
        # Sometimes data truly live in low 12 bits
        return (img_u16 >> 4).astype(np.uint8)


def build_filelist(directory: Path) -> List[Path]:
    import re
    exts = ("*.jxl", "*.JXL")
    files: List[Path] = []
    for pat in exts:
        files.extend(p for p in directory.glob(pat) if p.is_file())
    
    # Sort numerically by extracting numbers from filename
    def numeric_key(path: Path) -> tuple:
        # Extract all numbers from the stem (filename without extension)
        numbers = re.findall(r'\d+', path.stem)
        # Convert to integers for proper numeric sorting
        # Return as tuple of ints; files without numbers sort first
        return tuple(int(n) for n in numbers) if numbers else ()
    
    return sorted(files, key=numeric_key)


def main():
    ap = argparse.ArgumentParser(description="Matplotlib JPEG-XL grayscale viewer with index slider")
    ap.add_argument("--directory", type=Path, default=Path.cwd(), help="Folder with .jxl files")
    ap.add_argument("--mode", type=str, default="hi12", choices=["hi12", "lo12", "minmax", "auto"],
                    help="Mapping from uint16 to uint8 (default: hi12)")
    ap.add_argument("--cmap", type=str, default="gray", help="Matplotlib colormap (default: gray)")
    ap.add_argument("--autoplay", action="store_true", help="Start autoplay on launch")
    ap.add_argument("--fps", type=float, default=10.0, help="Autoplay frame rate (default: 10 fps)")
    args = ap.parse_args()

    files = build_filelist(args.directory)
    if not files:
        print(f"No .jxl files found in {args.directory}")
        return 1

    # Lazy loader cache (simple one-entry)
    cache_index: Optional[int] = None
    cache_raw: Optional[np.ndarray] = None
    cache_u8: Optional[np.ndarray] = None

    def load_index(idx: int) -> Tuple[np.ndarray, np.ndarray]:
        nonlocal cache_index, cache_raw, cache_u8
        if cache_index == idx and cache_raw is not None and cache_u8 is not None:
            return cache_raw, cache_u8
        raw = decode_jxl(files[idx])
        u8 = map_u16_to_u8(raw, args.mode)
        cache_index, cache_raw, cache_u8 = idx, raw, u8
        return raw, u8

    # Initial image
    idx0 = 0
    raw0, u80 = load_index(idx0)

    # Figure + Axes - UI on right side
    fig = plt.figure(figsize=(14, 8))
    
    # Main image area (left side, larger)
    ax = plt.subplot2grid((10, 11), (0, 0), rowspan=10, colspan=8)
    
    # Slider column (full height)
    ax_slider = plt.subplot2grid((10, 11), (0, 8), rowspan=10, colspan=1)
    
    # Control buttons on far right (vertical layout)
    ax_prev = plt.subplot2grid((10, 11), (1, 9), rowspan=1, colspan=2)
    ax_play = plt.subplot2grid((10, 11), (2, 9), rowspan=1, colspan=2)
    ax_next = plt.subplot2grid((10, 11), (3, 9), rowspan=1, colspan=2)
    ax_fps = plt.subplot2grid((10, 11), (5, 9), rowspan=1, colspan=2)

    im = ax.imshow(u80, cmap=args.cmap, vmin=0, vmax=255, interpolation="nearest")
    ax.set_axis_off()

    def title_for(i: int, raw: np.ndarray) -> str:
        mn, mx = int(raw.min()), int(raw.max())
        low4 = float(((raw & 0xF) != 0).sum()) / raw.size
        return f"{files[i].name}  [{i+1}/{len(files)}]  raw16:min={mn} max={mx} low4%={low4*100:.2f}  mode={args.mode}"

    ax.set_title(title_for(idx0, raw0), fontsize=10)

    # Slider (integer index) - vertical orientation
    slider = Slider(ax=ax_slider, label="Frame", valmin=0, valmax=len(files) - 1, 
                    valinit=idx0, valstep=1, orientation='vertical')

    def update(_):
        i = int(slider.val)
        raw, u8 = load_index(i)
        im.set_data(u8)
        ax.set_title(title_for(i, raw), fontsize=10)
        im.axes.figure.canvas.draw_idle()

    slider.on_changed(update)

    # Autoplay state
    autoplay_state = {"running": args.autoplay, "timer": None, "fps": args.fps}
    
    def get_interval_ms():
        return int(1000.0 / autoplay_state["fps"])

    def autoplay_step():
        if not autoplay_state["running"]:
            return
        v = int(slider.val)
        if v < len(files) - 1:
            slider.set_val(v + 1)
        else:
            # Loop back to start
            slider.set_val(0)
        # Schedule next frame
        autoplay_state["timer"] = fig.canvas.new_timer(get_interval_ms())
        autoplay_state["timer"].single_shot = True
        autoplay_state["timer"].add_callback(autoplay_step)
        autoplay_state["timer"].start()

    def start_autoplay():
        if not autoplay_state["running"]:
            autoplay_state["running"] = True
            # Update button text through ax
            ax_play.clear()
            global btn_play
            btn_play = Button(ax_play, "⏸ Pause")
            btn_play.on_clicked(play_pause)
            print(f"Autoplay started at {autoplay_state['fps']} fps")
            autoplay_step()
            fig.canvas.draw_idle()

    def stop_autoplay():
        if autoplay_state["running"]:
            autoplay_state["running"] = False
            # Update button text through ax
            ax_play.clear()
            global btn_play
            btn_play = Button(ax_play, "▶ Play")
            btn_play.on_clicked(play_pause)
            if autoplay_state["timer"] is not None:
                autoplay_state["timer"].stop()
            print("Autoplay paused")
            fig.canvas.draw_idle()

    def toggle_autoplay():
        if autoplay_state["running"]:
            stop_autoplay()
        else:
            start_autoplay()

    # Button callbacks
    def prev_frame(event):
        stop_autoplay()
        v = int(slider.val)
        if v > 0:
            slider.set_val(v - 1)

    def next_frame(event):
        stop_autoplay()
        v = int(slider.val)
        if v < len(files) - 1:
            slider.set_val(v + 1)

    def play_pause(event):
        toggle_autoplay()

    def update_fps(text):
        try:
            new_fps = float(text)
            if new_fps > 0:
                autoplay_state["fps"] = new_fps
                print(f"FPS updated to {new_fps}")
                # If playing, restart with new fps
                if autoplay_state["running"]:
                    stop_autoplay()
                    start_autoplay()
            else:
                print("FPS must be positive")
        except ValueError:
            print(f"Invalid FPS value: {text}")

    # Create UI buttons
    btn_prev = Button(ax_prev, "◀ Prev")
    btn_prev.on_clicked(prev_frame)

    btn_play = Button(ax_play, "▶ Play" if not args.autoplay else "⏸ Pause")
    btn_play.on_clicked(play_pause)

    btn_next = Button(ax_next, "Next ▶")
    btn_next.on_clicked(next_frame)

    fps_box = TextBox(ax_fps, "FPS:", initial=str(args.fps), textalignment="center")
    fps_box.on_submit(update_fps)

    # Key bindings: left/right to step, space to toggle autoplay
    def on_key(event):
        if event.key in ("left", "j", "J"):
            prev_frame(None)
        elif event.key in ("right", "k", "K"):
            next_frame(None)
        elif event.key == " ":
            toggle_autoplay()

    fig.canvas.mpl_connect("key_press_event", on_key)

    # Start autoplay if requested
    if args.autoplay:
        print(f"Autoplay enabled at {args.fps} fps. Press SPACE to pause/resume.")
        start_autoplay()

    plt.tight_layout()
    plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
