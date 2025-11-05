"""HTTP server that exposes Arduino Due telemetry data via gRPC streaming.

This server:
1. Reads services_config.yaml to get Arduino gRPC server endpoint
2. Connects to grpc_server_streaming.py via grpc_client_streaming.py
3. Monitors all telemetry data from the Arduino Due
4. Exposes HTTP GET endpoint (no-cache) at http://0.0.0.0:9002/telemetry
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# Add Arduino RPC path for gRPC client
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARDUINO_RPC_PATH = _REPO_ROOT / "Arduino" / "rpc"
if str(_ARDUINO_RPC_PATH) not in sys.path:
    sys.path.insert(0, str(_ARDUINO_RPC_PATH))

from grpc_client_streaming import DueStreamingClient  # type: ignore  # noqa: E402


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class TelemetryManager:
    """Manages connection to Arduino Due gRPC server and telemetry data."""
    
    def __init__(self, grpc_target: str):
        """Initialize the telemetry manager.
        
        Args:
            grpc_target: The gRPC target address (e.g., "localhost:50051")
        """
        self.grpc_target = grpc_target
        self.client: Optional[DueStreamingClient] = None
        self.connected = False
        self.telemetry_data: Dict[str, Any] = {}
        self.telemetry_lock = threading.Lock()
        self.last_update_timestamp: Optional[str] = None
        self.connection_error: Optional[str] = None
        
    def connect(self) -> None:
        """Connect to the Arduino Due gRPC server."""
        if self.connected:
            logger.warning("Already connected to gRPC server")
            return
        
        try:
            logger.info("Connecting to Arduino Due gRPC server at %s", self.grpc_target)
            # Only enable telemetry, not commands (to avoid conflicts with dashboard)
            self.client = DueStreamingClient(self.grpc_target, timeout=5.0, enable_commands=False, enable_telemetry=True)
            
            # Set up telemetry callback
            def telemetry_callback(timestamp: str, measurements: Dict[str, Any]) -> None:
                with self.telemetry_lock:
                    self.last_update_timestamp = timestamp
                    # Update telemetry data with all measurements
                    self.telemetry_data.update(measurements)
            
            self.client.set_telemetry_callback(telemetry_callback)
            self.client.connect()
            
            self.connected = True
            self.connection_error = None
            logger.info("Successfully connected to Arduino Due gRPC server")
            
        except Exception as exc:
            self.connection_error = str(exc)
            logger.exception("Failed to connect to gRPC server: %s", exc)
            raise
    
    def disconnect(self) -> None:
        """Disconnect from the gRPC server."""
        if not self.connected:
            return
        
        try:
            if self.client:
                self.client.shutdown()
                self.client = None
            
            self.connected = False
            logger.info("Disconnected from Arduino Due gRPC server")
            
        except Exception as exc:
            logger.exception("Error during disconnect: %s", exc)
        finally:
            with self.telemetry_lock:
                self.telemetry_data.clear()
                self.last_update_timestamp = None
    
    def get_telemetry_snapshot(self) -> Dict[str, Any]:
        """Get a snapshot of current telemetry data.
        
        Returns:
            Dictionary containing:
                - connected: Connection status
                - timestamp: Last update timestamp
                - measurements: All telemetry measurements
                - error: Connection error if any
        """
        with self.telemetry_lock:
            return {
                "connected": self.connected,
                "timestamp": self.last_update_timestamp,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "grpc_target": self.grpc_target,
                "measurements": dict(self.telemetry_data),
                "error": self.connection_error,
            }


class TelemetryHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for telemetry data."""
    
    # Class variable to hold the telemetry manager
    telemetry_manager: Optional[TelemetryManager] = None
    
    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/telemetry" or self.path == "/":
            self._serve_telemetry()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_error(404, "Not Found")
    
    def _serve_telemetry(self) -> None:
        """Serve telemetry data as JSON."""
        try:
            if self.telemetry_manager is None:
                self.send_error(500, "Telemetry manager not initialized")
                return
            
            # Get telemetry snapshot
            data = self.telemetry_manager.get_telemetry_snapshot()
            
            # Send response
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            # Disable caching
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            # CORS headers
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            
            # Write JSON response
            response_json = json.dumps(data, indent=2, default=str)
            self.wfile.write(response_json.encode("utf-8"))
            
        except Exception as exc:
            logger.exception("Error serving telemetry: %s", exc)
            self.send_error(500, f"Internal Server Error: {exc}")
    
    def _serve_health(self) -> None:
        """Serve health check endpoint."""
        try:
            if self.telemetry_manager is None:
                self.send_error(500, "Telemetry manager not initialized")
                return
            
            health_data = {
                "status": "ok" if self.telemetry_manager.connected else "disconnected",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            
            response_json = json.dumps(health_data, indent=2)
            self.wfile.write(response_json.encode("utf-8"))
            
        except Exception as exc:
            logger.exception("Error serving health check: %s", exc)
            self.send_error(500, f"Internal Server Error: {exc}")
    
    def log_message(self, format: str, *args: Any) -> None:
        """Override to use our logger."""
        logger.info("%s - %s", self.address_string(), format % args)


def load_services_config(config_path: Path) -> Dict[str, Any]:
    """Load services configuration from YAML file.
    
    Args:
        config_path: Path to services_config.yaml
        
    Returns:
        Dictionary containing services configuration
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config
    except Exception as exc:
        logger.exception("Failed to load services config from %s: %s", config_path, exc)
        raise


def get_arduino_grpc_target(config: Dict[str, Any]) -> str:
    """Extract Arduino gRPC server target from services config.
    
    Args:
        config: Services configuration dictionary
        
    Returns:
        Target address in format "host:port"
    """
    services = config.get("services", {})
    arduino_config = services.get("arduino_grpc", {})
    
    if not arduino_config.get("enabled", False):
        raise ValueError("Arduino gRPC service is not enabled in config")
    
    args = arduino_config.get("args", {})
    host = args.get("host", "0.0.0.0")
    port = args.get("port", 50051)
    
    # Convert 0.0.0.0 to localhost for client connection
    if host in ("0.0.0.0", "[::]"):
        host = "localhost"
    
    target = f"{host}:{port}"
    logger.info("Arduino gRPC target from config: %s", target)
    return target


def run_http_server(
    telemetry_manager: TelemetryManager,
    host: str = "0.0.0.0",
    port: int = 9002,
) -> None:
    """Run the HTTP server.
    
    Args:
        telemetry_manager: The telemetry manager instance
        host: Host to bind to
        port: Port to bind to
    """
    # Set the telemetry manager as a class variable
    TelemetryHTTPHandler.telemetry_manager = telemetry_manager
    
    server_address = (host, port)
    httpd = HTTPServer(server_address, TelemetryHTTPHandler)
    
    logger.info("HTTP telemetry server listening on http://%s:%d", host, port)
    logger.info("Endpoints:")
    logger.info("  GET http://%s:%d/telemetry - Get all telemetry data (no cache)", host, port)
    logger.info("  GET http://%s:%d/health - Health check", host, port)
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down HTTP server...")
    finally:
        httpd.shutdown()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="HTTP server for Arduino Due telemetry via gRPC streaming"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent.parent / "services_config.yaml",
        help="Path to services_config.yaml (default: ../services_config.yaml)",
    )
    parser.add_argument(
        "--grpc-target",
        type=str,
        help="Override gRPC target (e.g., localhost:50051)",
    )
    parser.add_argument(
        "--http-host",
        type=str,
        default="0.0.0.0",
        help="HTTP server host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=9002,
        help="HTTP server port (default: 9002)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )
    
    args = parser.parse_args()
    
    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    
    # Get gRPC target
    if args.grpc_target:
        grpc_target = args.grpc_target
        logger.info("Using gRPC target from command line: %s", grpc_target)
    else:
        logger.info("Loading services config from: %s", args.config)
        config = load_services_config(args.config)
        grpc_target = get_arduino_grpc_target(config)
    
    # Create telemetry manager
    telemetry_manager = TelemetryManager(grpc_target)
    
    # Connect to gRPC server
    try:
        telemetry_manager.connect()
    except Exception as exc:
        logger.error("Failed to connect to gRPC server. Continuing anyway...")
        logger.error("Server will report disconnected status until connection succeeds")
    
    # Start HTTP server
    try:
        run_http_server(
            telemetry_manager,
            host=args.http_host,
            port=args.http_port,
        )
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        # Cleanup
        logger.info("Cleaning up...")
        telemetry_manager.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
