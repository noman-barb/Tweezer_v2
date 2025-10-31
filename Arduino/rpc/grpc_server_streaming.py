"""Streaming gRPC client for Arduino Due bridge with reduced latency."""

from __future__ import annotations

import argparse
import queue
import threading
import time
from typing import Any, Callable, Dict, Iterator, Optional

import grpc

import due_streaming_pb2
import due_streaming_pb2_grpc


def _encode_variant(value: Any) -> due_streaming_pb2.Variant:
    """Convert Python value to protobuf Variant."""
    variant = due_streaming_pb2.Variant()
    if value is None:
        variant.null_value = due_streaming_pb2.NullValue.NULL_VALUE
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
            items.append(_encode_variant(item))
        return variant
    if isinstance(value, dict):
        fields = variant.struct_value.fields
        for key, item in value.items():
            fields[str(key)].CopyFrom(_encode_variant(item))
        return variant
    raise TypeError(f"Unsupported value type: {type(value)!r}")


def _decode_variant(variant: due_streaming_pb2.Variant) -> Any:
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
        return [_decode_variant(item) for item in variant.list_value.items]
    if kind == "struct_value":
        return {key: _decode_variant(val) for key, val in variant.struct_value.fields.items()}
    if kind == "null_value":
        return None
    return None


class DueStreamingClient:
    """Streaming gRPC client for Arduino Due bridge."""
    
    def __init__(self, target: str, *, timeout: float = 5.0):
        self._target = target
        self._timeout = timeout
        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[due_streaming_pb2_grpc.DueStreamingStub] = None
        self._request_queue: queue.Queue = queue.Queue()
        self._response_handlers: Dict[int, queue.Queue] = {}
        self._next_request_id = 1
        self._request_id_lock = threading.Lock()
        self._stream_thread: Optional[threading.Thread] = None
        self._telemetry_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False
        self._telemetry_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None

    def connect(self) -> None:
        """Connect to the streaming server."""
        if self._connected:
            return
        
        self._channel = grpc.insecure_channel(
            self._target,
            options=[
                ('grpc.max_send_message_length', 50 * 1024 * 1024),
                ('grpc.max_receive_message_length', 50 * 1024 * 1024),
                ('grpc.keepalive_time_ms', 10000),
                ('grpc.keepalive_timeout_ms', 5000),
                ('grpc.keepalive_permit_without_calls', 1),
                ('grpc.http2.max_pings_without_data', 0),
            ]
        )
        self._stub = due_streaming_pb2_grpc.DueStreamingStub(self._channel)
        self._stop_event.clear()
        
        # Start command streaming thread
        self._stream_thread = threading.Thread(target=self._stream_commands, daemon=True)
        self._stream_thread.start()
        
        # Start telemetry streaming thread
        self._telemetry_thread = threading.Thread(target=self._stream_telemetry, daemon=True)
        self._telemetry_thread.start()
        
        self._connected = True

    def shutdown(self) -> None:
        """Shutdown the client and close connections."""
        if not self._connected:
            return
        
        self._stop_event.set()
        
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)
        
        if self._telemetry_thread and self._telemetry_thread.is_alive():
            self._telemetry_thread.join(timeout=2.0)
        
        if self._channel:
            self._channel.close()
        
        self._connected = False
        self._channel = None
        self._stub = None

    def set_telemetry_callback(self, callback: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
        """Set callback for telemetry updates."""
        self._telemetry_callback = callback

    def _get_next_request_id(self) -> int:
        """Get next request ID in thread-safe manner."""
        with self._request_id_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            return request_id

    def _request_iterator(self) -> Iterator[due_streaming_pb2.StreamRequest]:
        """Generate request stream."""
        while not self._stop_event.is_set():
            try:
                request = self._request_queue.get(timeout=0.1)
                yield request
            except queue.Empty:
                continue

    def _stream_commands(self) -> None:
        """Background thread for streaming commands and receiving responses."""
        try:
            if not self._stub:
                return
            
            response_stream = self._stub.StreamCommands(self._request_iterator())
            
            for response in response_stream:
                request_id = response.request_id
                handler_queue = self._response_handlers.get(request_id)
                if handler_queue:
                    handler_queue.put(response)
                    del self._response_handlers[request_id]
        except grpc.RpcError as exc:
            if not self._stop_event.is_set():
                print(f"Stream error: {exc}")
        except Exception as exc:
            if not self._stop_event.is_set():
                print(f"Unexpected stream error: {exc}")

    def _telemetry_request_iterator(self) -> Iterator[due_streaming_pb2.StreamRequest]:
        """Generate empty requests for telemetry stream (for keepalive)."""
        while not self._stop_event.is_set():
            time.sleep(1.0)
            # Send empty keepalive request
            yield due_streaming_pb2.StreamRequest()

    def _stream_telemetry(self) -> None:
        """Background thread for receiving telemetry updates."""
        try:
            if not self._stub:
                return
            
            telemetry_stream = self._stub.StreamTelemetry(self._telemetry_request_iterator())
            
            for update in telemetry_stream:
                if self._telemetry_callback:
                    measurements = {}
                    for name, variant in update.measurements.items():
                        measurements[name] = _decode_variant(variant)
                    self._telemetry_callback(update.timestamp, measurements)
        except grpc.RpcError as exc:
            if not self._stop_event.is_set():
                print(f"Telemetry stream error: {exc}")
        except Exception as exc:
            if not self._stop_event.is_set():
                print(f"Unexpected telemetry stream error: {exc}")

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call a method on the Due bridge."""
        if not self._connected:
            raise RuntimeError("Client not connected")
        
        request_id = self._get_next_request_id()
        request = due_streaming_pb2.StreamRequest(method=method, request_id=request_id)
        request.args.extend(_encode_variant(arg) for arg in args)
        for key, value in kwargs.items():
            request.kwargs[str(key)].CopyFrom(_encode_variant(value))
        
        # Create response queue for this request
        response_queue: queue.Queue = queue.Queue()
        self._response_handlers[request_id] = response_queue
        
        # Send request
        self._request_queue.put(request)
        
        # Wait for response
        try:
            response = response_queue.get(timeout=self._timeout)
            if response.error:
                raise RuntimeError(f"Remote error: {response.error}")
            return _decode_variant(response.result)
        except queue.Empty:
            # Cleanup handler on timeout
            self._response_handlers.pop(request_id, None)
            raise TimeoutError(f"Request {request_id} timed out")

    # Convenience methods for common operations
    
    def close(self) -> Any:
        return self.call("close")

    def set_voltage_mode(self, enabled: bool) -> Any:
        return self.call("set_voltage_mode", enabled)

    def set_vref(self, adc_vref: Optional[float] = None, dac_vref: Optional[float] = None) -> Any:
        return self.call("set_vref", adc_vref, dac_vref)

    def flush(self) -> Any:
        return self.call("flush")

    def digital_write(self, pin: int, state: int) -> Any:
        return self.call("digital_write", pin, state)

    def digital_read(self, pin: int) -> Any:
        return self.call("digital_read", pin)

    def analog_read_raw(self, pin: int) -> Any:
        return self.call("analog_read_raw", pin)

    def analog_write_raw(self, pin: int, value: int) -> Any:
        return self.call("analog_write_raw", pin, value)

    def analog_read(self, pin: int) -> Any:
        return self.call("analog_read", pin)

    def analog_write(self, pin: int, value: Any) -> Any:
        return self.call("analog_write", pin, value)

    def batch_write_22_29(self, mask: int) -> Any:
        return self.call("batch_write_22_29", mask)

    def i2c_begin(self, bus: int, khz: int) -> Any:
        return self.call("i2c_begin", bus, khz)

    def i2c_write(self, bus: int, addr: int, data: bytes) -> Any:
        return self.call("i2c_write", bus, addr, data)

    def i2c_read(self, bus: int, addr: int, n: int) -> Any:
        return self.call("i2c_read", bus, addr, n)

    def spi_begin(self, mode: int = 0, lsb_first: bool = False, clk_div_code: int = 0) -> Any:
        return self.call("spi_begin", mode, lsb_first, clk_div_code)

    def spi_transfer(self, cs_pin: int, data: bytes) -> Any:
        return self.call("spi_transfer", cs_pin, data)

    def lcd_init(self, addr: int = 0x27, cols: int = 16, rows: int = 2) -> Any:
        return self.call("lcd_init", addr, cols, rows)

    def lcd_print(self, col: int, row: int, text: str) -> Any:
        return self.call("lcd_print", col, row, text)

    def lcd_clear(self) -> Any:
        return self.call("lcd_clear")

    def servo_attach(self, ch: int, pin: int) -> Any:
        return self.call("servo_attach", ch, pin)

    def servo_write(self, ch: int, angle: int) -> Any:
        return self.call("servo_write", ch, angle)

    def servo_detach(self, ch: int) -> Any:
        return self.call("servo_detach", ch)

    def tone(self, pin: int, freq_hz: int, duration_ms: int = 0) -> Any:
        return self.call("tone", pin, freq_hz, duration_ms)

    def no_tone(self, pin: int = 0) -> Any:
        return self.call("no_tone", pin)

    def pwm_resolution(self, bits: int) -> Any:
        return self.call("pwm_resolution", bits)

    def pwm_write(self, pin: int, value: int) -> Any:
        return self.call("pwm_write", pin, value)

    def uart_begin(self, port: int, baud: int) -> Any:
        return self.call("uart_begin", port, baud)

    def uart_write(self, port: int, data: bytes) -> Any:
        return self.call("uart_write", port, data)

    def uart_read(self, port: int, n: int) -> Any:
        return self.call("uart_read", port, n)

    def int_attach(self, slot: int, pin: int, mode: str = "CHANGE") -> Any:
        return self.call("int_attach", slot, pin, mode)

    def int_detach(self, slot: int) -> Any:
        return self.call("int_detach", slot)

    def int_query(self, slot: int) -> Any:
        return self.call("int_query", slot)

    def timer_start(self, period_us: int) -> Any:
        return self.call("timer_start", period_us)

    def timer_stop(self) -> Any:
        return self.call("timer_stop")

    def timer_count(self) -> Any:
        return self.call("timer_count")

    def adc_resolution(self, bits: int) -> Any:
        return self.call("adc_resolution", bits)


def main() -> None:
    """Example usage of streaming client."""
    parser = argparse.ArgumentParser(description="Call Due streaming gRPC procedures")
    parser.add_argument("target", help="gRPC target, e.g. localhost:50052")
    parser.add_argument("pin", type=int, help="Analog pin to read")
    parser.add_argument("--count", type=int, default=10, help="Number of reads")
    args = parser.parse_args()
    
    client = DueStreamingClient(args.target)
    
    def telemetry_handler(timestamp: str, measurements: Dict[str, Any]) -> None:
        print(f"[{timestamp}] Telemetry: {measurements}")
    
    try:
        client.connect()
        client.set_telemetry_callback(telemetry_handler)
        
        print(f"Reading pin {args.pin} {args.count} times...")
        for i in range(args.count):
            value = client.analog_read(args.pin)
            print(f"Read {i+1}/{args.count}: Pin {args.pin} -> {value}")
            time.sleep(0.1)
        
        # Wait a bit for telemetry
        time.sleep(2.0)
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()