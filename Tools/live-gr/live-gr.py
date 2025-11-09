#!/usr/bin/env python3
"""
Live Pair Correlation Function g(r) Visualizer

Connects to the ImageServer gRPC tracking service and displays:
- Real-time particle positions
- Live-updated pair correlation function g(r)
- Running statistics and metrics

Author: Optimized for Tweezer project
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import grpc
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.collections import PathCollection, PolyCollection
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.text import Text
import numpy as np
import pandas as pd
import trackpy as tp
from google.protobuf import empty_pb2
from google.protobuf import json_format
from matplotlib.animation import FuncAnimation
from matplotlib.gridspec import GridSpec

# Add Camera directory to path for imports
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CAMERA_PATH = _REPO_ROOT / "Camera"
if _CAMERA_PATH.is_dir() and str(_CAMERA_PATH) not in sys.path:
    sys.path.insert(0, str(_CAMERA_PATH))

from image_exchange_pb2_grpc import ImageExchangeStub  # type: ignore


class GrCalculator:
    """Handles g(r) calculation with efficient caching."""
    
    def __init__(
        self,
        cutoff: float = 200.0,
        dr: float = 1.0,
        fraction: float = 1.0,
        pixel_to_sigma: float = 32.0,
        skip_first: int = 5,
    ):
        self.cutoff = cutoff
        self.dr = dr
        self.fraction = fraction
        self.pixel_to_sigma = pixel_to_sigma
        self.skip_first = skip_first
        
    def compute(self, df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
        """Compute g(r) from particle positions DataFrame."""
        if df.empty or len(df) < 2:
            return np.array([]), np.array([])
        
        try:
            r, gr = tp.static.pair_correlation_2d(  # type: ignore[attr-defined]
                df, cutoff=self.cutoff, fraction=self.fraction, dr=self.dr
            )
            # Remove last element from r to match length
            r = r[:-1]
            
            # Skip noisy short-range data and convert to sigma units
            r = r[self.skip_first:] / self.pixel_to_sigma
            gr = gr[self.skip_first:]
            
            return r, gr
        except Exception as e:
            print(f"Warning: g(r) calculation failed: {e}")
            return np.array([]), np.array([])


class TrackingDataBuffer:
    """Thread-safe buffer for tracking data with averaging."""
    
    def __init__(self, history_length: int = 20):
        self.history_length = history_length
        self.particle_data = pd.DataFrame(columns=["x", "y", "mass"])
        self.gr_history: deque[Tuple[np.ndarray, np.ndarray]] = deque(maxlen=history_length)
        self.particle_count = 0
        self.last_update_time = 0.0
        self.frame_count = 0
        
    def update(self, positions: pd.DataFrame, r: np.ndarray, gr: np.ndarray) -> None:
        """Update buffer with new data."""
        self.particle_data = positions.copy()
        self.particle_count = len(positions)
        self.last_update_time = time.time()
        self.frame_count += 1
        
        if len(r) > 0 and len(gr) > 0:
            self.gr_history.append((r, gr))
    
    def get_averaged_gr(self) -> Tuple[np.ndarray, np.ndarray]:
        """Get time-averaged g(r) from history."""
        if not self.gr_history:
            return np.array([]), np.array([])
        
        r_list = [item[0] for item in self.gr_history]
        gr_list = [item[1] for item in self.gr_history]
        
        avg_r = np.mean(r_list, axis=0)
        avg_gr = np.mean(gr_list, axis=0)
        
        return np.asarray(avg_r, dtype=float), np.asarray(avg_gr, dtype=float)
    
    def get_std_gr(self) -> np.ndarray:
        """Get standard deviation of g(r) for error bands."""
        if len(self.gr_history) < 2:
            return np.array([])
        
        gr_list = [item[1] for item in self.gr_history]
        return np.asarray(np.std(gr_list, axis=0), dtype=float)


class LiveGrVisualizer:
    """Main application for live g(r) visualization."""
    
    def __init__(
        self,
        server_address: str = "localhost:50052",
        history_length: int = 20,
        update_interval_ms: int = 100,
        pixel_to_sigma: float = 32.0,
        mass_cutoff: float = 25000.0,
        style: str = "dark_background",
    ):
        self.server_address = server_address
        self.update_interval_ms = update_interval_ms
        
        # Initialize components
        self.gr_calculator = GrCalculator(pixel_to_sigma=pixel_to_sigma)
        self.mass_cutoff = mass_cutoff
        self.buffer = TrackingDataBuffer(history_length=history_length)
        
        # gRPC client
        self.channel: Optional[grpc.Channel] = None
        self.stub: Optional[ImageExchangeStub] = None
        
        # Plotting style
        self.style = style
        self.fig: Optional[Figure] = None
        self.ax_particles: Optional[Axes] = None
        self.ax_gr: Optional[Axes] = None
        self.ax_stats: Optional[Axes] = None
        self.ax_info: Optional[Axes] = None
        
        # Plot elements
        self.scatter: Optional[PathCollection] = None
        self.line_gr: Optional[Line2D] = None
        self.fill_gr: Optional[PolyCollection] = None
        self.stats_text: Optional[Text] = None
        self.info_text_artist: Optional[Text] = None
        
        # Connection state
        self.connected = False
        self.connection_start_time = 0.0
        
    def connect(self) -> bool:
        """Establish gRPC connection to image server."""
        try:
            self.channel = grpc.insecure_channel(
                self.server_address,
                options=[
                    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
                    ("grpc.max_send_message_length", 16 * 1024 * 1024),
                ],
            )
            stub = ImageExchangeStub(self.channel)
            self.stub = stub
            
            # Test connection
            _ = stub.GetTrackingConfig(empty_pb2.Empty(), timeout=5.0)
            
            self.connected = True
            self.connection_start_time = time.time()
            print(f"✓ Connected to image server at {self.server_address}")
            return True
            
        except Exception as e:
            print(f"✗ Failed to connect to {self.server_address}: {e}")
            self.connected = False
            return False
    
    def disconnect(self) -> None:
        """Close gRPC connection."""
        if self.channel:
            self.channel.close()
            self.channel = None
            self.stub = None
        self.connected = False
        print("Disconnected from image server")
    
    def fetch_latest_tracks(self) -> Optional[Dict[str, Any]]:
        """Fetch latest tracking data from server."""
        if not self.connected or not self.stub:
            return None
        
        try:
            response = self.stub.GetLatestTracks(empty_pb2.Empty(), timeout=1.0)
            data = json_format.MessageToDict(response, preserving_proto_field_name=True)
            
            if not data.get("has_tracks", False):
                return None
            
            return data
            
        except Exception as e:
            print(f"Warning: Failed to fetch tracks: {e}")
            return None
    
    def process_tracking_data(self, data: Dict[str, Any]) -> None:
        """Process tracking data and update buffer."""
        detections = data.get("detections", [])
        
        if not detections:
            return
        
        # Convert to DataFrame with mass information
        x_vals: list[float] = []
        y_vals: list[float] = []
        masses: list[float] = []
        for det in detections:
            x_val = det.get("x", float("nan"))
            y_val = det.get("y", float("nan"))
            mass_val = det.get("mass", float("nan"))
            try:
                x_vals.append(float(x_val))
            except (TypeError, ValueError):
                x_vals.append(float("nan"))
            try:
                y_vals.append(float(y_val))
            except (TypeError, ValueError):
                y_vals.append(float("nan"))
            try:
                masses.append(float(mass_val))
            except (TypeError, ValueError):
                masses.append(float("nan"))
        positions = pd.DataFrame({
            "x": x_vals,
            "y": y_vals,
            "mass": masses,
        })
        
        # Calculate g(r)
        r, gr = self.gr_calculator.compute(positions[["x", "y"]])
        
        # Update buffer
        self.buffer.update(positions, r, gr)
    
    def setup_plot(self) -> None:
        """Initialize matplotlib figure and axes."""
        plt.style.use(self.style)
        plt.rcParams['axes.grid'] = False

        fig = plt.figure(figsize=(14, 10))
        self.fig = fig
        gs = GridSpec(3, 2, figure=fig, height_ratios=[2, 2, 1], hspace=0.3, wspace=0.3)

        # Particle positions (top-left)
        ax_particles = fig.add_subplot(gs[0, 0])
        ax_particles.set_title("Real-Time Particle Positions", fontsize=16, fontweight="bold")
        ax_particles.set_xlabel("x (pixels)", fontsize=14)
        ax_particles.set_ylabel("y (pixels)", fontsize=14)
        ax_particles.set_xlim(0, 1152)
        ax_particles.set_ylim(0, 1152)
        ax_particles.set_aspect("equal")
        ax_particles.tick_params(labelsize=12)
        ax_particles.grid(False, which="both")
        self.ax_particles = ax_particles

        self.scatter = ax_particles.scatter([], [], s=16, alpha=0.8, edgecolors="none")

        # g(r) plot (top-right and middle-right)
        ax_gr = fig.add_subplot(gs[0:2, 1])
        ax_gr.set_title("Pair Correlation Function g(r)", fontsize=16, fontweight="bold")
        ax_gr.set_xlabel(r"r ($\sigma$)", fontsize=14)
        ax_gr.set_ylabel("g(r)", fontsize=14)
        ax_gr.axhline(y=1, color="white", linestyle="--", linewidth=2, alpha=0.5, label="g(r) = 1")
        ax_gr.tick_params(labelsize=12)
        ax_gr.grid(True, which="both", linestyle=":", linewidth=0.7, alpha=0.4)

        self.line_gr, = ax_gr.plot([], [], color="cyan", linewidth=3, label="Average g(r)")
        ax_gr.legend(loc="upper right", fontsize=12)
        self.ax_gr = ax_gr
        self.fill_gr = None

        # Statistics panel (middle-left)
        self.ax_stats = fig.add_subplot(gs[1, 0])
        self.ax_stats.axis("off")
        self.stats_text = self.ax_stats.text(
            0.05, 0.95, "", transform=self.ax_stats.transAxes,
            fontsize=12, verticalalignment="top", fontfamily="monospace",
            bbox=dict(boxstyle="round", facecolor="black", alpha=0.8, edgecolor="cyan", linewidth=2)
        )

        # Info panel (bottom)
        ax_info = fig.add_subplot(gs[2, :])
        ax_info.axis("off")
        info_text = (
            f"Live g(r) Visualizer • Connected to {self.server_address}\n"
            f"Averaging window: {self.buffer.history_length} frames • "
            f"Update rate: {1000/self.update_interval_ms:.1f} Hz • "
            f"Pixel to σ: {self.gr_calculator.pixel_to_sigma:.1f}\n"
            f"Mass > {self.mass_cutoff:.1f} (red): 0 • Mass ≤ {self.mass_cutoff:.1f} (blue): 0 • Total: 0\n"
            "Ratios >cutoff:≤cutoff = -- : --"
        )
        self.ax_info = ax_info
        self.info_text_artist = ax_info.text(
            0.5, 0.5, info_text, transform=ax_info.transAxes,
            fontsize=11, ha="center", va="center",
            bbox=dict(boxstyle="round", facecolor="darkblue", alpha=0.6, edgecolor="cyan", linewidth=1.5)
        )

        fig.set_facecolor("#0a0a0a")
        
    def update_plot(self, frame: int) -> Tuple:
        """Animation update function."""
        # Fetch and process new data
        if self.connected:
            data = self.fetch_latest_tracks()
            if data:
                self.process_tracking_data(data)
        
        # Update particle scatter plot with mass-based coloring
        df_particles = self.buffer.particle_data
        total_count = int(len(df_particles))
        high_count = 0
        low_count = 0
        valid_count = 0
        high_ratio = 0.0
        low_ratio = 0.0
        invalid_count = 0
        if self.scatter is not None:
            if total_count > 0:
                positions = df_particles[["x", "y"]].to_numpy(dtype=float, copy=False)
            else:
                positions = np.empty((0, 2), dtype=float)
            self.scatter.set_offsets(positions)

            if total_count > 0 and "mass" in df_particles:
                masses = df_particles["mass"].to_numpy(dtype=float, copy=False)
                valid_mask = np.isfinite(masses)
                valid_count = int(np.count_nonzero(valid_mask))
                invalid_count = total_count - valid_count
                colors = np.full(total_count, "royalblue", dtype=object)
                if valid_count > 0:
                    high_mask = np.zeros(total_count, dtype=bool)
                    high_mask[valid_mask] = masses[valid_mask] > self.mass_cutoff
                    high_count = int(np.count_nonzero(high_mask))
                    low_count = valid_count - high_count
                    if valid_count > 0:
                        high_ratio = max(0.0, min(1.0, high_count / valid_count))
                        low_ratio = max(0.0, min(1.0, 1.0 - high_ratio))
                    colors[high_mask] = "red"
                    colors[valid_mask & ~high_mask] = "royalblue"
                else:
                    colors = np.full(total_count, "#808080", dtype=object)
                if invalid_count > 0:
                    colors[~valid_mask] = "#808080"
                self.scatter.set_facecolors(colors.tolist())
            else:
                if total_count > 0:
                    colors = np.full(total_count, "royalblue", dtype=object)
                    self.scatter.set_facecolors(colors.tolist())
                else:
                    self.scatter.set_facecolors([])
        
        # Update g(r) plot with error bands
        avg_r, avg_gr = self.buffer.get_averaged_gr()
        if len(avg_r) > 0 and len(avg_gr) > 0 and self.line_gr is not None and self.ax_gr is not None:
            avg_r = np.asarray(avg_r, dtype=float)
            avg_gr = np.asarray(avg_gr, dtype=float)
            self.line_gr.set_data(avg_r, avg_gr)
            self.ax_gr.relim()
            self.ax_gr.autoscale_view()

            # Add std deviation bands
            std_gr = np.asarray(self.buffer.get_std_gr(), dtype=float)
            if len(std_gr) > 0:
                if self.fill_gr is not None:
                    self.fill_gr.remove()
                    self.fill_gr = None
                lower = (avg_gr - std_gr).tolist()
                upper = (avg_gr + std_gr).tolist()
                x_vals = avg_r.tolist()
                self.fill_gr = self.ax_gr.fill_between(
                    x_vals,
                    lower,
                    upper,
                    alpha=0.2,
                    color="cyan",
                    label="±1 std"
                )
        
        # Update statistics
        if self.stats_text is not None:
            if self.connected:
                uptime = time.time() - self.connection_start_time
                fps = self.buffer.frame_count / uptime if uptime > 0 else 0

                stats_str = (
                    f"╔═══════════════════════════╗\n"
                    f"║    TRACKING STATISTICS    ║\n"
                    f"╠═══════════════════════════╣\n"
                    f"║ Particles: {self.buffer.particle_count:>14} ║\n"
                    f"║ Frames:    {self.buffer.frame_count:>14} ║\n"
                    f"║ FPS:       {fps:>14.2f} ║\n"
                    f"║ Uptime:    {uptime:>11.1f} s ║\n"
                    f"║ Status:    {'Connected':>14} ║\n"
                    f"╚═══════════════════════════╝"
                )
                self.stats_text.set_text(stats_str)
                self.stats_text.set_color("lime")
            else:
                self.stats_text.set_text(
                    "╔═══════════════════════════╗\n"
                    "║        DISCONNECTED        ║\n"
                    "╚═══════════════════════════╝"
                )
                self.stats_text.set_color("red")

        # Update mass summary panel
        if self.info_text_artist is not None:
            ratio_line = (
                f"Ratios >cutoff:≤cutoff = {high_ratio:.2f} : {low_ratio:.2f}"
                if valid_count > 0
                else "Ratios >cutoff:≤cutoff = -- : --"
            )
            info_lines = [
                f"Live g(r) Visualizer • Connected to {self.server_address}",
                (
                    f"Averaging window: {self.buffer.history_length} frames • "
                    f"Update rate: {1000/self.update_interval_ms:.1f} Hz • "
                    f"Pixel to σ: {self.gr_calculator.pixel_to_sigma:.1f}"
                ),
                (
                    f"Mass > {self.mass_cutoff:.1f} (red): {high_count} • "
                    f"Mass ≤ {self.mass_cutoff:.1f} (blue): {low_count} • Total: {total_count}"
                ),
                ratio_line,
            ]
            if invalid_count > 0:
                info_lines.append(f"Mass unavailable: {invalid_count}")
            self.info_text_artist.set_text("\n".join(info_lines))
        
        return self.scatter, self.line_gr, self.stats_text
    
    def run(self) -> None:
        """Start the visualization."""
        if not self.connect():
            print("Failed to connect. Exiting.")
            return
        
        try:
            self.setup_plot()
            if self.fig is None:
                print("Plot initialization failed. Exiting.")
                return
            
            ani = FuncAnimation(
                self.fig,
                self.update_plot,
                interval=self.update_interval_ms,
                blit=False,
                cache_frame_data=False,
            )
            
            plt.show()
            
        except KeyboardInterrupt:
            print("\nShutdown requested...")
        finally:
            self.disconnect()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Live g(r) visualizer for particle tracking data",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        type=str,
        default="localhost:50052",
        help="Image server address (host:port)",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=20,
        help="Number of frames to average for g(r)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=100,
        help="Update interval in milliseconds",
    )
    parser.add_argument(
        "--pixel-to-sigma",
        type=float,
        default=16,
        help="Conversion factor from pixels to particle diameter σ",
    )
    parser.add_argument(
        "--mass-cutoff",
        type=float,
        default=25000.0,
        help="Mass threshold; values above are shown in red, otherwise blue",
    )
    parser.add_argument(
        "--style",
        type=str,
        default="dark_background",
        choices=["dark_background", "seaborn", "ggplot", "bmh"],
        help="Matplotlib style",
    )
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    print("╔═══════════════════════════════════════════════╗")
    print("║   Live g(r) Pair Correlation Visualizer      ║")
    print("╚═══════════════════════════════════════════════╝")
    print()
    
    visualizer = LiveGrVisualizer(
        server_address=args.server,
        history_length=args.history,
        update_interval_ms=args.interval,
        pixel_to_sigma=args.pixel_to_sigma,
        mass_cutoff=args.mass_cutoff,
        style=args.style,
    )
    
    visualizer.run()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
