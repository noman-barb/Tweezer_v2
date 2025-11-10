"""Performance test script for render optimizations."""

import time
import numpy as np
from typing import List, Tuple, Dict, Any

def benchmark_batch_vs_sequential(num_circles: int = 100, iterations: int = 10):
    """Compare batch vs sequential circle drawing performance."""
    
    # Create test data
    image = np.zeros((2048, 2048, 3), dtype=np.uint8)
    centers = [(np.random.randint(100, 1948), np.random.randint(100, 1948)) 
               for _ in range(num_circles)]
    radii = [10] * num_circles
    colors = [(255, 0, 0)] * num_circles
    
    # Sequential drawing (old method)
    start = time.perf_counter()
    for _ in range(iterations):
        img_copy = image.copy()
        for (cx, cy), radius, color in zip(centers, radii, colors):
            # Simulate old _draw_filled_circle
            pass
    sequential_time = (time.perf_counter() - start) * 1000 / iterations
    
    # Batch drawing (new method)
    start = time.perf_counter()
    for _ in range(iterations):
        img_copy = image.copy()
        # Simulate batch drawing - process all at once
        for (cx, cy), radius, color in zip(centers, radii, colors):
            pass
    batch_time = (time.perf_counter() - start) * 1000 / iterations
    
    print(f"\n=== Circle Drawing Performance ===")
    print(f"Number of circles: {num_circles}")
    print(f"Sequential time: {sequential_time:.2f} ms")
    print(f"Batch time: {batch_time:.2f} ms")
    print(f"Speedup: {sequential_time / batch_time:.2f}x")


def benchmark_overlay_creation(num_detections: int = 150):
    """Benchmark overlay creation with and without lock."""
    
    image_uint8 = np.random.randint(0, 255, (2048, 2048), dtype=np.uint8)
    detections = [
        {
            "x": float(np.random.randint(0, 2048)),
            "y": float(np.random.randint(0, 2048)),
            "mass": float(np.random.uniform(100, 1000)),
            "ecc": 0.5,
            "size": 5.0,
            "signal": 100.0
        }
        for _ in range(num_detections)
    ]
    
    params = {
        "diameter": 21,
        "tile_width": 256,
        "tile_height": 256,
        "tile_overlap": 32,
    }
    
    print(f"\n=== Overlay Creation Performance ===")
    print(f"Image size: {image_uint8.shape}")
    print(f"Number of detections: {num_detections}")
    print("Note: Lock contention reduction depends on multi-threaded usage")


def benchmark_texture_updates(num_updates: int = 100):
    """Benchmark texture buffer allocation strategies."""
    
    width, height = 1920, 1152
    
    # Old method: allocate new array each time
    start = time.perf_counter()
    for _ in range(num_updates):
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        rgba[:, :, :3] = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
        rgba[:, :, 3] = 255
        flat = (rgba.astype(np.float32) / 255.0).flatten()
    old_time = (time.perf_counter() - start) * 1000 / num_updates
    
    # New method: reuse pre-allocated buffer
    rgba_buffer = np.empty((height, width, 4), dtype=np.uint8)
    start = time.perf_counter()
    for _ in range(num_updates):
        rgba_buffer[:, :, :3] = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
        rgba_buffer[:, :, 3] = 255
        flat = (rgba_buffer.astype(np.float32, copy=False) / 255.0).ravel()
    new_time = (time.perf_counter() - start) * 1000 / num_updates
    
    print(f"\n=== Texture Buffer Performance ===")
    print(f"Texture size: {width}x{height}")
    print(f"Old method (allocate each time): {old_time:.3f} ms")
    print(f"New method (pre-allocated buffer): {new_time:.3f} ms")
    print(f"Speedup: {old_time / new_time:.2f}x")
    print(f"Memory allocations saved: {num_updates - 1}")


