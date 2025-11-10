"""WebSocket-driven Microscope XY Stage Controller."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, cast
from urllib.parse import unquote, urlparse

import ftd2xx  # type: ignore
import websockets


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class MicroscopeStageController:
    """Controls the microscope XY stage via FTDI serial interface."""

    STEP_STEPPER = 20000000
    STEP_MM = 97.66
    DEFAULT_MAX_SPEED_MM_S = 0.2
    ABSOLUTE_MAX_SPEED_MM_S = 0.2
    MOVEMENT_TICK_S = 0.05
    POSITION_REFRESH_S = 0.2
    MANUAL_POSITION_REFRESH_S = 0.75
    MANUAL_PROGRESS_TIMEOUT = 0.25
    MANUAL_TOLERANCE_RATIO = 0.25
    MAX_SAVED_POSITIONS = 20

    def __init__(self, device_index: int = 0) -> None:
        self.device_index = device_index
        self.device: Optional[ftd2xx.FTD2XX] = None
        self.steps_per_mm = self.STEP_STEPPER / self.STEP_MM

        self._device_lock = threading.Lock()
        self._position_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._current_x_steps: Optional[int] = None
        self._current_y_steps: Optional[int] = None
        self._is_moving = False
        self._movement_enabled = False
        self._manual_direction: Tuple[int, int] = (0, 0)
        self._residual_steps: List[float] = [0.0, 0.0]
        self._target_position_steps: Optional[Tuple[int, int]] = None
        self._max_speed_limit = self.DEFAULT_MAX_SPEED_MM_S
        self._requested_speed_mm_s = self.DEFAULT_MAX_SPEED_MM_S
        self._saved_positions: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
        self._light_state = False

        self._stop_event = threading.Event()
        self._movement_event = threading.Event()
        self._pending_manual_nudge = False
        self._last_nonzero_direction: Tuple[int, int] = (0, 0)
        self._movement_thread: Optional[threading.Thread] = None
        self._last_position_refresh = time.time()
        self._last_hardware_refresh = self._last_position_refresh
        self._last_direction_update = time.time()
        self._emergency_flag = False
        self._response_warning_ts: Dict[str, float] = {"x": 0.0, "y": 0.0}
        self._direction_timeout_s = 2.0  # Auto-stop if no direction update for 2 seconds

        logger.info(
            "MicroscopeStageController initialized (steps_per_mm=%.2f)",
            self.steps_per_mm,
        )

    def connect(self) -> None:
        try:
            device = cast(Any, ftd2xx.open(self.device_index))
            device.setBaudRate(19200)  # type: ignore[attr-defined]
            device.setFlowControl(0)  # type: ignore[attr-defined]
            device.setTimeouts(500, 500)  # type: ignore[attr-defined]
            self.device = device
            logger.info("Connected to FTDI device %s", self.device_index)
            self.update_position()
            self._ensure_movement_thread()
        except ftd2xx.ftd2xx.DeviceError as exc:
            logger.error("Failed to connect to FTDI device: %s", exc)
            raise

    def disconnect(self) -> None:
        self._stop_event.set()
        self._movement_event.set()
        if self._movement_thread and self._movement_thread.is_alive():
            self._movement_thread.join(timeout=1.5)
        if self.device:
            try:
                self.device.close()
                logger.info("FTDI device disconnected")
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error disconnecting FTDI device: %s", exc)
            finally:
                self.device = None

    def _ensure_movement_thread(self) -> None:
        if self._movement_thread and self._movement_thread.is_alive():
            return
        self._stop_event.clear()
        self._movement_thread = threading.Thread(
            target=self._movement_loop,
            name="StageMovementLoop",
            daemon=True,
        )
        self._movement_thread.start()
        self._signal_movement_loop()

    def _signal_movement_loop(self) -> None:
        self._movement_event.set()

    def _movement_loop(self) -> None:
        logger.info("Movement control loop started")
        tick = self.MOVEMENT_TICK_S
        while not self._stop_event.is_set():
            self._movement_event.wait(timeout=tick)
            self._movement_event.clear()
            loop_start = time.time()
            try:
                if self._emergency_flag:
                    self._handle_emergency_flag()
                moved = False
                manual_motion = False
                if self._movement_enabled:
                    dx_steps, dy_steps = self._compute_next_step(tick)
                    if dx_steps or dy_steps:
                        manual_motion = self._is_manual_motion()
                        moved = self._execute_step(dx_steps, dy_steps)
                now = time.time()
                if not moved and now - self._last_position_refresh >= self.POSITION_REFRESH_S:
                    self.update_position()
                elif manual_motion and now - self._last_hardware_refresh >= self.MANUAL_POSITION_REFRESH_S:
                    # Periodically resync hardware position during sustained manual commands.
                    self.update_position()
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Movement loop error: %s", exc, exc_info=True)
                time.sleep(0.5)
        logger.info("Movement control loop stopped")

    def _handle_emergency_flag(self) -> None:
        with self._state_lock:
            self._manual_direction = (0, 0)
            self._target_position_steps = None
            self._residual_steps = [0.0, 0.0]
            self._emergency_flag = False
            self._pending_manual_nudge = False
        self._halt_motion()
        self.update_position()

    def _halt_motion(self) -> None:
        if not self.device:
            return
        with self._position_lock:
            if self._current_x_steps is None or self._current_y_steps is None:
                return
            target_x = self._current_x_steps
            target_y = self._current_y_steps
        try:
            self._send_move_absolute(target_x, target_y)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to issue halt command: %s", exc)

    def _compute_next_step(self, tick: float) -> Tuple[int, int]:
        with self._state_lock:
            target = self._target_position_steps
            direction = self._manual_direction
            speed_mm_s = min(self._requested_speed_mm_s, self._max_speed_limit)
            last_update = self._last_direction_update

        # Safety: Auto-stop if manual direction hasn't been updated recently
        # This prevents runaway movement from stuck keys or lost WebSocket messages
        if direction != (0, 0) and target is None:
            time_since_update = time.time() - last_update
            if time_since_update > self._direction_timeout_s:
                logger.warning(
                    "Manual direction timeout (%.1fs since last update) - forcing stop",
                    time_since_update
                )
                with self._state_lock:
                    self._manual_direction = (0, 0)
                    self._residual_steps = [0.0, 0.0]
                return 0, 0

        if speed_mm_s <= 0:
            return 0, 0

        if target is not None:
            current_x, current_y = self._get_or_refresh_position()
            target_x, target_y = target
            dx_remaining = target_x - current_x
            dy_remaining = target_y - current_y
            steps_per_tick = max(1, int(self.steps_per_mm * speed_mm_s * tick))

            dx_steps = int(math.copysign(min(abs(dx_remaining), steps_per_tick), dx_remaining)) if dx_remaining else 0
            dy_steps = int(math.copysign(min(abs(dy_remaining), steps_per_tick), dy_remaining)) if dy_remaining else 0

            if dx_steps == 0 and dy_steps == 0:
                with self._state_lock:
                    self._target_position_steps = None
                return 0, 0
            return dx_steps, dy_steps

        dx, dy = direction
        if dx == 0 and dy == 0:
            with self._state_lock:
                if self._pending_manual_nudge and self._last_nonzero_direction != (0, 0):
                    dx, dy = self._last_nonzero_direction
                    self._pending_manual_nudge = False
                else:
                    return 0, 0
        else:
            with self._state_lock:
                self._pending_manual_nudge = False

        steps_per_tick_float = self.steps_per_mm * speed_mm_s * tick
        norm = math.hypot(dx, dy)
        if norm == 0:
            return 0, 0

        desired_x = dx * steps_per_tick_float / norm
        desired_y = dy * steps_per_tick_float / norm

        with self._state_lock:
            total_x = desired_x + self._residual_steps[0]
            total_y = desired_y + self._residual_steps[1]
            dx_steps = math.floor(total_x) if total_x >= 0 else math.ceil(total_x)
            dy_steps = math.floor(total_y) if total_y >= 0 else math.ceil(total_y)
            self._residual_steps[0] = total_x - dx_steps
            self._residual_steps[1] = total_y - dy_steps

        return dx_steps, dy_steps

    def _is_manual_motion(self) -> bool:
        with self._state_lock:
            return (
                self._target_position_steps is None
                and self._movement_enabled
                and self._manual_direction != (0, 0)
            )

    def _execute_step(self, dx_steps: int, dy_steps: int) -> bool:
        if dx_steps == 0 and dy_steps == 0:
            return False

        manual_mode = self._is_manual_motion()
        current_x, current_y = self._get_or_refresh_position()
        target_x = current_x + dx_steps
        target_y = current_y + dy_steps

        try:
            self._set_is_moving(True)
            self._send_move_absolute(target_x, target_y)
            if manual_mode:
                primary_step = max(abs(dx_steps), abs(dy_steps))
                tolerance = max(1, int(primary_step * self.MANUAL_TOLERANCE_RATIO))
                self._wait_for_position(
                    target_x,
                    target_y,
                    timeout=self.MANUAL_PROGRESS_TIMEOUT,
                    tolerance_steps=tolerance,
                )
            else:
                self._wait_for_position(target_x, target_y)
            self.update_position()
            return True
        finally:
            self._set_is_moving(False)

    def _wait_for_position(
        self,
        target_x: int,
        target_y: int,
        timeout: float = 1.0,
        tolerance_steps: int = 0,
    ) -> None:
        stop_time = time.time() + timeout
        while time.time() < stop_time and not self._stop_event.is_set():
            current_x, current_y = self._get_or_refresh_position(force_refresh=True)
            if (
                abs(current_x - target_x) <= tolerance_steps
                and abs(current_y - target_y) <= tolerance_steps
            ):
                return
            time.sleep(0.005 if tolerance_steps else 0.01)

    def _get_or_refresh_position(self, force_refresh: bool = False) -> Tuple[int, int]:
        if force_refresh:
            return self.update_position()
        with self._position_lock:
            x_steps = self._current_x_steps
            y_steps = self._current_y_steps
        if x_steps is None or y_steps is None:
            return self.update_position()
        return x_steps, y_steps

    def _set_is_moving(self, value: bool) -> None:
        with self._state_lock:
            self._is_moving = value

    def update_position(self) -> Tuple[int, int]:
        if not self.device:
            raise RuntimeError("Device not connected")
        try:
            response_x = self._send_command("72023\r")
            response_y = self._send_command("73023\r")
            with self._position_lock:
                prev_x = self._current_x_steps
                prev_y = self._current_y_steps
            x_steps = self._parse_position_response(response_x, axis="x", fallback=prev_x)
            y_steps = self._parse_position_response(response_y, axis="y", fallback=prev_y)
            with self._position_lock:
                self._current_x_steps = x_steps
                self._current_y_steps = y_steps
                now = time.time()
                self._last_position_refresh = now
                self._last_hardware_refresh = now
            return x_steps, y_steps
        except Exception as exc:
            logger.error("Error updating position: %s", exc)
            raise

    def _send_move_absolute(self, x_steps: int, y_steps: int) -> None:
        command_x = f"72022 {x_steps}\r"
        command_y = f"73022 {y_steps}\r"
        self._send_command(command_x)
        self._send_command(command_y)

    def _send_command(self, command: str) -> str:
        if not self.device:
            raise RuntimeError("Device not connected")
        with self._device_lock:
            self.device.write(command.encode("ascii"))
            start = time.time()
            response = b""
            while True:
                try:
                    queued = self.device.getQueueStatus()  # type: ignore[attr-defined]
                except Exception:
                    queued = 0
                if queued:
                    response = self.device.read(queued)
                    break
                if time.time() - start > 0.2:
                    response = self.device.read(1000)
                    break
                time.sleep(0.002)
        return response.decode("utf-8", errors="ignore").strip()

    def _parse_position_response(
        self,
        response: str,
        axis: str,
        fallback: Optional[int],
    ) -> int:
        parts = response.strip().split()
        if len(parts) < 2:
            return self._handle_position_parse_error(axis, response, fallback)
        try:
            return int(parts[1])
        except ValueError as exc:
            return self._handle_position_parse_error(axis, response, fallback, exc)

    def _handle_position_parse_error(
        self,
        axis: str,
        response: str,
        fallback: Optional[int],
        error: Optional[Exception] = None,
    ) -> int:
        if fallback is not None:
            now = time.time()
            last_warn = self._response_warning_ts.get(axis, 0.0)
            if now - last_warn > 1.0:
                logger.warning(
                    "Using cached %s-axis position after unexpected response: %r",
                    axis,
                    response,
                )
                self._response_warning_ts[axis] = now
            return fallback
        if error is None:
            raise ValueError(f"Unexpected {axis}-axis response with no fallback: {response!r}")
        raise ValueError(
            f"Invalid {axis}-axis position with no fallback available: {response!r}"
        ) from error

    def get_position(self) -> Dict[str, Any]:
        with self._position_lock, self._state_lock:
            x_steps = self._current_x_steps
            y_steps = self._current_y_steps
            is_moving = self._is_moving
        if x_steps is None or y_steps is None:
            return {
                "x_steps": None,
                "y_steps": None,
                "x_mm": None,
                "y_mm": None,
                "is_moving": is_moving,
            }
        return {
            "x_steps": x_steps,
            "y_steps": y_steps,
            "x_mm": x_steps / self.steps_per_mm,
            "y_mm": y_steps / self.steps_per_mm,
            "is_moving": is_moving,
        }

    def get_status_snapshot(self) -> Dict[str, Any]:
        position = self.get_position()
        with self._state_lock:
            enabled = self._movement_enabled
            speed = self._requested_speed_mm_s
            light = self._light_state
            saved = list(self._saved_positions.items())
        saved_formatted = [
            {
                "name": name,
                "x_steps": pos[0],
                "y_steps": pos[1],
                "x_mm": pos[0] / self.steps_per_mm,
                "y_mm": pos[1] / self.steps_per_mm,
            }
            for name, pos in saved
        ]
        return {
            "timestamp": datetime.now().isoformat(),
            "position": position,
            "movement_enabled": enabled,
            "speed_mm_s": speed,
            "max_speed_mm_s": self._max_speed_limit,
            "max_speed_limit_mm_s": self._max_speed_limit,
            "absolute_max_speed_mm_s": self.ABSOLUTE_MAX_SPEED_MM_S,
            "light_state": light,
            "saved_positions": saved_formatted,
        }

    def set_manual_direction(self, dx: int, dy: int) -> None:
        dx = max(-1, min(1, dx))
        dy = max(-1, min(1, dy))
        with self._state_lock:
            self._manual_direction = (dx, dy)
            self._last_direction_update = time.time()
            if dx == 0 and dy == 0:
                self._residual_steps = [0.0, 0.0]
            else:
                self._last_nonzero_direction = (dx, dy)
                if self._movement_enabled:
                    self._pending_manual_nudge = True
        self._signal_movement_loop()

    def set_movement_enabled(self, enable: bool) -> None:
        with self._state_lock:
            self._movement_enabled = bool(enable)
            if enable:
                self._pending_manual_nudge = False
            if not enable:
                self._manual_direction = (0, 0)
                self._target_position_steps = None
                self._residual_steps = [0.0, 0.0]
                self._pending_manual_nudge = False
        self._signal_movement_loop()

    def is_movement_enabled(self) -> bool:
        with self._state_lock:
            return self._movement_enabled

    def emergency_stop(self) -> None:
        logger.warning("Emergency stop activated")
        with self._state_lock:
            self._emergency_flag = True
        self._signal_movement_loop()

    def save_current_position(self, name: str) -> None:
        if not name:
            raise ValueError("Position name cannot be empty")
        name = name.strip()
        if len(name) > 40:
            raise ValueError("Position name too long")
        position = self.get_position()
        if position["x_steps"] is None or position["y_steps"] is None:
            raise RuntimeError("Current position unknown")
        with self._state_lock:
            if name in self._saved_positions:
                del self._saved_positions[name]
            self._saved_positions[name] = (position["x_steps"], position["y_steps"])
            while len(self._saved_positions) > self.MAX_SAVED_POSITIONS:
                self._saved_positions.popitem(last=False)

    def delete_saved_position(self, name: str) -> None:
        with self._state_lock:
            self._saved_positions.pop(name, None)

    def move_to_saved_position(self, name: str) -> None:
        with self._state_lock:
            if not self._movement_enabled:
                raise RuntimeError("Movement is disabled")
            if name not in self._saved_positions:
                raise KeyError(name)
            target = self._saved_positions[name]
            self._target_position_steps = target
            self._manual_direction = (0, 0)
            self._residual_steps = [0.0, 0.0]
            self._pending_manual_nudge = False
        self._signal_movement_loop()

    def set_requested_speed(self, speed_mm_s: float) -> None:
        if speed_mm_s <= 0:
            raise ValueError("Speed must be positive")
        if speed_mm_s > self.ABSOLUTE_MAX_SPEED_MM_S:
            raise ValueError("Speed exceeds absolute limit")
        with self._state_lock:
            if speed_mm_s > self._max_speed_limit:
                raise ValueError("Speed exceeds configured limit")
            self._requested_speed_mm_s = speed_mm_s
            self._residual_steps = [0.0, 0.0]
        self._signal_movement_loop()

    def set_max_speed_limit(self, limit_mm_s: float) -> None:
        if limit_mm_s <= 0:
            raise ValueError("Maximum speed must be positive")
        if limit_mm_s > self.ABSOLUTE_MAX_SPEED_MM_S:
            raise ValueError("Maximum speed exceeds absolute limit")
        with self._state_lock:
            self._max_speed_limit = limit_mm_s
            if self._requested_speed_mm_s > self._max_speed_limit:
                self._requested_speed_mm_s = self._max_speed_limit
            self._residual_steps = [0.0, 0.0]
        self._signal_movement_loop()

    def get_saved_positions(self) -> List[Dict[str, Any]]:
        status = self.get_status_snapshot()
        return status["saved_positions"]

    def set_light_state(self, on: bool) -> None:
        command_value = "1" if on else "0"
        command = f"77032 0 0{command_value}\r"
        self._send_command(command)
        with self._state_lock:
            self._light_state = bool(on)

    def get_light_state(self) -> bool:
        with self._state_lock:
            return self._light_state


class StageControllerHTTPHandler(BaseHTTPRequestHandler):
    controller: Optional[MicroscopeStageController] = None
    ws_port: int = 0

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler signature)
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path in {"/", "/index.html"}:
            self._serve_html()
            return
        self._serve_static(path)

    def _serve_html(self) -> None:
        html_path = Path(__file__).parent / "web" / "stage_control.html"
        if not html_path.exists():
            self.send_error(404, "Web interface not found")
            return
        content = html_path.read_text(encoding="utf-8")
        default_limit = f"{MicroscopeStageController.DEFAULT_MAX_SPEED_MM_S:.4f}"
        abs_limit = f"{MicroscopeStageController.ABSOLUTE_MAX_SPEED_MM_S:.4f}"
        content = content.replace("__WS_PORT__", str(self.ws_port))
        content = content.replace("__DEFAULT_MAX_SPEED__", default_limit)
        content = content.replace("__MAX_SPEED__", default_limit)
        content = content.replace("__ABS_MAX_SPEED__", abs_limit)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _serve_static(self, path: str) -> None:
        safe_path = Path(unquote(path.lstrip("/")))
        if safe_path.parts and safe_path.parts[0] == "..":
            self.send_error(403)
            return
        file_path = Path(__file__).parent / "web" / safe_path
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        self.send_response(200)
        if file_path.suffix == ".js":
            content_type = "application/javascript"
        elif file_path.suffix == ".css":
            content_type = "text/css"
        else:
            content_type = "application/octet-stream"
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(file_path.read_bytes())


def run_http_server(
    controller: MicroscopeStageController,
    host: str,
    port: int,
    ws_port: int,
) -> ThreadingHTTPServer:
    StageControllerHTTPHandler.controller = controller
    StageControllerHTTPHandler.ws_port = ws_port
    server = ThreadingHTTPServer((host, port), StageControllerHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    setattr(server, "_thread", thread)
    logger.info("HTTP server running at http://%s:%s/", host, port)
    return server


KEY_DIRECTION_MAP: Dict[str, Tuple[int, int]] = {
    "ArrowUp": (0, 1),
    "ArrowDown": (0, -1),
    "ArrowLeft": (-1, 0),
    "ArrowRight": (1, 0),
    "w": (0, 1),
    "s": (0, -1),
    "a": (-1, 0),
    "d": (1, 0),
}


class StageWebSocketServer:
    def __init__(self, controller: MicroscopeStageController) -> None:
        self.controller = controller
        self.clients: Set[Any] = set()
        self.client_keys: Dict[Any, Set[str]] = {}
        self._loop = asyncio.get_event_loop()
        self._status_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    async def handler(self, websocket: Any) -> None:
        path = getattr(websocket, "path", None)
        if path is None:
            request = getattr(websocket, "request", None)
            if request is not None:
                path = getattr(request, "path", None)
        if path != "/ws":
            close = getattr(websocket, "close", None)
            if callable(close):
                result = close(code=4000, reason="Unsupported path")
                if asyncio.iscoroutine(result):
                    await result
            return
        await self._register(websocket)
        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(websocket, "Invalid JSON payload")
                    continue
                await self._handle_message(websocket, data)
        finally:
            await self._unregister(websocket)

    async def _register(self, websocket: Any) -> None:
        self.clients.add(websocket)
        self.client_keys[websocket] = set()
        await websocket.send(json.dumps({"type": "status", **self.controller.get_status_snapshot()}))

    async def _unregister(self, websocket: Any) -> None:
        keys = self.client_keys.pop(websocket, set())
        self.clients.discard(websocket)
        # Always recompute direction when a client disconnects, even if they had no keys pressed
        # This ensures we clear any stale state
        await self._recompute_direction()
        if keys:
            logger.warning("Client disconnected with %d keys still pressed: %s", len(keys), keys)

    async def _handle_message(self, websocket: Any, data: Dict[str, Any]) -> None:
        msg_type = data.get("type")
        if msg_type == "key":
            await self._handle_key_event(websocket, data)
        elif msg_type == "enable_movement":
            enabled = bool(data.get("enabled"))
            self.controller.set_movement_enabled(enabled)
            await self.broadcast_status()
        elif msg_type == "emergency_stop":
            self.controller.emergency_stop()
            await self.broadcast_status()
        elif msg_type == "request_status":
            await websocket.send(json.dumps({"type": "status", **self.controller.get_status_snapshot()}))
        elif msg_type == "save_position":
            name = str(data.get("name", "")).strip()
            try:
                self.controller.save_current_position(name)
            except Exception as exc:
                await self._send_error(websocket, str(exc))
            else:
                await self.broadcast_status()
        elif msg_type == "delete_position":
            name = str(data.get("name", ""))
            self.controller.delete_saved_position(name)
            await self.broadcast_status()
        elif msg_type == "move_to_position":
            name = str(data.get("name", ""))
            try:
                self.controller.move_to_saved_position(name)
            except KeyError:
                await self._send_error(websocket, f"Unknown position '{name}'")
            except Exception as exc:
                await self._send_error(websocket, str(exc))
            else:
                await self.broadcast_status()
        elif msg_type == "set_speed":
            if "speed" not in data:
                await self._send_error(websocket, "Missing speed value")
                return
            try:
                raw_speed = data.get("speed")
                if raw_speed is None:
                    raise ValueError("Speed is required")
                speed = float(raw_speed)
                self.controller.set_requested_speed(speed)
            except Exception as exc:
                await self._send_error(websocket, str(exc))
            else:
                await self.broadcast_status()
        elif msg_type == "set_max_speed":
            if "limit" not in data:
                await self._send_error(websocket, "Missing max speed value")
                return
            try:
                raw_limit = data.get("limit")
                if raw_limit is None:
                    raise ValueError("Limit is required")
                limit = float(raw_limit)
                self.controller.set_max_speed_limit(limit)
            except Exception as exc:
                await self._send_error(websocket, str(exc))
            else:
                await self.broadcast_status()
        elif msg_type == "light":
            state = str(data.get("state", "")).lower()
            if state not in {"on", "off"}:
                await self._send_error(websocket, "Invalid light state")
            else:
                try:
                    self.controller.set_light_state(state == "on")
                except Exception as exc:
                    await self._send_error(websocket, str(exc))
                else:
                    await self.broadcast_status()
        else:
            await self._send_error(websocket, f"Unsupported message type '{msg_type}'")

    async def _handle_key_event(self, websocket: Any, data: Dict[str, Any]) -> None:
        action = data.get("event")
        key = str(data.get("key", ""))
        if action not in {"down", "up"}:
            await self._send_error(websocket, "Invalid key event")
            return
        if key not in KEY_DIRECTION_MAP:
            return
        key_set = self.client_keys.get(websocket)
        if key_set is None:
            return
        if action == "down":
            key_set.add(key)
        else:
            key_set.discard(key)
        await self._recompute_direction()

    async def _recompute_direction(self) -> None:
        combined: Set[str] = set()
        for key_set in self.client_keys.values():
            combined.update(key_set)
        dx = dy = 0
        for key in combined:
            vec = KEY_DIRECTION_MAP[key]
            dx += vec[0]
            dy += vec[1]
        dx = max(-1, min(1, dx))
        dy = max(-1, min(1, dy))
        self.controller.set_manual_direction(dx, dy)

    async def broadcast_status(self) -> None:
        if not self.clients:
            return
        payload = json.dumps({"type": "status", **self.controller.get_status_snapshot()})
        await asyncio.gather(
            *(self._safe_send(client, payload) for client in list(self.clients)),
            return_exceptions=True,
        )

    async def _safe_send(self, websocket: Any, payload: str) -> None:
        try:
            await websocket.send(payload)
        except Exception:
            await self._unregister(websocket)

    async def _send_error(self, websocket: Any, message: str) -> None:
        payload = json.dumps({"type": "error", "message": message})
        await self._safe_send(websocket, payload)

    async def status_publisher(self) -> None:
        while not self._stop_event.is_set():
            await self.broadcast_status()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    async def run_forever(self) -> None:
        self._status_task = asyncio.create_task(self.status_publisher())
        try:
            await self._stop_event.wait()
        finally:
            if self._status_task:
                self._status_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._status_task

    def shutdown(self) -> None:
        self._stop_event.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Microscope XY stage controller server")
    parser.add_argument("--device", type=int, default=0, help="FTDI device index (default: 0)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Server host")
    parser.add_argument("--port", type=int, default=8001, help="HTTP server port")
    parser.add_argument(
        "--ws-port",
        type=int,
        default=None,
        help="WebSocket server port (default: http port + 1)",
    )
    return parser.parse_args()


async def run_async_servers(host: str, ws_port: int, ws_server: StageWebSocketServer) -> None:
    async with websockets.serve(ws_server.handler, host, ws_port, ping_interval=20, ping_timeout=20):
        logger.info("WebSocket server running at ws://%s:%s/ws", host, ws_port)
        await ws_server.run_forever()


def main() -> None:
    args = parse_args()
    ws_port = args.ws_port or (args.port + 1)

    controller = MicroscopeStageController(device_index=args.device)
    try:
        controller.connect()
    except Exception as exc:  # pragma: no cover - hardware dependency
        logger.error("Unable to start controller: %s", exc)
        return

    http_server = run_http_server(controller, host=args.host, port=args.port, ws_port=ws_port)
    ws_server = StageWebSocketServer(controller)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_async_servers(args.host, ws_port, ws_server))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        ws_server.shutdown()
        controller.disconnect()
        http_server.shutdown()
        thread = getattr(http_server, "_thread", None)
        http_server.server_close()
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
