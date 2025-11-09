#!/usr/bin/env python3
"""
Test script for live-gr visualizer components
Run this to verify your installation and test individual components
"""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

def test_imports():
    """Test that all required packages are available."""
    print("Testing imports...")
    
    required = [
        ('numpy', 'np'),
        ('pandas', 'pd'),
        ('matplotlib.pyplot', 'plt'),
        ('trackpy', 'tp'),
        ('scipy.spatial', None),
        ('grpc', None),
        ('google.protobuf', None),
    ]
    
    failed = []
    for module, alias in required:
        try:
            if alias:
                exec(f"import {module} as {alias}")
            else:
                exec(f"import {module}")
            print(f"  ✓ {module}")
        except ImportError as e:
            print(f"  ✗ {module}: {e}")
            failed.append(module)
    
    if failed:
        print(f"\n❌ Missing packages: {', '.join(failed)}")
        print("Install with: pip install " + " ".join(failed))
        return False
    
    print("✓ All imports successful\n")
    return True


def test_calculator():
    """Test GrCalculator component."""
    print("Testing GrCalculator...")
    
    try:
        from live_gr import GrCalculator
        import pandas as pd
        import numpy as np
        
        calc = GrCalculator(cutoff=100, dr=1.0, pixel_to_sigma=32.0)
        
        # Create test data: grid of points
        x = np.repeat(np.arange(0, 100, 10), 10)
        y = np.tile(np.arange(0, 100, 10), 10)
        df = pd.DataFrame({'x': x, 'y': y})
        
        r, gr = calc.compute(df)
        
        if len(r) > 0 and len(gr) > 0:
            print(f"  ✓ Computed g(r) with {len(r)} bins")
            print(f"    r range: {r[0]:.2f} - {r[-1]:.2f} σ")
            print(f"    g(r) range: {gr.min():.2f} - {gr.max():.2f}")
        else:
            print("  ✗ g(r) calculation returned empty arrays")
            return False
        
        print("✓ GrCalculator working\n")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def test_buffer():
    """Test TrackingDataBuffer component."""
    print("Testing TrackingDataBuffer...")
    
    try:
        from live_gr import TrackingDataBuffer
        import pandas as pd
        import numpy as np
        
        buffer = TrackingDataBuffer(history_length=5)
        
        # Add some test data
        for i in range(10):
            x = np.random.rand(50) * 100
            y = np.random.rand(50) * 100
            df = pd.DataFrame({'x': x, 'y': y})
            
            r = np.linspace(0, 3, 50)
            gr = 1 + np.exp(-r)  # Fake g(r)
            
            buffer.update(df, r, gr)
        
        avg_r, avg_gr = buffer.get_averaged_gr()
        std_gr = buffer.get_std_gr()
        
        print(f"  ✓ Buffer stored {len(buffer.gr_history)} frames")
        print(f"  ✓ Average g(r) has {len(avg_r)} points")
        print(f"  ✓ Std deviation computed: {len(std_gr)} points")
        print(f"  ✓ Particle count: {buffer.particle_count}")
        print(f"  ✓ Frame count: {buffer.frame_count}")
        
        print("✓ TrackingDataBuffer working\n")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def test_grpc_connection(server_address="localhost:50052"):
    """Test connection to ImageServer."""
    print(f"Testing gRPC connection to {server_address}...")
    
    try:
        import grpc
        from google.protobuf import empty_pb2
        
        # Add Camera to path for imports
        camera_path = Path(__file__).resolve().parents[2] / "Camera"
        if str(camera_path) not in sys.path:
            sys.path.insert(0, str(camera_path))
        
        from image_exchange_pb2_grpc import ImageExchangeStub
        
        channel = grpc.insecure_channel(
            server_address,
            options=[
                ("grpc.max_receive_message_length", 16 * 1024 * 1024),
            ],
        )
        stub = ImageExchangeStub(channel)
        
        # Try to get tracking config
        response = stub.GetTrackingConfig(empty_pb2.Empty(), timeout=5.0)
        
        print(f"  ✓ Connected successfully")
        print(f"  ✓ Server responded with tracking config")
        
        # Try to get latest tracks
        tracks = stub.GetLatestTracks(empty_pb2.Empty(), timeout=2.0)
        tracks_dict = dict(tracks)
        
        if tracks_dict.get('has_tracks', False):
            count = tracks_dict.get('detection_count', 0)
            print(f"  ✓ Tracking active: {count} particles detected")
        else:
            print("  ℹ Server running but no tracks available yet")
        
        channel.close()
        
        print("✓ gRPC connection working\n")
        return True
        
    except grpc.RpcError as e:
        print(f"  ✗ gRPC Error: {e}")
        print("  ℹ Make sure ImageServer is running")
        return False
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def test_utils():
    """Test utility functions."""
    print("Testing utility functions...")
    
    try:
        from utils import calculate_structure_metrics
        import numpy as np
        
        # Create fake g(r) with a peak
        r = np.linspace(0.1, 5, 100)
        gr = 2.0 * np.exp(-(r - 1.0)**2 / 0.1) + 1.0
        
        metrics = calculate_structure_metrics(r, gr)
        
        print(f"  ✓ First peak position: {metrics.get('first_peak_position', 'N/A')}")
        print(f"  ✓ First peak height: {metrics.get('first_peak_height', 'N/A')}")
        print(f"  ✓ Mean g(r): {metrics.get('mean_gr', 'N/A'):.3f}")
        
        print("✓ Utility functions working\n")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def test_matplotlib():
    """Test matplotlib can create plots."""
    print("Testing matplotlib...")
    
    try:
        import matplotlib
        matplotlib.use('Agg')  # Non-interactive backend
        import matplotlib.pyplot as plt
        import numpy as np
        
        plt.style.use('dark_background')
        
        fig, ax = plt.subplots(figsize=(8, 6))
        x = np.linspace(0, 10, 100)
        y = np.sin(x)
        ax.plot(x, y)
        ax.set_title("Test Plot")
        
        # Try to save
        test_file = Path(__file__).parent / "test_plot.png"
        plt.savefig(test_file)
        plt.close()
        
        if test_file.exists():
            print(f"  ✓ Created test plot: {test_file}")
            test_file.unlink()  # Clean up
        else:
            print("  ✗ Failed to save plot")
            return False
        
        print("✓ Matplotlib working\n")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def main():
    """Run all tests."""
    print("="*60)
    print("Live g(r) Visualizer - Component Tests")
    print("="*60)
    print()
    
    results = {
        'Imports': test_imports(),
        'Matplotlib': test_matplotlib(),
        'GrCalculator': test_calculator(),
        'TrackingDataBuffer': test_buffer(),
        'Utility Functions': test_utils(),
    }
    
    # Optional: test connection if requested
    if '--test-connection' in sys.argv:
        server = 'localhost:50052'
        for arg in sys.argv:
            if arg.startswith('--server='):
                server = arg.split('=', 1)[1]
        results['gRPC Connection'] = test_grpc_connection(server)
    else:
        print("ℹ Skipping gRPC connection test (use --test-connection to enable)")
        print()
    
    # Summary
    print("="*60)
    print("Test Summary")
    print("="*60)
    
    passed = sum(results.values())
    total = len(results)
    
    for name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status:8} {name}")
    
    print()
    print(f"Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n✅ All tests passed! Your installation is ready.")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed. Check the output above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
