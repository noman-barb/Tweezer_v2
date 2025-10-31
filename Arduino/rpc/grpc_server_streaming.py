"""gRPC streaming server that exposes the Arduino Due bridge with reduced latency."""

from __future__ import annotations

import argparse
import importlib
import json
import csv
import logging
import queue
import sys
import threading
import time
from concurrent import futures
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast

import grpc

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from interface.due_bridge import Due, DueError  # noqa: E402

# Import the generated protobuf files
import due_streaming_pb2 as streaming_pb2  # noqa: E402
import due_streaming_pb2_grpc as streaming_pb2_grpc  # noqa: E402

_pb2 = cast(Any, streaming_pb2)
_pb2_grpc = cast(Any, streaming_pb2_grpc)

_PIN_CONFIG_PATH = _REPO_ROOT / "pin_config.json"
_LOG_DIRECTORY = _REPO_ROOT.parent / "logs" / "AutoLogs" / "Arduino_duo_server"
_LOG_SAMPLE_INTERVAL_SECONDS = 0.2
_LOG_WRITE_INTERVAL_SECONDS = 60.0
_TELEMETRY_BATCH_SIZE = 10


def _variant_from_value(value: Any) -> Any:
    """Convert Python value to protobuf Variant."""
    variant = _pb2.Variant()
    if value is None:
        variant.null_value = _pb2.NullValue.NULL_VALUE
        return variant
    if isinstance(value, bool):
        variant.bool_value = value
        return variant
    if isinstance(value, int) and not isinstance(value, bool):
        variant.int_value = value
        return variant
    if isinstance(value, float):
        variant.double_value = value
        return variant
    if isinstance(value, str):
        variant.string_value = value
        return variant
    if isinstance(value, (bytes, bytearray, memoryview)):
        variant.bytes_value = bytes(value)
        return variant
    if isinstance(value, (list, tuple)):
        items = variant.list_value.items
        for item in value:
            items.append(_variant_from_value(item))
        return variant
    if isinstance(value, dict):
        fields = variant.struct_value.fields
        for key, item in value.items():
            fields[str(key)].CopyFrom(_variant_from_value(item))
        return variant
    raise TypeError(f"Unsupported value type: {type(value)!r}")


def _value_from_variant(variant: Any) -> Any:
    """Convert protobuf Variant to Python value."""
    kind = variant.WhichOneof("kind")
    if kind == "bool_value":
        return variant.bool_value
    if kind == "int_value":
        return variant.int_value
    if kind == "double_value":
        return variant.double_value
    if kind == "string_value":
        return variant.string_value
    if kind == "bytes_value":
        return variant.bytes_value
    if kind == "list_value":
        return [_value_from_variant(item) for item in variant.list_value.items]
    if kind == "struct_value":
        return {key: _value_from_variant(val) for key, val in variant.struct_value.fields.items()}
    if kind == "null_value":
        return None
    return None


@dataclass(frozen=True)
class LogSpec:
    """Specification for a loggable channel."""
    name: str
    kind: str
    unit: Optional[str]
    conversion: Optional[float]
    min_value: Optional[float]
    max_value: Optional[float]
    log_default: bool
    pin: Optional[int] = None
    pin_label: Optional[str] = None
    sensor: Optional[str] = None
    bus: Optional[int] = None
    address: Optional[int] = None
    quantity: Optional[str] = None
    frequency_khz: Optional[int] = None


def _resolve_pin_identifier(identifier: str) -> int:
    """Convert pin identifier string to pin number."""
    ident = identifier.strip().upper()
    if ident.startswith("DAC"):
        suffix = ident[3:]
        if suffix == "0":
            return 66
        if suffix == "1":
            return 67
        raise ValueError(f"Unknown DAC pin identifier: {identifier!r}")
    if ident.startswith("A") and ident[1:].isdigit():
        return int(ident[1:])
    if ident.startswith("D") and ident[1:].isdigit():
        return int(ident[1:])
    if ident.isdigit():
        return int(ident)
    raise ValueError(f"Unsupported pin identifier: {identifier!r}")


