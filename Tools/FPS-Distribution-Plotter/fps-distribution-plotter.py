import argparse
import os
import sys
from pathlib import Path
from multiprocessing import Pool, cpu_count
import numpy as np
from typing import List, Tuple

#!/usr/bin/env python3
"""
FPS Distribution Plotter - Analyzes file creation time deltas and plots distribution.
Optimized for Linux with multiprocessing support.
"""

import matplotlib.pyplot as plt


def get_file_ctime(filepath: Path) -> float:
    """Get file creation time (ctime on Linux)."""
    return filepath.stat().st_ctime


def process_batch(filepaths: List[Path]) -> List[float]:
    """Process a batch of files and return their creation times."""
    times = []
    for filepath in filepaths:
        try:
            times.append(get_file_ctime(filepath))
        except (OSError, PermissionError):
            continue
    return times


def chunk_list(lst: List, n: int) -> List[List]:
    """Divide list into n roughly equal chunks."""
    k, m = divmod(len(lst), n)
    return [lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]


def collect_files(directory: Path, extension: str) -> List[Path]:
    """Collect all files with given extension from directory."""
    pattern = f"*.{extension.lstrip('.')}"
    return list(directory.glob(pattern))


def main():
    parser = argparse.ArgumentParser(
        description="Analyze file creation time deltas and plot distribution"
    )
    parser.add_argument("directory", type=str, help="Directory containing files")
    parser.add_argument("extension", type=str, help="File extension to analyze")
    parser.add_argument(
        "-p", "--processes",
        type=int,
        default=32,
        help="Number of processes to use (default: 32)"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="fps_distribution.png",
        help="Output plot filename (default: fps_distribution.png)"
    )
    
    args = parser.parse_args()
    
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Error: {args.directory} is not a valid directory", file=sys.stderr)
        sys.exit(1)
    
    print(f"Collecting files with extension '.{args.extension}'...")
    files = collect_files(directory, args.extension)
    
    if len(files) == 0:
        print(f"Error: No files found with extension '.{args.extension}'", file=sys.stderr)
        sys.exit(1)
    
    print(f"Found {len(files)} files. Processing with {args.processes} processes...")
    
    # Split files into chunks for multiprocessing
    chunks = chunk_list(files, args.processes)
    
    # Process in parallel
    with Pool(processes=args.processes) as pool:
        results = pool.map(process_batch, chunks)
    
    # Flatten results and sort times
    all_times = sorted([t for batch in results for t in batch])
    
    if len(all_times) < 2:
        print("Error: Need at least 2 valid files to compute deltas", file=sys.stderr)
        sys.exit(1)
    
    print(f"Successfully processed {len(all_times)} files")
    
    # Calculate deltas (time differences between consecutive files)
    deltas = np.diff(all_times)
    
    # Calculate statistics
    mean_delta = np.mean(deltas)
    std_delta = np.std(deltas)
    median_delta = np.median(deltas)
    fps = 1.0 / mean_delta if mean_delta > 0 else 0
    
    print(f"\nStatistics:")
    print(f"  Mean delta: {mean_delta:.6f} seconds ({fps:.2f} FPS)")
    print(f"  Std delta:  {std_delta:.6f} seconds")
    print(f"  Median delta: {median_delta:.6f} seconds")
    print(f"  Min delta:  {np.min(deltas):.6f} seconds")
    print(f"  Max delta:  {np.max(deltas):.6f} seconds")
    
    # Plot distribution
    plt.figure(figsize=(12, 6))
    
    plt.subplot(1, 2, 1)
    plt.hist(deltas, bins=100, edgecolor='black', alpha=0.7)
    plt.axvline(mean_delta, color='r', linestyle='--', label=f'Mean: {mean_delta:.6f}s')
    plt.axvline(median_delta, color='g', linestyle='--', label=f'Median: {median_delta:.6f}s')
    plt.xlabel('Time Delta (seconds)')
    plt.ylabel('Frequency')
    plt.title('Distribution of File Creation Time Deltas')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    plt.subplot(1, 2, 2)
    fps_values = 1.0 / deltas[deltas > 0]
    plt.hist(fps_values, bins=100, edgecolor='black', alpha=0.7)
    plt.axvline(fps, color='r', linestyle='--', label=f'Mean FPS: {fps:.2f}')
    plt.xlabel('FPS (Frames Per Second)')
    plt.ylabel('Frequency')
    plt.title('Distribution of FPS')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(args.output, dpi=300)
    print(f"\nPlot saved to: {args.output}")
    plt.show()


if __name__ == "__main__":
    main()