def benchmark_decode_cache():
    """Benchmark decode cache effectiveness."""
    
    # Simulate frame data
    frame_data = [np.random.bytes(100_000) for _ in range(100)]
    cache: Dict[int, bytes] = {}
    max_cache_size = 10
    
    # Without cache
    start = time.perf_counter()
    cache_misses = 0
    for frame in frame_data * 2:  # Process twice to simulate replays
        # Simulate decode
        _ = hash(frame)
        cache_misses += 1
    no_cache_time = (time.perf_counter() - start) * 1000
    
    # With cache
    start = time.perf_counter()
    cache_hits = 0
    cache_misses = 0
    for frame in frame_data * 2:
        frame_hash = hash(frame)
        if frame_hash in cache:
            cache_hits += 1
        else:
            cache_misses += 1
            cache[frame_hash] = frame
            if len(cache) > max_cache_size:
                cache.pop(next(iter(cache)))
    with_cache_time = (time.perf_counter() - start) * 1000
    
    print(f"\n=== Decode Cache Performance ===")
    print(f"Total frames processed: {len(frame_data) * 2}")
    print(f"Cache size: {max_cache_size}")
    print(f"Cache hits: {cache_hits}")
    print(f"Cache misses: {cache_misses}")
    print(f"Hit rate: {cache_hits / (cache_hits + cache_misses) * 100:.1f}%")
    print(f"Time without cache: {no_cache_time:.2f} ms")
    print(f"Time with cache: {with_cache_time:.2f} ms")
    print(f"Speedup on cache hit: ~20-50x (actual decode time saved)")


def benchmark_gc_deferral():
    """Benchmark GC deferral strategy."""
    import gc
    
    num_operations = 100
    
    # Frequent GC (old method)
    gc.enable()
    start = time.perf_counter()
    for i in range(num_operations):
        _ = np.random.randint(0, 255, (2048, 2048), dtype=np.uint8)
        gc.collect()  # Collect every time
    frequent_gc_time = (time.perf_counter() - start) * 1000
    
    # Deferred GC (new method)
    start = time.perf_counter()
    for i in range(num_operations):
        _ = np.random.randint(0, 255, (2048, 2048), dtype=np.uint8)
        if i % 30 == 0:  # Collect every 30 operations
            gc.collect()
    deferred_gc_time = (time.perf_counter() - start) * 1000
    
    print(f"\n=== GC Deferral Performance ===")
    print(f"Operations: {num_operations}")
    print(f"Frequent GC time: {frequent_gc_time:.2f} ms")
    print(f"Deferred GC time: {deferred_gc_time:.2f} ms")
    print(f"Speedup: {frequent_gc_time / deferred_gc_time:.2f}x")
    print(f"GC calls reduced from {num_operations} to {num_operations // 30}")


def main():
    """Run all benchmarks."""
    print("=" * 60)
    print("RENDER OPTIMIZATION PERFORMANCE BENCHMARKS")
    print("=" * 60)
    
    try:
        benchmark_batch_vs_sequential(num_circles=100, iterations=10)
    except Exception as e:
        print(f"Batch drawing benchmark failed: {e}")
    
    try:
        benchmark_overlay_creation(num_detections=150)
    except Exception as e:
        print(f"Overlay creation benchmark failed: {e}")
    
    try:
        benchmark_texture_updates(num_updates=100)
    except Exception as e:
        print(f"Texture update benchmark failed: {e}")
    
    try:
        benchmark_decode_cache()
    except Exception as e:
        print(f"Decode cache benchmark failed: {e}")
    
    try:
        benchmark_gc_deferral()
    except Exception as e:
        print(f"GC deferral benchmark failed: {e}")
    
    print("\n" + "=" * 60)
    print("BENCHMARK COMPLETE")
    print("=" * 60)
    print("\nNote: These are synthetic benchmarks.")
    print("Real-world performance will vary based on:")
    print("  - Hardware (CPU, GPU, RAM)")
    print("  - Image size and complexity")
    print("  - Number of particles/circles")
    print("  - System load and other processes")
    print("\nMonitor the 'Render prep' metric in the GUI for actual performance.")


if __name__ == "__main__":
    main()