def _load_pin_config() -> Dict[str, Any]:
    """Load pin configuration from JSON file."""
    if not _PIN_CONFIG_PATH.exists():
        logging.warning("Pin configuration file not found at %s", _PIN_CONFIG_PATH)
        return {}
    try:
        with _PIN_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        logging.exception("Failed to load pin configuration from %s", _PIN_CONFIG_PATH)
        return {}


def _build_log_specs() -> Dict[str, LogSpec]:
    """Build log specifications from pin configuration."""
    config = _load_pin_config()
    specs: Dict[str, LogSpec] = {}
    if not isinstance(config, dict):
        return specs
    for name, entry in config.items():
        if not isinstance(entry, dict):
            logging.warning("Skipping %s: configuration entry must be an object", name)
            continue
        log_default = bool(entry.get("log_default", True))
        if not log_default:
            continue
        sensor_type = entry.get("sensor")
        if sensor_type:
            sensor = str(sensor_type).upper()
            if sensor == "SHTC3":
                # Ensure bus, address, and frequency are integers
                try:
                    bus = int(entry.get("bus", 0))
                except (TypeError, ValueError):
                    bus = 0
                try:
                    address_val = entry.get("address", 0x70)
                    # Handle both hex strings and integers
                    if isinstance(address_val, str):
                        address = int(address_val, 0)  # Auto-detect base (hex if 0x prefix)
                    else:
                        address = int(address_val)
                except (TypeError, ValueError):
                    address = 0x70
                try:
                    frequency_khz = int(entry.get("frequency_khz", 400))
                except (TypeError, ValueError):
                    frequency_khz = 400
                
                specs[name] = LogSpec(
                    name=name,
                    kind="shtc3",
                    unit=entry.get("unit"),
                    conversion=None,
                    min_value=entry.get("min_value"),
                    max_value=entry.get("max_value"),
                    log_default=log_default,
                    sensor=sensor,
                    bus=bus,
                    address=address,
                    quantity=entry.get("quantity"),
                    frequency_khz=frequency_khz,
                )
            else:
                logging.warning("Unsupported sensor type %s for %s", sensor, name)
            continue
        pin_label = entry.get("pin")
        if not pin_label:
            logging.warning("Missing pin label for %s in pin configuration", name)
            continue
        try:
            pin_number = _resolve_pin_identifier(pin_label)
        except ValueError as exc:
            logging.warning("Skipping %s: %s", name, exc)
            continue
        kind = entry.get("kind", "analog_read")
        try:
            conversion = float(entry.get("conversion", 1.0))
        except (TypeError, ValueError):
            conversion = 1.0
        specs[name] = LogSpec(
            name=name,
            kind=kind,
            unit=entry.get("unit"),
            conversion=conversion,
            min_value=entry.get("min_value"),
            max_value=entry.get("max_value"),
            log_default=log_default,
            pin=pin_number,
            pin_label=pin_label,
        )
    return specs


