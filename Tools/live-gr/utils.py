#!/usr/bin/env python3
"""
Utility functions for live-gr visualizer
Includes data export, analysis helpers, and custom colormaps
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def save_gr_data(
    r: np.ndarray,
    gr: np.ndarray,
    output_path: Path,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Save g(r) data to CSV file with metadata.
    
    Args:
        r: Distance array
        gr: Correlation function values
        output_path: Output file path
        metadata: Optional metadata dictionary
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Write metadata header
        if metadata:
            writer.writerow(['# Metadata'])
            for key, value in metadata.items():
                writer.writerow([f'# {key}: {value}'])
            writer.writerow(['#'])
        
        # Write data header
        writer.writerow(['r_sigma', 'g_r'])
        
        # Write data
        for r_val, gr_val in zip(r, gr):
            writer.writerow([f'{r_val:.6f}', f'{gr_val:.6f}'])
    
    print(f"Saved g(r) data to {output_path}")


def export_session_data(
    buffer: Any,
    output_dir: Path,
    session_name: Optional[str] = None,
) -> Dict[str, Path]:
    """
    Export all data from current session.
    
    Args:
        buffer: TrackingDataBuffer instance
        output_dir: Output directory
        session_name: Optional session name (default: timestamp)
    
    Returns:
        Dictionary mapping data type to file path
    """
    if session_name is None:
        session_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    output_dir = output_dir / session_name
    output_dir.mkdir(parents=True, exist_ok=True)
    
    saved_files = {}
    
    # Save particle positions
    if not buffer.particle_data.empty:
        particles_path = output_dir / "particles.csv"
        buffer.particle_data.to_csv(particles_path, index=False)
        saved_files['particles'] = particles_path
    
    # Save averaged g(r)
    avg_r, avg_gr = buffer.get_averaged_gr()
    if len(avg_r) > 0:
        gr_path = output_dir / "gr_averaged.csv"
        metadata = {
            'timestamp': datetime.now().isoformat(),
            'particle_count': buffer.particle_count,
            'frame_count': buffer.frame_count,
            'history_length': buffer.history_length,
        }
        save_gr_data(avg_r, avg_gr, gr_path, metadata)
        saved_files['gr_averaged'] = gr_path
    
    # Save all g(r) history
    if buffer.gr_history:
        history_path = output_dir / "gr_history.json"
        history_data = {
            'frames': [
                {
                    'r': r.tolist(),
                    'gr': gr.tolist(),
                }
                for r, gr in buffer.gr_history
            ],
            'metadata': {
                'frame_count': len(buffer.gr_history),
                'timestamp': datetime.now().isoformat(),
            }
        }
        with open(history_path, 'w') as f:
            json.dump(history_data, f, indent=2)
        saved_files['gr_history'] = history_path
    
    # Save session metadata
    metadata_path = output_dir / "session_metadata.json"
    session_metadata = {
        'session_name': session_name,
        'timestamp': datetime.now().isoformat(),
        'particle_count': buffer.particle_count,
        'frame_count': buffer.frame_count,
        'last_update': buffer.last_update_time,
    }
    with open(metadata_path, 'w') as f:
        json.dump(session_metadata, f, indent=2)
    saved_files['metadata'] = metadata_path
    
    print(f"\n✓ Session data exported to {output_dir}")
    for data_type, path in saved_files.items():
        print(f"  - {data_type}: {path.name}")
    
    return saved_files


def calculate_structure_metrics(r: np.ndarray, gr: np.ndarray) -> Dict[str, float]:
    """
    Calculate structural metrics from g(r).
    
    Args:
        r: Distance array
        gr: Correlation function values
    
    Returns:
        Dictionary of metrics
    """
    metrics = {}
    
    if len(r) == 0 or len(gr) == 0:
        return metrics
    
    # First peak position (nearest neighbor distance)
    if len(gr) > 1:
        first_peak_idx = np.argmax(gr)
        metrics['first_peak_position'] = float(r[first_peak_idx])
        metrics['first_peak_height'] = float(gr[first_peak_idx])
    
    # Coordination number (integral of first peak)
    # Approximate first minimum
    if len(gr) > 2:
        for i in range(1, len(gr) - 1):
            if gr[i] < gr[i-1] and gr[i] < gr[i+1]:
                first_min_idx = i
                break
        else:
            first_min_idx = len(gr) // 2
        
        # Integrate 2π r * g(r) * ρ dr up to first minimum
        # Assuming normalized density, approximate coordination
        r_integration = r[:first_min_idx]
        gr_integration = gr[:first_min_idx]
        
        if len(r_integration) > 1:
            # Trapezoidal integration
            integrand = 2 * np.pi * r_integration * gr_integration
            coordination = np.trapz(integrand, r_integration)
            metrics['coordination_number'] = float(coordination)
    
    # Long-range correlation
    if len(gr) > 10:
        long_range_mean = np.mean(gr[-10:])
        metrics['long_range_value'] = float(long_range_mean)
        metrics['long_range_deviation'] = float(abs(long_range_mean - 1.0))
    
    # Overall structure factor
    metrics['mean_gr'] = float(np.mean(gr))
    metrics['std_gr'] = float(np.std(gr))
    
    return metrics


def print_structure_report(metrics: Dict[str, float]) -> None:
    """Print a formatted structure analysis report."""
    if not metrics:
        print("No structural metrics available")
        return
    
    print("\n" + "="*50)
    print("STRUCTURAL ANALYSIS REPORT")
    print("="*50)
    
    if 'first_peak_position' in metrics:
        print(f"\nNearest Neighbor Distance:")
        print(f"  Position: {metrics['first_peak_position']:.3f} σ")
        print(f"  Height:   {metrics['first_peak_height']:.3f}")
    
    if 'coordination_number' in metrics:
        print(f"\nCoordination Number: {metrics['coordination_number']:.2f}")
    
    if 'long_range_value' in metrics:
        print(f"\nLong-Range Correlation:")
        print(f"  Value:     {metrics['long_range_value']:.4f}")
        print(f"  Deviation: {metrics['long_range_deviation']:.4f}")
    
    print(f"\nOverall Statistics:")
    print(f"  Mean g(r): {metrics.get('mean_gr', 0):.4f}")
    print(f"  Std g(r):  {metrics.get('std_gr', 0):.4f}")
    
    print("="*50 + "\n")


def detect_phase_transition(
    gr_history: List[Tuple[np.ndarray, np.ndarray]],
    window: int = 10,
) -> Dict[str, Any]:
    """
    Detect potential phase transitions from g(r) time series.
    
    Args:
        gr_history: List of (r, gr) tuples over time
        window: Window size for change detection
    
    Returns:
        Dictionary with transition information
    """
    if len(gr_history) < window * 2:
        return {'detected': False, 'reason': 'Insufficient history'}
    
    # Extract first peak positions over time
    peak_positions = []
    peak_heights = []
    
    for r, gr in gr_history:
        if len(gr) > 1:
            peak_idx = np.argmax(gr)
            peak_positions.append(r[peak_idx])
            peak_heights.append(gr[peak_idx])
    
    if len(peak_positions) < window * 2:
        return {'detected': False, 'reason': 'Insufficient peaks'}
    
    peak_positions = np.array(peak_positions)
    peak_heights = np.array(peak_heights)
    
    # Calculate moving statistics
    recent_pos_mean = np.mean(peak_positions[-window:])
    older_pos_mean = np.mean(peak_positions[-2*window:-window])
    pos_change = abs(recent_pos_mean - older_pos_mean)
    
    recent_height_mean = np.mean(peak_heights[-window:])
    older_height_mean = np.mean(peak_heights[-2*window:-window])
    height_change = abs(recent_height_mean - older_height_mean)
    
    # Thresholds for significant change
    pos_threshold = 0.1  # 0.1 σ change
    height_threshold = 0.3  # 30% change
    
    transition_detected = (
        pos_change > pos_threshold or
        height_change > height_threshold * older_height_mean
    )
    
    return {
        'detected': transition_detected,
        'position_change': float(pos_change),
        'height_change': float(height_change),
        'recent_peak_position': float(recent_pos_mean),
        'recent_peak_height': float(recent_height_mean),
        'change_percentage': float(height_change / older_height_mean * 100 if older_height_mean > 0 else 0),
    }


def create_comparison_plot(
    datasets: List[Tuple[str, np.ndarray, np.ndarray]],
    output_path: Path,
) -> None:
    """
    Create a comparison plot of multiple g(r) datasets.
    
    Args:
        datasets: List of (label, r, gr) tuples
        output_path: Output file path
    """
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 6))
    
    colors = cm.viridis(np.linspace(0, 0.9, len(datasets)))
    
    for (label, r, gr), color in zip(datasets, colors):
        ax.plot(r, gr, label=label, linewidth=2, color=color)
    
    ax.axhline(y=1, color='white', linestyle='--', linewidth=1, alpha=0.5)
    ax.set_xlabel(r'r ($\sigma$)', fontsize=14)
    ax.set_ylabel('g(r)', fontsize=14)
    ax.set_title('g(r) Comparison', fontsize=16, fontweight='bold')
    ax.legend(loc='best', fontsize=12)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, facecolor='black')
    print(f"Saved comparison plot to {output_path}")
    plt.close()


if __name__ == "__main__":
    print("This is a utility module. Import it in your scripts.")
    print("\nAvailable functions:")
    print("  - save_gr_data()")
    print("  - export_session_data()")
    print("  - calculate_structure_metrics()")
    print("  - print_structure_report()")
    print("  - detect_phase_transition()")
    print("  - create_comparison_plot()")
