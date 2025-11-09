#!/usr/bin/env python3
"""
Example usage of live-gr with data export and analysis

This script demonstrates:
1. Connecting to the tracking server
2. Collecting data for a specified duration
3. Exporting data to files
4. Performing structural analysis
5. Creating comparison plots
"""

import argparse
import sys
import time
from pathlib import Path

# Add live-gr to path
sys.path.insert(0, str(Path(__file__).parent))

from live_gr import LiveGrVisualizer, GrCalculator, TrackingDataBuffer
from utils import (
    export_session_data,
    calculate_structure_metrics,
    print_structure_report,
    detect_phase_transition,
    save_gr_data,
)


def collect_data_session(
    server: str,
    duration_seconds: int,
    output_dir: Path,
    session_name: str = None,
) -> None:
    """
    Collect tracking data for specified duration and export results.
    
    Args:
        server: Server address
        duration_seconds: Collection duration in seconds
        output_dir: Output directory for exported data
        session_name: Optional session name
    """
    print(f"\n{'='*60}")
    print(f"Starting data collection session")
    print(f"Server: {server}")
    print(f"Duration: {duration_seconds} seconds")
    print(f"Output: {output_dir}")
    print(f"{'='*60}\n")
    
    # Initialize visualizer (headless mode possible)
    visualizer = LiveGrVisualizer(
        server_address=server,
        history_length=30,
        update_interval_ms=100,
    )
    
    if not visualizer.connect():
        print("Failed to connect to server!")
        return
    
    print("✓ Connected. Collecting data...\n")
    
    start_time = time.time()
    frame_count = 0
    
    try:
        while time.time() - start_time < duration_seconds:
            # Fetch data
            data = visualizer.fetch_latest_tracks()
            if data:
                visualizer.process_tracking_data(data)
                frame_count += 1
                
                # Progress update every 5 seconds
                elapsed = time.time() - start_time
                if int(elapsed) % 5 == 0 and elapsed > 0:
                    remaining = duration_seconds - elapsed
                    print(f"Progress: {elapsed:.1f}s / {duration_seconds}s "
                          f"(Frames: {frame_count}, Particles: {visualizer.buffer.particle_count})")
            
            time.sleep(0.1)
    
    except KeyboardInterrupt:
        print("\nCollection interrupted by user")
    
    finally:
        elapsed = time.time() - start_time
        print(f"\n✓ Collection complete: {elapsed:.1f}s, {frame_count} frames")
        
        # Export data
        print("\nExporting data...")
        saved_files = export_session_data(
            visualizer.buffer,
            output_dir,
            session_name,
        )
        
        # Calculate and display structural metrics
        avg_r, avg_gr = visualizer.buffer.get_averaged_gr()
        if len(avg_r) > 0:
            metrics = calculate_structure_metrics(avg_r, avg_gr)
            print_structure_report(metrics)
            
            # Save metrics
            import json
            metrics_path = saved_files.get('metadata').parent / "structure_metrics.json"
            with open(metrics_path, 'w') as f:
                json.dump(metrics, f, indent=2)
            print(f"✓ Saved structure metrics to {metrics_path}")
        
        # Check for phase transitions
        if len(visualizer.buffer.gr_history) > 20:
            transition_info = detect_phase_transition(
                list(visualizer.buffer.gr_history),
                window=10,
            )
            
            if transition_info['detected']:
                print("\n" + "!"*60)
                print("POTENTIAL PHASE TRANSITION DETECTED!")
                print("!"*60)
                print(f"Position change: {transition_info['position_change']:.4f} σ")
                print(f"Height change: {transition_info['change_percentage']:.2f}%")
                print("="*60 + "\n")
        
        visualizer.disconnect()


def compare_sessions(
    session_dirs: list[Path],
    output_path: Path,
) -> None:
    """
    Compare g(r) from multiple sessions.
    
    Args:
        session_dirs: List of session directories to compare
        output_path: Output path for comparison plot
    """
    from utils import create_comparison_plot
    import pandas as pd
    
    datasets = []
    
    for session_dir in session_dirs:
        gr_file = session_dir / "gr_averaged.csv"
        if not gr_file.exists():
            print(f"Warning: {gr_file} not found, skipping")
            continue
        
        df = pd.read_csv(gr_file, comment='#')
        r = df['r_sigma'].values
        gr = df['g_r'].values
        
        label = session_dir.name
        datasets.append((label, r, gr))
    
    if len(datasets) < 2:
        print("Need at least 2 sessions to compare")
        return
    
    create_comparison_plot(datasets, output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Data collection and analysis for live-gr",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # Collect command
    collect_parser = subparsers.add_parser('collect', help='Collect data for specified duration')
    collect_parser.add_argument('--server', type=str, default='localhost:50052', help='Server address')
    collect_parser.add_argument('--duration', type=int, default=60, help='Duration in seconds')
    collect_parser.add_argument('--output', type=Path, default=Path('./data'), help='Output directory')
    collect_parser.add_argument('--name', type=str, default=None, help='Session name')
    
    # Compare command
    compare_parser = subparsers.add_parser('compare', help='Compare multiple sessions')
    compare_parser.add_argument('sessions', nargs='+', type=Path, help='Session directories to compare')
    compare_parser.add_argument('--output', type=Path, default=Path('./comparison.png'), help='Output plot path')
    
    args = parser.parse_args()
    
    if args.command == 'collect':
        collect_data_session(
            server=args.server,
            duration_seconds=args.duration,
            output_dir=args.output,
            session_name=args.name,
        )
    
    elif args.command == 'compare':
        compare_sessions(args.sessions, args.output)
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