class TelemetryLogger:
    """CSV telemetry logger that emits measurement snapshots at fixed intervals."""

    _FIELDNAMES = [
        "snapshot_timestamp",
        "measurement_timestamp",
        "name",
        "pin_label",
        "source",
        "status",
        "value",
        "unit",
        "volts",
        "raw",
        "min_value",
        "max_value",
        "quantity",
        "error",
        "metadata",
    ]

    def __init__(self, log_path: Path, interval_seconds: float = _LOG_WRITE_INTERVAL_SECONDS):
        self.path = log_path
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._latest: Optional[Tuple[str, List[Dict[str, Any]]]] = None
        self._next_flush = time.monotonic() + self.interval_seconds
        self._last_written_timestamp: Optional[str] = None
        self._file = self.path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self._FIELDNAMES)
        self._header_written = self.path.stat().st_size > 0
        if not self._header_written:
            self._writer.writeheader()
            self._file.flush()
            self._header_written = True

    def record_sample(self, timestamp: str, measurements: List[Dict[str, Any]]) -> None:
        if not measurements:
            return
        with self._lock:
            self._latest = (timestamp, measurements)
            now = time.monotonic()
            if now >= self._next_flush:
                self._write_latest_locked()
                self._next_flush = now + self.interval_seconds

    def close(self) -> None:
        with self._lock:
            self._write_latest_locked()
            self._file.close()

    def _write_latest_locked(self) -> None:
        if not self._latest:
            return
        timestamp, measurements = self._latest
        if timestamp == self._last_written_timestamp:
            return

        for measurement in sorted(measurements, key=lambda m: (m.get("name") or "")):
            row = self._format_row(timestamp, measurement)
            self._writer.writerow(row)

        self._file.flush()
        self._last_written_timestamp = timestamp

    def _format_row(self, snapshot_timestamp: str, measurement: Dict[str, Any]) -> Dict[str, str]:
        value = measurement.get("value")
        volts = measurement.get("volts")
        raw = measurement.get("raw")
        metadata = measurement.get("metadata")

        return {
            "snapshot_timestamp": snapshot_timestamp,
            "measurement_timestamp": measurement.get("updated_at") or "",
            "name": (measurement.get("name") or ""),
            "pin_label": (measurement.get("pin_label") or ""),
            "source": (measurement.get("source") or ""),
            "status": (measurement.get("status") or ""),
            "value": self._format_value(value) if value is not None else "",
            "unit": (measurement.get("unit") or ""),
            "volts": self._format_value(volts) if volts is not None else "",
            "raw": self._format_value(raw) if raw is not None else "",
            "min_value": self._format_value(measurement.get("min_value")),
            "max_value": self._format_value(measurement.get("max_value")),
            "quantity": (measurement.get("quantity") or ""),
            "error": (measurement.get("error") or ""),
            "metadata": json.dumps(metadata, ensure_ascii=False) if metadata else "",
        }

    @staticmethod
    def _format_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.6g}"
        if isinstance(value, (int, bool)):
            return str(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)


