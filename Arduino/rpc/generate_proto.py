"""Generate Python gRPC code from protobuf definitions."""

import subprocess
import sys
from pathlib import Path


def main():
    """Generate protobuf and gRPC Python files."""
    script_dir = Path(__file__).resolve().parent
    proto_dir = script_dir / "proto"
    output_dir = script_dir
    
    if not proto_dir.exists():
        print(f"Error: Proto directory not found: {proto_dir}")
        return 1
    
    proto_file = proto_dir / "due_streaming.proto"
    if not proto_file.exists():
        print(f"Error: Proto file not found: {proto_file}")
        return 1
    
    print(f"Generating Python code from {proto_file}...")
    print(f"Output directory: {output_dir}")
    
    try:
        # Generate Python protobuf and gRPC code
        cmd = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{proto_dir}",
            f"--python_out={output_dir}",
            f"--grpc_python_out={output_dir}",
            str(proto_file),
        ]
        
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        
        if result.stdout:
            print(result.stdout)
        
        print("✓ Successfully generated:")
        print(f"  - {output_dir / 'due_streaming_pb2.py'}")
        print(f"  - {output_dir / 'due_streaming_pb2_grpc.py'}")
        
        return 0
        
    except subprocess.CalledProcessError as exc:
        print(f"Error running protoc: {exc}")
        if exc.stderr:
            print(f"stderr: {exc.stderr}")
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