class DueStreamingService(Due, _pb2_grpc.DueStreamingServicer):
    """Streaming gRPC service for Arduino Due bridge."""
    
    _PUBLIC_METHODS = {
        name
        for name in dir(Due)
        if callable(getattr(Due, name)) and not name.startswith("_")
    }

    def __init__(self, port: str, *, baud: int = 2_000_000, timeout: float = 0.35):
        super().__init__(port=port, baud=baud, timeout=timeout)
        self._lock = threading.Lock()
        self._log_specs = _build_log_specs()
        self._output_state: Dict[int, Dict[str, Any]] = {}
        self._i2c_buses: Dict[int, int] = {}
        self._logger: Optional[TelemetryLogger] = None
        self._telemetry_queues: List[queue.Queue] = []
        self._telemetry_stop = threading.Event()
        self._telemetry_thread: Optional[threading.Thread] = None
        
        if self._log_specs:
            self._logger = self._create_logger()
            self._prepare_sensor_interfaces()
            self._telemetry_stop.clear()
            self._telemetry_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
            self._telemetry_thread.start()
            logging.info("Telemetry logging started")

    def close(self):
        """Shutdown the service."""
        self._telemetry_stop.set()
        if self._telemetry_thread and self._telemetry_thread.is_alive():
            self._telemetry_thread.join(timeout=2.0)
        logger = self._logger
        self._logger = None
        if logger:
            logger.close()
        with self._lock:
            super().close()

    def analog_write(self, pin: int, value: Any):
        """Override to track output state."""
        super().analog_write(pin, value)
        if self.voltage_mode:
            volts = float(value)
            raw = self._volts_to_raw(volts, self.dac_bits, self.vref_dac)
        else:
            raw = int(value)
            volts = self._raw_to_volts(raw, self.dac_bits, self.vref_dac)
        self._record_output(pin, volts, raw, method="analog_write")

    def analog_write_raw(self, pin: int, value: int):
        """Override to track output state."""
        super().analog_write_raw(pin, value)
        raw = int(value)
        volts = self._raw_to_volts(raw, self.dac_bits, self.vref_dac)
        self._record_output(pin, volts, raw, method="analog_write_raw")

    def StreamCommands(
        self, request_iterator: Iterator[Any], context: grpc.ServicerContext
    ) -> Iterator[Any]:
        """Handle bidirectional streaming of commands and responses."""
        logging.info("Client connected to StreamCommands")
        try:
            for request in request_iterator:
                method_name = request.method
                request_id = request.request_id
                
                response = _pb2.StreamResponse(request_id=request_id)
                
                if method_name not in self._PUBLIC_METHODS:
                    response.error = f"Unknown method: {method_name}"
                    yield response
                    continue
                
                target = getattr(self, method_name, None)
                if not callable(target):
                    response.error = f"Method not callable: {method_name}"
                    yield response
                    continue
                
                try:
                    args = [_value_from_variant(arg) for arg in request.args]
                    kwargs = {key: _value_from_variant(val) for key, val in request.kwargs.items()}
                    result = target(*args, **kwargs)
                    response.result.CopyFrom(_variant_from_value(result))
                except DueError as exc:
                    response.error = f"DueError: {exc}"
                except (TypeError, ValueError) as exc:
                    response.error = f"Invalid arguments: {exc}"
                except Exception as exc:
                    logging.exception("Unexpected error in %s", method_name)
                    response.error = f"Server error: {exc}"
                
                yield response
        except Exception as exc:
            logging.exception("Error in StreamCommands")
        finally:
            logging.info("Client disconnected from StreamCommands")

    def StreamTelemetry(
        self, request_iterator: Iterator[Any], context: grpc.ServicerContext
    ) -> Iterator[Any]:
        """Handle server-side streaming of telemetry data."""
        logging.info("Client connected to StreamTelemetry")
        telemetry_queue: queue.Queue = queue.Queue(maxsize=100)
        self._telemetry_queues.append(telemetry_queue)
        
        try:
            # Start a thread to consume requests (for backpressure/keepalive)
            def consume_requests():
                try:
                    for _ in request_iterator:
                        pass  # Just consume to detect disconnect
                except Exception:
                    pass
            
            request_thread = threading.Thread(target=consume_requests, daemon=True)
            request_thread.start()
            
            # Stream telemetry updates
            while not self._telemetry_stop.is_set() and context.is_active():
                try:
                    update = telemetry_queue.get(timeout=1.0)
                    yield update
                except queue.Empty:
                    continue
                    
        except Exception as exc:
            logging.exception("Error in StreamTelemetry")
        finally:
            if telemetry_queue in self._telemetry_queues:
                self._telemetry_queues.remove(telemetry_queue)
            logging.info("Client disconnected from StreamTelemetry")

    def _create_logger(self) -> Optional[TelemetryLogger]:
        """Create telemetry logger."""
        try:
            _LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        except OSError:
            logging.exception("Failed to create log directory %s", _LOG_DIRECTORY)
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = _LOG_DIRECTORY / f"due_metrics_{timestamp}.csv"
        try:
            return TelemetryLogger(log_path, interval_seconds=_LOG_WRITE_INTERVAL_SECONDS)
        except Exception:
            logging.exception("Failed to create telemetry logger")
            return None

    def _prepare_sensor_interfaces(self) -> None:
        """Initialize sensor interfaces."""
        sensor_specs = [spec for spec in self._log_specs.values() if spec.kind == "shtc3" and spec.bus is not None]
        if not sensor_specs:
            return
        with self._lock:
            for spec in sensor_specs:
                if spec.bus is None or spec.frequency_khz is None:
                    continue
                try:
                    self._ensure_i2c_bus(spec.bus, spec.frequency_khz)
                    logging.info("Initialized I2C bus %d at %d kHz for %s", spec.bus, spec.frequency_khz, spec.name)
                except Exception:
                    logging.exception("Failed to initialize I2C bus %d for %s", spec.bus, spec.name)

    def _ensure_i2c_bus(self, bus: int, khz: int) -> None:
        """Ensure I2C bus is initialized."""
        current = self._i2c_buses.get(bus)
        if current == khz:
            return
        super().i2c_begin(bus, khz)
        self._i2c_buses[bus] = khz

    def _telemetry_loop(self) -> None:
        """Background loop for collecting telemetry."""
        while not self._telemetry_stop.is_set():
            try:
                timestamp = datetime.now(timezone.utc).isoformat()
                measurements = self._collect_measurements(timestamp)
                
                if measurements and self._logger:
                    self._logger.record_sample(timestamp, measurements)
                
                # Broadcast to all connected clients
                if measurements and self._telemetry_queues:
                    update = _pb2.TelemetryUpdate(timestamp=timestamp)
                    for measurement in measurements:
                        name = measurement.get("name", "")
                        value = measurement.get("value")
                        if name and value is not None:
                            update.measurements[name].CopyFrom(_variant_from_value(value))
                    
                    for telemetry_queue in self._telemetry_queues[:]:
                        try:
                            telemetry_queue.put_nowait(update)
                        except queue.Full:
                            logging.warning("Telemetry queue full, dropping update")
                
                time.sleep(_LOG_SAMPLE_INTERVAL_SECONDS)
            except Exception:
                logging.exception("Error in telemetry loop")
                time.sleep(1.0)

    def _collect_measurements(self, timestamp: str) -> List[Dict[str, Any]]:
        """Collect measurements from all configured channels."""
        if not self._log_specs:
            return []
        measurements: List[Dict[str, Any]] = []
        sensor_cache: Dict[Tuple[Optional[str], Optional[int], Optional[int]], Any] = {}
        
        for spec in self._log_specs.values():
            try:
                if spec.kind == "shtc3" and spec.sensor and spec.bus is not None and spec.address is not None:
                    cache_key = (spec.sensor, spec.bus, spec.address)
                    if cache_key not in sensor_cache:
                        with self._lock:
                            temp_c, humidity = self._read_shtc3(spec.bus, spec.address)
                        sensor_cache[cache_key] = {"temperature": temp_c, "humidity": humidity}
                    
                    sensor_data = sensor_cache[cache_key]
                    quantity = spec.quantity or "temperature"
                    value = sensor_data.get(quantity)
                    
                    measurements.append(
                        self._build_measurement(
                            spec,
                            source="shtc3",
                            value=value,
                            unit=spec.unit,
                            updated_at=timestamp,
                        )
                    )
                elif spec.kind == "analog_read" and spec.pin is not None:
                    with self._lock:
                        volts = super().analog_read(spec.pin)
                    value, unit = self._value_from_voltage(spec, volts)
                    measurements.append(
                        self._build_measurement(
                            spec,
                            source="analog_read",
                            value=value,
                            unit=unit,
                            volts=volts,
                            updated_at=timestamp,
                        )
                    )
                elif spec.kind == "dac_pin" and spec.pin is not None:
                    state = self._output_state.get(spec.pin)
                    if state:
                        volts = state.get("volts")
                        value, unit = self._value_from_voltage(spec, volts)
                        measurements.append(
                            self._build_measurement(
                                spec,
                                source=state.get("source", "dac_pin"),
                                value=value,
                                unit=unit,
                                volts=volts,
                                raw=state.get("raw"),
                                updated_at=state.get("timestamp"),
                            )
                        )
            except Exception as exc:
                logging.exception("Error collecting measurement for %s", spec.name)
                measurements.append(
                    self._build_measurement(
                        spec,
                        source=spec.kind,
                        status="error",
                        error=str(exc),
                        updated_at=timestamp,
                    )
                )
        
        return measurements

    def _value_from_voltage(self, spec: LogSpec, volts: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
        """Convert voltage to physical value using conversion factor."""
        if volts is None:
            return None, spec.unit
        conversion = spec.conversion if spec.conversion is not None else 1.0
        value = volts * conversion
        return value, spec.unit

    def _build_measurement(
        self,
        spec: LogSpec,
        *,
        source: str,
        status: str = "ok",
        value: Optional[float] = None,
        unit: Optional[str] = None,
        volts: Optional[float] = None,
        raw: Optional[int] = None,
        updated_at: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a measurement dictionary."""
        meta: Dict[str, Any] = dict(metadata or {})
        if spec.sensor:
            meta["sensor"] = spec.sensor
            if spec.bus is not None:
                meta["bus"] = spec.bus
            if spec.address is not None:
                meta["address"] = spec.address
        
        measurement: Dict[str, Any] = {
            "name": spec.name,
            "pin_label": spec.pin_label,
            "source": source,
            "unit": unit or spec.unit,
            "value": value,
            "volts": volts,
            "raw": raw,
            "min_value": spec.min_value,
            "max_value": spec.max_value,
            "status": status,
            "error": error,
            "updated_at": updated_at,
            "quantity": spec.quantity,
            "metadata": meta or None,
        }
        return measurement

    def _read_shtc3(self, bus: int, address: int) -> Tuple[float, float]:
        """Read temperature and humidity from SHTC3 sensor."""
        super().i2c_write(bus, address, bytes([0x35, 0x17]))
        time.sleep(0.001)
        super().i2c_write(bus, address, bytes([0x78, 0x66]))
        time.sleep(0.020)
        data = super().i2c_read(bus, address, 6)
        raw_temp = (data[0] << 8) | data[1]
        raw_rh = (data[3] << 8) | data[4]
        temp_c = -45.0 + 175.0 * (raw_temp / 65535.0)
        humidity = 100.0 * (raw_rh / 65535.0)
        super().i2c_write(bus, address, bytes([0xB0, 0x98]))
        return temp_c, humidity

    def _record_output(self, pin: int, volts: float, raw: int, *, method: str) -> None:
        """Record output state for logging."""
        self._output_state[pin] = {
            "volts": volts,
            "raw": raw,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": method,
        }


def serve(
    serial_port: str,
    *,
    host: str = "[::]",
    port: int = 50052,
    max_workers: int = 4,
    baud: int = 2_000_000,
    timeout: float = 0.35,
    credentials: Optional[grpc.ServerCredentials] = None,
) -> None:
    """Start the streaming gRPC server."""
    service = DueStreamingService(serial_port, baud=baud, timeout=timeout)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            ('grpc.max_send_message_length', 50 * 1024 * 1024),
            ('grpc.max_receive_message_length', 50 * 1024 * 1024),
            ('grpc.keepalive_time_ms', 10000),
            ('grpc.keepalive_timeout_ms', 5000),
            ('grpc.keepalive_permit_without_calls', True),
            ('grpc.http2.max_pings_without_data', 0),
        ]
    )
    _pb2_grpc.add_DueStreamingServicer_to_server(service, server)
    listen_target = f"{host}:{port}"
    if credentials:
        server.add_secure_port(listen_target, credentials)
    else:
        server.add_insecure_port(listen_target)
    server.start()
    logging.info("Due streaming gRPC server listening on %s", listen_target)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logging.info("Due streaming gRPC server interrupted; shutting down")
        server.stop(grace=2.0).wait()
    finally:
        service.close()


def main(argv: Optional[List[str]] = None) -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Expose Arduino Due bridge via streaming gRPC")
    parser.add_argument("--serial_port", default="/dev/ttyACM0", help="Serial device path for the Arduino Due")
    parser.add_argument("--host", default="[::]", help="Bind address for the gRPC server")
    parser.add_argument("--port", type=int, default=50052, help="Port for the gRPC server")
    parser.add_argument("--baud", type=int, default=2_000_000, help="Baud rate for the serial port")
    parser.add_argument("--timeout", type=float, default=0.35, help="Serial read/write timeout in seconds")
    parser.add_argument(
        "--max-workers", type=int, default=4, help="Maximum worker threads for the gRPC server"
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    serve(
        args.serial_port,
        host=args.host,
        port=args.port,
        max_workers=args.max_workers,
        baud=args.baud,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
