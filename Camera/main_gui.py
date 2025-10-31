"""DearPyGui dashboard for monitoring and controlling the tracking image server."""

from __future__ import annotations

import argparse
import io
import json
import logging
import threading
import time
import math
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple, cast

import grpc
import numpy as np
from collections import OrderedDict, deque
from google.protobuf import empty_pb2, json_format, struct_pb2

import dearpygui.dearpygui as dpg

try:
    from .image_proto import LatestImageRequest, StorageConfig  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - fallback for script execution
    from image_proto import LatestImageRequest, StorageConfig  # type: ignore[import-not-found]

try:
    import tifffile  # type: ignore
except ImportError:  # pragma: no cover - tifffile optional
    tifffile = None  # type: ignore

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - fallback when cv2 missing
    cv2 = None  # type: ignore

try:
    import h5py  # type: ignore
except ImportError:  # pragma: no cover - h5py optional
    h5py = None  # type: ignore


def _configure_cv2_threads() -> None:
    if cv2 is None:
        return
    try:
        cv2.setNumThreads(1)  # type: ignore[attr-defined]
    except AttributeError:
        pass
    try:
        cv2.ocl.setUseOpenCL(False)  # type: ignore[attr-defined]
    except AttributeError:
        pass


_configure_cv2_threads()


LOGGER = logging.getLogger("main_gui")

MAX_GRPC_MESSAGE_BYTES = 64 * 1024 * 1024
DEFAULT_TEXTURE_SIZE = (64, 64)
DEFAULT_DISPLAY_SCALE = 0.5
DEFAULT_HISTORY_LIMIT = 10_000
CV2_AVAILABLE = cv2 is not None
if not CV2_AVAILABLE:
    LOGGER.info("OpenCV not available; using NumPy overlay rendering")


class ImageExchangeStub:
    """Lightweight stub mirroring the generated client without codegen import."""

    def __init__(self, channel: grpc.Channel) -> None:
        try:
            from .image_proto import (  # type: ignore[import-not-found]
                FrameEnvelope,
                ImageChunk,
                LatestImageReply,
                StorageConfig,
                UploadAck,
            )
        except ImportError:  # pragma: no cover - fallback for script execution
            from image_proto import (  # type: ignore[import-not-found]
                FrameEnvelope,
                ImageChunk,
                LatestImageReply,
                StorageConfig,
                UploadAck,
            )

        self._upload = channel.unary_unary(
            "/images.ImageExchange/UploadImage",
            request_serializer=ImageChunk.SerializeToString,
            response_deserializer=UploadAck.FromString,
        )
        self._get_latest = channel.unary_unary(
            "/images.ImageExchange/GetLatestImage",
            request_serializer=LatestImageRequest.SerializeToString,
            response_deserializer=LatestImageReply.FromString,
        )
        self._get_tracks = channel.unary_unary(
            "/images.ImageExchange/GetLatestTracks",
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )
        self._get_config = channel.unary_unary(
            "/images.ImageExchange/GetTrackingConfig",
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )
        self._update_config = channel.unary_unary(
            "/images.ImageExchange/UpdateTrackingConfig",
            request_serializer=struct_pb2.Struct.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )
        self._stream_frames = channel.unary_stream(
            "/images.ImageExchange/StreamFrames",
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=FrameEnvelope.FromString,
        )
        self._get_storage_config = channel.unary_unary(
            "/images.ImageExchange/GetStorageConfig",
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=StorageConfig.FromString,
        )
        self._update_storage_config = channel.unary_unary(
            "/images.ImageExchange/UpdateStorageConfig",
            request_serializer=StorageConfig.SerializeToString,
            response_deserializer=StorageConfig.FromString,
        )

    def GetLatestImage(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._get_latest(request, timeout=timeout)

    def GetLatestTracks(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._get_tracks(request, timeout=timeout)

    def GetTrackingConfig(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._get_config(request, timeout=timeout)

    def UpdateTrackingConfig(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._update_config(request, timeout=timeout)

    def StreamFrames(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._stream_frames(request, timeout=timeout)

    def GetStorageConfig(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._get_storage_config(request, timeout=timeout)

    def UpdateStorageConfig(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._update_storage_config(request, timeout=timeout)


def _decode_tiff_image(data: bytes) -> np.ndarray:
    if tifffile is not None:
        with tifffile.TiffFile(io.BytesIO(data)) as tif:  # type: ignore[arg-type]
            array = tif.asarray()
    else:  # pragma: no cover - pillow fallback
        try:
            from PIL import Image  # type: ignore
        except ImportError as exc:  # pragma: no cover - pillow required when tifffile missing
            raise RuntimeError("Pillow or tifffile must be installed to decode TIFF images") from exc
        with Image.open(io.BytesIO(data)) as img:  # type: ignore[name-defined]
            array = np.array(img)
    if array.ndim > 2:
        array = np.mean(array, axis=-1)
    if array.dtype != np.float32:
        array = array.astype(np.float32)
    return np.ascontiguousarray(array)


def _normalize_to_uint8(image: np.ndarray) -> np.ndarray:
    if image.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    if image.dtype == np.uint8:
        return np.ascontiguousarray(image)
    if np.issubdtype(image.dtype, np.unsignedinteger):
        bit_depth = image.dtype.itemsize * 8
        if bit_depth >= 8:
            shift = bit_depth - 8
            shifted = np.right_shift(image, shift)
        else:
            shift = 8 - bit_depth
            shifted = np.left_shift(image.astype(np.uint16), shift)
        return np.ascontiguousarray(shifted.astype(np.uint8, copy=False))
    if np.issubdtype(image.dtype, np.signedinteger):
        bit_depth = image.dtype.itemsize * 8
        max_pos = float(2 ** (bit_depth - 1) - 1)
        scale = max(max_pos / 127.0, 1.0)
        normalized = np.rint(image.astype(np.float32) / scale)
        clipped = np.clip(normalized, -128.0, 127.0)
        shifted = clipped + 128.0
        return np.ascontiguousarray(shifted.astype(np.uint8, copy=False))
    working = image.astype(np.float32, copy=False)
    finite_mask = np.isfinite(working)
    if not np.any(finite_mask):
        return np.zeros_like(working, dtype=np.uint8)
    max_val = float(np.max(working[finite_mask]))
    min_val = float(np.min(working[finite_mask]))
    if min_val >= 0.0:
        if max_val <= 255.0:
            scale = 1.0
        else:
            bits = max(8, int(math.ceil(math.log2(max_val + 1.0))))
            scale = (2 ** bits - 1) / 255.0
        scaled = np.rint(working / scale)
        clipped = np.clip(scaled, 0.0, 255.0)
        return np.ascontiguousarray(clipped.astype(np.uint8, copy=False))
    abs_max = max(abs(min_val), abs(max_val))
    if abs_max <= 127.0:
        scale = 1.0
    else:
        bits = max(8, int(math.ceil(math.log2(abs_max + 1.0))) + 1)
        scale = (2 ** (bits - 1) - 1) / 127.0
    scaled = np.rint(working / scale)
    clipped = np.clip(scaled, -128.0, 127.0)
    shifted = clipped + 128.0
    return np.ascontiguousarray(shifted.astype(np.uint8, copy=False))


def _mass_to_color(mass: float, min_mass: float, max_mass: float) -> Tuple[int, int, int]:
    if max_mass <= min_mass:
        return (255, 255, 0)
    norm = (mass - min_mass) / (max_mass - min_mass)
    norm = max(0.0, min(1.0, norm))
    r = int(255 * norm)
    g = int(255 * (1.0 - abs(norm - 0.5) * 2.0))
    b = int(255 * (1.0 - norm))
    return (r, g, b)


def _clamp_color_channel(value: float) -> int:
    return int(max(0, min(255, round(value))))


def _rgb_to_dpg_color(color: Tuple[int, int, int]) -> Tuple[float, float, float, float]:
    r, g, b = color
    return (r / 255.0, g / 255.0, b / 255.0, 1.0)


def _dpg_color_to_rgb(value: Sequence[float]) -> Tuple[int, int, int]:
    if len(value) < 3:
        return (0, 0, 0)
    if any(component > 1.0 for component in value[:3]):
        r, g, b = value[:3]
        return (_clamp_color_channel(r), _clamp_color_channel(g), _clamp_color_channel(b))
    r, g, b = value[:3]
    return (
        _clamp_color_channel(r * 255.0),
        _clamp_color_channel(g * 255.0),
        _clamp_color_channel(b * 255.0),
    )


def _coerce_rgb_tuple(value: Any, fallback: Tuple[int, int, int]) -> Tuple[int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (
            _clamp_color_channel(float(value[0])),
            _clamp_color_channel(float(value[1])),
            _clamp_color_channel(float(value[2])),
        )
    return fallback




def _draw_tile_grid(image: np.ndarray, tile_w: int, tile_h: int, overlap: int) -> None:
    h, w, _ = image.shape
    tile_w = max(4, tile_w)
    tile_h = max(4, tile_h)
    overlap = max(0, overlap)
    # Stride matches backend tiling step (tile size minus overlap, clamped to >=1).
    stride_x = max(1, tile_w - overlap) if overlap < tile_w else tile_w
    stride_y = max(1, tile_h - overlap) if overlap < tile_h else tile_h

    def _line_positions(length: int, tile_size: int, stride: int) -> List[int]:
        positions: List[int] = []
        seen = set()
        for start in range(0, length, stride):
            if start not in seen:
                positions.append(start)
                seen.add(start)
            if stride < tile_size:
                end = min(length - 1, start + tile_size - 1)
                if end not in seen:
                    positions.append(end)
                    seen.add(end)
            if start + stride >= length:
                break
        if length - 1 not in seen:
            positions.append(length - 1)
        return sorted(positions)

    vertical_lines = _line_positions(w, tile_w, stride_x)
    horizontal_lines = _line_positions(h, tile_h, stride_y)

    if CV2_AVAILABLE and cv2 is not None:
        color_bgr = (200, 200, 50)
        for x in vertical_lines:
            cv2.line(image, (x, 0), (x, h - 1), color_bgr, 1)  # type: ignore[attr-defined]
        for y in horizontal_lines:
            cv2.line(image, (0, y), (w - 1, y), color_bgr, 1)  # type: ignore[attr-defined]
        return

    color = np.array([200, 200, 50], dtype=np.uint8)
    for x in vertical_lines:
        if 0 <= x < w:
            image[:, x : x + 1, :] = color
    for y in horizontal_lines:
        if 0 <= y < h:
            image[y : y + 1, :, :] = color


def _draw_filled_circle(image: np.ndarray, cx: float, cy: float, radius: int, color: Tuple[int, int, int]) -> None:
    h, w, _ = image.shape
    if radius <= 0:
        return
    center_x = int(round(cx))
    center_y = int(round(cy))
    if center_x + radius < 0 or center_x - radius >= w:
        return
    if center_y + radius < 0 or center_y - radius >= h:
        return
    if CV2_AVAILABLE and cv2 is not None:
        color_bgr = (int(color[2]), int(color[1]), int(color[0]))
        cv2.circle(image, (center_x, center_y), radius, color_bgr, -1)  # type: ignore[attr-defined]
        return
    x0 = max(0, center_x - radius)
    y0 = max(0, center_y - radius)
    x1 = min(w - 1, center_x + radius)
    y1 = min(h - 1, center_y + radius)
    radius_sq = radius * radius
    for yy in range(y0, y1 + 1):
        dy_sq = (yy - cy) * (yy - cy)
        for xx in range(x0, x1 + 1):
            dx_sq = (xx - cx) * (xx - cx)
            if dx_sq + dy_sq <= radius_sq:
                image[yy, xx, 0] = color[0]
                image[yy, xx, 1] = color[1]
                image[yy, xx, 2] = color[2]


def _compose_overlay(
    base_uint8: np.ndarray,
    detections: List[Dict[str, Any]],
    params: Dict[str, Any],
    show_grid: bool,
    use_colormap: bool,
    mass_cutoff: float,
    below_color: Tuple[int, int, int],
    above_color: Tuple[int, int, int],
    circle_scale: float,
    out: Optional[np.ndarray] = None,
) -> np.ndarray:
    height = int(base_uint8.shape[0])
    width = int(base_uint8.shape[1])
    if out is None or out.shape != (height, width, 3):
        overlay = np.empty((height, width, 3), dtype=np.uint8)
    else:
        overlay = out
    overlay[:, :, 0] = base_uint8
    overlay[:, :, 1] = base_uint8
    overlay[:, :, 2] = base_uint8
    if show_grid:
        tile_w = int(params.get("tile_width", 256))
        tile_h = int(params.get("tile_height", 256))
        overlap = int(params.get("tile_overlap", 32))
        _draw_tile_grid(overlay, tile_w, tile_h, overlap)
    if not detections:
        return overlay
    masses = [float(det.get("mass", 0.0)) for det in detections]
    min_mass = min(masses) if masses else 0.0
    max_mass = max(masses) if masses else 0.0
    base_diameter = max(2, int(params.get("diameter", 21) or 21))
    scaled_diameter = max(2, int(round(base_diameter * max(circle_scale, 0.1))))
    radius = max(1, scaled_diameter // 2)
    for det in detections:
        x = float(det.get("x", 0.0))
        y = float(det.get("y", 0.0))
        mass = float(det.get("mass", 0.0))
        if use_colormap and masses:
            color = _mass_to_color(mass, min_mass, max_mass)
        else:
            color = below_color if mass < mass_cutoff else above_color
        _draw_filled_circle(overlay, x, y, radius, color)
    return overlay


def create_overlay(
    image_array: np.ndarray,
    detections: List[Dict[str, Any]],
    params: Dict[str, Any],
    show_grid: bool,
    use_colormap: bool,
    mass_cutoff: float,
    below_color: Tuple[int, int, int],
    above_color: Tuple[int, int, int],
    circle_scale: float,
    out: Optional[np.ndarray] = None,
) -> np.ndarray:
    if image_array.ndim == 2 and image_array.dtype == np.uint8:
        base = image_array
    else:
        base = _normalize_to_uint8(image_array)
    return _compose_overlay(
        base,
        detections,
        params,
        show_grid,
        use_colormap,
        mass_cutoff,
        below_color,
        above_color,
        circle_scale,
        out=out,
    )


def _resample_for_display(image: np.ndarray, scale: float) -> np.ndarray:
    if image.size == 0:
        return image
    try:
        scale_value = float(scale)
    except (TypeError, ValueError):
        return image
    if not np.isfinite(scale_value) or scale_value <= 0:
        return image
    if abs(scale_value - 1.0) < 1e-3:
        return image
    height = int(round(image.shape[0] * scale_value))
    width = int(round(image.shape[1] * scale_value))
    height = max(1, height)
    width = max(1, width)
    y_idx = np.linspace(0, image.shape[0] - 1, height).astype(np.float32)
    x_idx = np.linspace(0, image.shape[1] - 1, width).astype(np.float32)
    y_idx = np.clip(np.rint(y_idx), 0, image.shape[0] - 1).astype(int)
    x_idx = np.clip(np.rint(x_idx), 0, image.shape[1] - 1).astype(int)
    if image.ndim == 2:
        return image[np.ix_(y_idx, x_idx)]
    return image[y_idx][:, x_idx]


@dataclass
class TrackingParameters:
    diameter: int = 21
    separation: int = 18
    percentile: float = 14.0
    minmass: float = 100.0
    maxmass: float = 0.0
    pixel_threshold: float = 0.0
    preprocess: bool = True
    lshort: int = 1
    llong: int = 21
    min_ecc: float = -1.0
    max_ecc: float = -1.0
    refine: int = 1
    tile_width: int = 256
    tile_height: int = 256
    tile_overlap: int = 32
    max_workers: int = 5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "diameter": self.diameter,
            "separation": self.separation,
            "percentile": self.percentile,
            "minmass": self.minmass,
            "maxmass": self.maxmass,
            "pixel_threshold": self.pixel_threshold,
            "preprocess": self.preprocess,
            "lshort": self.lshort,
            "llong": self.llong,
            "min_ecc": self.min_ecc,
            "max_ecc": self.max_ecc,
            "refine": self.refine,
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "tile_overlap": self.tile_overlap,
            "max_workers": self.max_workers,
        }

    def update_from_dict(self, payload: Dict[str, Any]) -> None:
        for key, value in payload.items():
            if not hasattr(self, key):
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                setattr(self, key, bool(value))
            elif isinstance(current, int):
                try:
                    setattr(self, key, int(float(value)))
                except (ValueError, TypeError):
                    LOGGER.warning("Unable to coerce %s to int for %s", value, key)
            elif isinstance(current, float):
                try:
                    setattr(self, key, float(value))
                except (ValueError, TypeError):
                    LOGGER.warning("Unable to coerce %s to float for %s", value, key)


@dataclass
class AppState:
    host: str = "127.0.0.1"
    port: int = 50052
    raw_save_dir: Path = field(default_factory=lambda: Path.cwd() / "raw_frames")
    overlay_save_dir: Path = field(default_factory=lambda: Path.cwd() / "tracked_frames")
    lock: threading.Lock = field(default_factory=threading.Lock)
    connected: bool = False
    status_message: str = ""
    display_mode: str = "overlay"
    show_tile_grid: bool = False
    use_mass_colormap: bool = True
    mass_cutoff: float = 0.0
    cutoff_below_color: Tuple[int, int, int] = (80, 180, 80)
    cutoff_above_color: Tuple[int, int, int] = (220, 60, 60)
    circle_size_scale: float = 1.0
    auto_save_raw: bool = False
    auto_save_overlay: bool = False
    save_to_hdf5: bool = False
    zoom: float = DEFAULT_DISPLAY_SCALE
    latest_sequence: int = 0
    latest_filename: str = ""
    latest_timestamp_ms: int = 0
    latest_source: str = ""
    latest_image_array: Optional[np.ndarray] = None
    latest_image_uint8: Optional[np.ndarray] = None
    latest_overlay_array: Optional[np.ndarray] = None
    detection_count: int = 0
    latest_latency_ms: int = 0
    latest_processing_ms: int = 0
    latest_request_latency_ms: float = 0.0
    latest_render_latency_ms: float = 0.0
    latest_raw_format: str = "tiff"
    tracks: List[Dict[str, Any]] = field(default_factory=list)
    tracking_params: TrackingParameters = field(default_factory=TrackingParameters)
    overlay_needs_refresh: bool = False
    texture_dirty: bool = True
    texture_tag: Optional[str] = None
    texture_size: Tuple[int, int] = field(default_factory=lambda: DEFAULT_TEXTURE_SIZE)
    pending_field_updates: Dict[str, Any] = field(default_factory=dict)
    last_tracking_params_path: Optional[Path] = None
    overlay_buffer: Optional[np.ndarray] = None
    texture_buffer: Optional[np.ndarray] = None
    display_cache: "OrderedDict[Tuple[str, float], np.ndarray]" = field(default_factory=OrderedDict)
    display_cache_limit: int = 6
    metrics_history_limit: int = DEFAULT_HISTORY_LIMIT
    metric_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    latency_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    processing_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    render_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    feature_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    save_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    save_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    save_kind_history: Deque[str] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    compression_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    compression_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LIMIT))
    latest_save_duration_ms: float = 0.0
    latest_save_kind: str = "idle"
    hdf5_path: Optional[Path] = None
    storage_image_format: str = "native"
    storage_target_fps: float = 0.0
    latest_storage_path: str = ""
    latest_storage_saved: bool = False
    latest_storage_ratio: float = 0.0
    latest_storage_bytes_in: int = 0
    latest_storage_bytes_out: int = 0
    latest_storage_codec: str = ""
    latest_throttle_ms: float = 0.0
    latest_storage_message: str = ""

    def set_texture_info(self, tag: str, size: Tuple[int, int]) -> None:
        with self.lock:
            self.texture_tag = tag
            self.texture_size = size

    def schedule_field_update(self, key: str, value: Any) -> None:
        with self.lock:
            self.pending_field_updates[key] = value

    def set_status(self, message: str) -> None:
        with self.lock:
            self.status_message = message

    def _ensure_overlay_buffer(self, shape: Tuple[int, int]) -> np.ndarray:
        height = int(shape[0])
        width = int(shape[1])
        if self.overlay_buffer is None or self.overlay_buffer.shape != (height, width, 3):
            self.overlay_buffer = np.empty((height, width, 3), dtype=np.uint8)
        return self.overlay_buffer

    def _ensure_texture_buffer(self, width: int, height: int) -> np.ndarray:
        size = width * height * 4
        if self.texture_buffer is None or self.texture_buffer.size != size:
            self.texture_buffer = np.empty(size, dtype=np.float32)
        return self.texture_buffer.reshape(height, width, 4)

    def _texture_payload_from_image(self, image: np.ndarray) -> Tuple[np.ndarray, int, int]:
        display = np.ascontiguousarray(image)
        height = int(display.shape[0])
        width = int(display.shape[1])
        texture_view = self._ensure_texture_buffer(width, height)
        factor = 1.0 / 255.0
        if display.ndim == 2:
            np.multiply(display, factor, out=texture_view[:, :, 0])
            np.multiply(display, factor, out=texture_view[:, :, 1])
            np.multiply(display, factor, out=texture_view[:, :, 2])
        else:
            np.multiply(display[:, :, 0], factor, out=texture_view[:, :, 0])
            np.multiply(display[:, :, 1], factor, out=texture_view[:, :, 1])
            np.multiply(display[:, :, 2], factor, out=texture_view[:, :, 2])
        texture_view[:, :, 3].fill(1.0)
        assert self.texture_buffer is not None
        return self.texture_buffer, width, height

    def set_connection_info(self, host: str, port: int, connected: bool, from_remote: bool = False) -> None:
        with self.lock:
            self.host = host
            self.port = port
            self.connected = connected
            if from_remote:
                self.pending_field_updates["host"] = host
                self.pending_field_updates["port"] = port

    def set_auto_save_raw(self, enabled: bool, from_remote: bool = False) -> None:
        with self.lock:
            self.auto_save_raw = bool(enabled)
            if from_remote:
                self.pending_field_updates["auto_save_raw"] = self.auto_save_raw

    def set_auto_save_overlay(self, enabled: bool, from_remote: bool = False) -> None:
        with self.lock:
            self.auto_save_overlay = bool(enabled)
            if from_remote:
                self.pending_field_updates["auto_save_overlay"] = self.auto_save_overlay

    def set_save_to_hdf5(self, enabled: bool, from_remote: bool = False) -> None:
        normalized = bool(enabled)
        with self.lock:
            if self.save_to_hdf5 == normalized:
                if from_remote:
                    self.pending_field_updates["save_to_hdf5"] = self.save_to_hdf5
                return
            self.save_to_hdf5 = normalized
            if not normalized:
                self.hdf5_path = None
                if from_remote:
                    self.pending_field_updates["hdf5_path"] = ""
            if from_remote:
                self.pending_field_updates["save_to_hdf5"] = self.save_to_hdf5

    def _prepare_new_hdf5_path_locked(self) -> None:
        self._close_hdf5_locked()
        path = self._generate_hdf5_path()
        self.hdf5_path = path
        self.hdf5_frame_count = 0
        self._hdf5_frame_shape = None
        self._hdf5_metadata_dtype = None
        self.pending_field_updates["hdf5_path"] = str(path)

    def _close_hdf5_locked(self) -> None:
        if self._hdf5_file is not None:
            try:
                self._hdf5_file.flush()
                self._hdf5_file.close()
            except Exception as exc:
                LOGGER.warning("Failed to close HDF5 writer: %s", exc)
        self._hdf5_file = None
        self._hdf5_frames = None
        self._hdf5_metadata = None
        self._hdf5_frame_shape = None
        self._hdf5_metadata_dtype = None
        self.hdf5_frame_count = 0

    def _generate_hdf5_path(self) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        base = self.raw_save_dir / f"raw_frames_{timestamp}.h5"
        self.raw_save_dir.mkdir(parents=True, exist_ok=True)
        return _ensure_unique(base)

    def _record_save_metric(self, duration_ms: float, timestamp: float, kind: str) -> None:
        with self.lock:
            self.latest_save_duration_ms = float(duration_ms)
            self.latest_save_kind = kind
            self.save_timestamps.append(float(timestamp))
            self.save_history.append(float(duration_ms))
            self.save_kind_history.append(kind)

    def set_display_mode(self, mode: str, from_remote: bool = False) -> None:
        normalized = "overlay" if mode not in {"overlay", "raw"} else mode
        with self.lock:
            if self.display_mode != normalized:
                self.display_mode = normalized
                self.texture_dirty = True
                self.display_cache.clear()
            if from_remote:
                self.pending_field_updates["display_mode"] = self.display_mode

    def set_show_tile_grid(self, value: bool, from_remote: bool = False) -> None:
        with self.lock:
            self.show_tile_grid = bool(value)
            self.overlay_needs_refresh = True
            self.display_cache.clear()
            if from_remote:
                self.pending_field_updates["tile_grid"] = self.show_tile_grid

    def set_use_mass_colormap(self, value: bool) -> None:
        with self.lock:
            normalized = bool(value)
            if self.use_mass_colormap != normalized:
                self.use_mass_colormap = normalized
                self.overlay_needs_refresh = True
                self.display_cache.clear()
            self.pending_field_updates["use_mass_colormap"] = self.use_mass_colormap

    def set_mass_cutoff(self, cutoff: float) -> None:
        with self.lock:
            value = float(cutoff)
            if not math.isfinite(value):
                value = 0.0
            if abs(value - self.mass_cutoff) > 1e-6:
                self.mass_cutoff = value
                self.overlay_needs_refresh = True
                self.display_cache.clear()
            self.pending_field_updates["mass_cutoff"] = self.mass_cutoff

    def set_cutoff_colors(
        self,
        below: Optional[Tuple[int, int, int]] = None,
        above: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        with self.lock:
            updated = False
            if below is not None:
                sanitized_below = (
                    _clamp_color_channel(below[0]),
                    _clamp_color_channel(below[1]),
                    _clamp_color_channel(below[2]),
                )
                if sanitized_below != self.cutoff_below_color:
                    self.cutoff_below_color = sanitized_below
                    updated = True
                self.pending_field_updates["below_cutoff_color"] = self.cutoff_below_color
            if above is not None:
                sanitized_above = (
                    _clamp_color_channel(above[0]),
                    _clamp_color_channel(above[1]),
                    _clamp_color_channel(above[2]),
                )
                if sanitized_above != self.cutoff_above_color:
                    self.cutoff_above_color = sanitized_above
                    updated = True
                self.pending_field_updates["above_cutoff_color"] = self.cutoff_above_color
            if updated:
                self.overlay_needs_refresh = True
                self.display_cache.clear()

    def set_circle_size_scale(self, scale: float) -> None:
        with self.lock:
            sanitized = max(0.1, min(float(scale), 5.0))
            if abs(sanitized - self.circle_size_scale) > 1e-3:
                self.circle_size_scale = sanitized
                self.overlay_needs_refresh = True
                self.display_cache.clear()
            self.pending_field_updates["circle_size_scale"] = self.circle_size_scale

    def set_zoom(self, value: float) -> None:
        with self.lock:
            scale = max(0.1, min(4.0, float(value)))
            if abs(scale - self.zoom) > 1e-3:
                self.zoom = scale
                self.texture_dirty = True

    def set_metrics_history_limit(self, limit: int, _from_remote: bool = False) -> None:
        sanitized = max(100, min(int(limit), 100_000))
        updated = False
        with self.lock:
            if sanitized != self.metrics_history_limit:
                self.metrics_history_limit = sanitized
                timestamps = list(self.metric_timestamps)
                latency = list(self.latency_history)
                processing = list(self.processing_history)
                render = list(self.render_history)
                features = list(self.feature_history)
                save_times = list(self.save_timestamps)
                save_values = list(self.save_history)
                save_kinds = list(self.save_kind_history)
                compression_times = list(self.compression_timestamps)
                compression_values = list(self.compression_history)
                self.metric_timestamps = deque(timestamps[-sanitized:], maxlen=sanitized)
                self.latency_history = deque(latency[-sanitized:], maxlen=sanitized)
                self.processing_history = deque(processing[-sanitized:], maxlen=sanitized)
                self.render_history = deque(render[-sanitized:], maxlen=sanitized)
                self.feature_history = deque(features[-sanitized:], maxlen=sanitized)
                self.save_timestamps = deque(save_times[-sanitized:], maxlen=sanitized)
                self.save_history = deque(save_values[-sanitized:], maxlen=sanitized)
                self.save_kind_history = deque(save_kinds[-sanitized:], maxlen=sanitized)
                self.compression_timestamps = deque(compression_times[-sanitized:], maxlen=sanitized)
                self.compression_history = deque(compression_values[-sanitized:], maxlen=sanitized)
                updated = True
        if updated:
            self.schedule_field_update("metrics_history_limit", sanitized)

    def set_directories(
        self,
        raw_dir: Optional[str] = None,
        overlay_dir: Optional[str] = None,
        from_remote: bool = False,
    ) -> None:
        with self.lock:
            if raw_dir is not None:
                self.raw_save_dir = Path(raw_dir)
                if from_remote:
                    self.pending_field_updates["raw_dir"] = str(self.raw_save_dir)
            if overlay_dir is not None:
                self.overlay_save_dir = Path(overlay_dir)
                if from_remote:
                    self.pending_field_updates["overlay_dir"] = str(self.overlay_save_dir)

    def set_storage_config(self, config: Dict[str, Any], from_remote: bool = False) -> None:
        with self.lock:
            enabled = bool(config.get("enabled", self.auto_save_raw))
            hdf5_enabled = bool(config.get("hdf5_enabled", self.save_to_hdf5))
            target_fps = float(config.get("target_fps", self.storage_target_fps))
            image_format = (str(config.get("image_format", self.storage_image_format or "native"))).lower()
            output_dir_raw = config.get("output_dir")
            if output_dir_raw:
                self.raw_save_dir = Path(output_dir_raw)
            hdf5_path_raw = config.get("hdf5_path") or ""
            self.hdf5_path = Path(hdf5_path_raw) if hdf5_path_raw else None
            self.auto_save_raw = enabled
            self.save_to_hdf5 = hdf5_enabled
            self.storage_target_fps = target_fps
            self.storage_image_format = "native" if image_format in {"native", "copy", "original", "tif", "tiff"} else self.storage_image_format
            if from_remote:
                self.pending_field_updates["auto_save_raw"] = self.auto_save_raw
                self.pending_field_updates["save_to_hdf5"] = self.save_to_hdf5
                self.pending_field_updates["raw_dir"] = str(self.raw_save_dir)
                self.pending_field_updates["hdf5_path"] = str(self.hdf5_path) if self.hdf5_path else ""
                self.pending_field_updates["storage_target_fps"] = self.storage_target_fps

    def update_tracking_params(self, payload: Dict[str, Any], from_remote: bool = False) -> None:
        with self.lock:
            self.tracking_params.update_from_dict(payload)
            self.overlay_needs_refresh = True
            self.display_cache.clear()
            params_snapshot = self.tracking_params.to_dict()
            if from_remote:
                self.pending_field_updates["tracking_params"] = params_snapshot

    def update_latest_frame(
        self,
        metadata: Dict[str, Any],
        raw_bytes: bytes,
        image_array: np.ndarray,
        image_uint8: np.ndarray,
        track_info: Dict[str, Any],
        image_format: str,
        overlay_params: Dict[str, Any],
        show_grid: bool,
        timestamp_s: Optional[float] = None,
    ) -> None:
        auto_raw = False
        auto_overlay = False
        raw_dir: Optional[Path] = None
        overlay_dir: Optional[Path] = None
        overlay_snapshot: Optional[np.ndarray] = None
        timestamp = float(timestamp_s if timestamp_s is not None else time.time())
        with self.lock:
            self.latest_sequence = int(metadata.get("sequence", 0))
            self.latest_filename = str(metadata.get("filename", ""))
            self.latest_timestamp_ms = int(metadata.get("timestamp_ms", 0))
            self.latest_source = str(metadata.get("source", ""))
            self.latest_image_array = image_array
            self.latest_image_uint8 = image_uint8
            overlay_buffer = self._ensure_overlay_buffer((image_uint8.shape[0], image_uint8.shape[1]))
            detections = track_info.get("detections", [])
            overlay = create_overlay(
                image_uint8,
                detections,
                overlay_params,
                show_grid,
                self.use_mass_colormap,
                self.mass_cutoff,
                self.cutoff_below_color,
                self.cutoff_above_color,
                self.circle_size_scale,
                out=overlay_buffer,
            )
            self.latest_overlay_array = overlay
            overlay_snapshot = overlay
            self.latest_raw_format = image_format
            self.detection_count = int(track_info.get("detection_count", len(detections)))
            self.latest_latency_ms = int(track_info.get("latency_ms", 0))
            self.latest_processing_ms = int(track_info.get("processing_ms", 0))
            self.tracks = list(detections)
            self.texture_dirty = True
            self.overlay_needs_refresh = False
            self.display_cache.clear()
            auto_overlay = self.auto_save_overlay
            raw_dir = self.raw_save_dir
            overlay_dir = self.overlay_save_dir
            metadata.setdefault("image_format", image_format)
            self.metric_timestamps.append(timestamp)
            self.latency_history.append(float(self.latest_latency_ms))
            self.processing_history.append(float(self.latest_processing_ms))
            self.feature_history.append(float(self.detection_count))

            storage_kind = str(metadata.get("storage_kind", "")).strip()
            storage_duration = float(metadata.get("storage_ms", 0.0))
            storage_saved = bool(metadata.get("storage_saved", False))
            storage_path = str(metadata.get("storage_path", ""))
            storage_codec = str(metadata.get("storage_codec", ""))
            try:
                storage_ratio_raw = float(metadata.get("storage_ratio", 0.0))
            except (TypeError, ValueError):
                storage_ratio_raw = 0.0
            storage_ratio_pct = max(0.0, storage_ratio_raw * 100.0)
            try:
                storage_bytes_in = int(metadata.get("storage_bytes_in", 0))
            except (TypeError, ValueError):
                storage_bytes_in = 0
            try:
                storage_bytes_out = int(metadata.get("storage_bytes_out", 0))
            except (TypeError, ValueError):
                storage_bytes_out = 0
            try:
                storage_throttle_ms = float(metadata.get("storage_throttle_ms", 0.0))
            except (TypeError, ValueError):
                storage_throttle_ms = 0.0
            storage_message = str(metadata.get("storage_message", ""))
            self.latest_save_duration_ms = storage_duration
            self.latest_save_kind = storage_kind or ("success" if storage_saved else "idle")
            self.latest_storage_path = storage_path
            self.latest_storage_saved = storage_saved
            self.latest_storage_codec = storage_codec
            self.latest_storage_ratio = storage_ratio_pct
            self.latest_storage_bytes_in = storage_bytes_in
            self.latest_storage_bytes_out = storage_bytes_out
            self.latest_throttle_ms = storage_throttle_ms
            self.latest_storage_message = storage_message
            if storage_kind:
                self.save_timestamps.append(float(timestamp))
                self.save_history.append(storage_duration)
                self.save_kind_history.append(storage_kind)
            if storage_bytes_in > 0 or storage_bytes_out > 0:
                self.compression_timestamps.append(float(timestamp))
                self.compression_history.append(storage_ratio_pct)
        if auto_overlay and overlay_snapshot is not None:
            _ = self._write_overlay_frame(
                overlay_dir,
                metadata,
                overlay_snapshot,
                quiet=True,
                recorded_at=timestamp,
            )

    def request_overlay_refresh(self) -> None:
        with self.lock:
            self.overlay_needs_refresh = True
            self.display_cache.clear()

    def update_latency_metrics(self, request_latency_ms: float, render_latency_ms: float) -> None:
        with self.lock:
            self.latest_request_latency_ms = float(request_latency_ms)
            self.latest_render_latency_ms = float(render_latency_ms)
            self.render_history.append(float(render_latency_ms))

    def rebuild_overlay_if_needed(self) -> None:
        with self.lock:
            if not self.overlay_needs_refresh or self.latest_image_array is None:
                return
            if self.latest_image_uint8 is not None:
                base_image = np.array(self.latest_image_uint8, copy=True)
            else:
                base_image = np.array(self.latest_image_array, copy=True)
            detections_copy = [dict(det) for det in self.tracks]
            params = self.tracking_params.to_dict()
            show_grid = self.show_tile_grid
            overlay_buffer = self._ensure_overlay_buffer((base_image.shape[0], base_image.shape[1]))
            overlay = create_overlay(
                base_image,
                detections_copy,
                params,
                show_grid,
                self.use_mass_colormap,
                self.mass_cutoff,
                self.cutoff_below_color,
                self.cutoff_above_color,
                self.circle_size_scale,
                out=overlay_buffer,
            )
            self.latest_overlay_array = overlay
            self.overlay_needs_refresh = False
            self.display_cache.clear()
            if self.display_mode == "overlay":
                self.texture_dirty = True

    def get_render_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            texture_array: Optional[np.ndarray] = None
            if self.texture_dirty:
                if self.display_mode == "overlay" and self.latest_overlay_array is not None:
                    texture_array = self.latest_overlay_array.copy()
                elif self.latest_image_array is not None:
                    texture_array = self.latest_image_array.copy()
                self.texture_dirty = False
            snapshot = {
                "host": self.host,
                "port": self.port,
                "connected": self.connected,
                "status_message": self.status_message,
                "sequence": self.latest_sequence,
                "filename": self.latest_filename,
                "timestamp_ms": self.latest_timestamp_ms,
                "source": self.latest_source,
                "detection_count": self.detection_count,
                "latency_ms": self.latest_latency_ms,
                "processing_ms": self.latest_processing_ms,
                "request_latency_ms": self.latest_request_latency_ms,
                "render_latency_ms": self.latest_render_latency_ms,
                "texture_array": texture_array,
                "display_mode": self.display_mode,
                "auto_save_raw": self.auto_save_raw,
                "auto_save_overlay": self.auto_save_overlay,
                "save_to_hdf5": self.save_to_hdf5,
                "raw_dir": str(self.raw_save_dir),
                "overlay_dir": str(self.overlay_save_dir),
                "hdf5_path": str(self.hdf5_path) if self.hdf5_path else "",
                "tile_grid": self.show_tile_grid,
                "use_mass_colormap": self.use_mass_colormap,
                "mass_cutoff": self.mass_cutoff,
                "below_cutoff_color": self.cutoff_below_color,
                "above_cutoff_color": self.cutoff_above_color,
                "circle_size_scale": self.circle_size_scale,
                "zoom": self.zoom,
                "save_duration_ms": self.latest_save_duration_ms,
                "save_kind": self.latest_save_kind,
                "storage_path": self.latest_storage_path,
                "storage_saved": self.latest_storage_saved,
                "storage_format": self.storage_image_format,
                "storage_target_fps": self.storage_target_fps,
                "storage_ratio": self.latest_storage_ratio,
                "storage_codec": self.latest_storage_codec,
                "storage_bytes_in": self.latest_storage_bytes_in,
                "storage_bytes_out": self.latest_storage_bytes_out,
                "storage_throttle_ms": self.latest_throttle_ms,
                "storage_message": self.latest_storage_message,
                "texture_tag": self.texture_tag,
                "texture_size": self.texture_size,
                "field_updates": dict(self.pending_field_updates),
                "tracking_params": self.tracking_params.to_dict(),
                "tracking_params_path": str(self.last_tracking_params_path) if self.last_tracking_params_path else "",
                "metrics_history": {
                    "timestamps": list(self.metric_timestamps),
                    "latency": list(self.latency_history),
                    "processing": list(self.processing_history),
                    "render": list(self.render_history),
                    "features": list(self.feature_history),
                    "save": {
                        "timestamps": list(self.save_timestamps),
                        "durations": list(self.save_history),
                        "kinds": list(self.save_kind_history),
                    },
                    "compression": {
                        "timestamps": list(self.compression_timestamps),
                        "ratios": list(self.compression_history),
                    },
                },
            }
            self.pending_field_updates.clear()
            return snapshot

    def _write_overlay_frame(
        self,
        directory: Optional[Path],
        metadata: Dict[str, Any],
        overlay_array: np.ndarray,
        quiet: bool = False,
        recorded_at: Optional[float] = None,
    ) -> Optional[Path]:
        if directory is None:
            return None
        recorded_time = float(recorded_at if recorded_at is not None else time.time())
        start = time.perf_counter()
        status_message: Optional[str] = None
        kind = "overlay"
        record_metric = True
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stem = Path(metadata.get("filename") or f"frame_{metadata.get('sequence', 0):06d}").stem
            target = directory / f"{stem}_tracked.png"
            _save_overlay_image(target, overlay_array)
            if not quiet:
                status_message = f"Saved overlay image to {target}"
            return target
        except Exception:
            kind = "overlay-error"
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            if record_metric:
                self._record_save_metric(duration_ms, recorded_time, kind)
            if status_message:
                self.set_status(status_message)

    def save_overlay_frame(self) -> Optional[Path]:
        with self.lock:
            if self.latest_overlay_array is None:
                return None
            metadata = {
                "filename": self.latest_filename,
                "sequence": self.latest_sequence,
            }
            overlay_copy = self.latest_overlay_array.copy()
            directory = self.overlay_save_dir
        return self._write_overlay_frame(
            directory,
            metadata,
            overlay_copy,
            quiet=False,
            recorded_at=time.time(),
        )

    def save_tracking_params_to_file(self, path: Path) -> Path:
        target = Path(path)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.tracking_params.to_dict()
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with self.lock:
            self.last_tracking_params_path = target
        self.set_status(f"Saved tracking parameters to {target}")
        self.schedule_field_update("tracking_params_path", str(target))
        return target

    def load_tracking_params_from_file(self, path: Path) -> Dict[str, Any]:
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Tracking parameter file must contain a JSON object")
        self.update_tracking_params(data, from_remote=True)
        with self.lock:
            self.last_tracking_params_path = source
        self.set_status(f"Loaded tracking parameters from {source}")
        self.schedule_field_update("tracking_params_path", str(source))
        return data

    def shutdown(self) -> None:
        with self.lock:
            self._close_hdf5_locked()


def _ensure_unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _save_overlay_image(path: Path, overlay_array: np.ndarray) -> None:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        if tifffile is not None and path.suffix.lower() not in {".tif", ".tiff"}:
            tif_path = path.with_suffix(".tiff")
            tifffile.imwrite(str(tif_path), overlay_array)  # type: ignore[arg-type]
        else:
            tifffile.imwrite(str(path), overlay_array)  # type: ignore[arg-type]
        return
    image = Image.fromarray(overlay_array)
    image.save(path)


def parse_track_payload(payload: struct_pb2.Struct) -> Dict[str, Any]:
    data = json_format.MessageToDict(payload, preserving_proto_field_name=True)
    has_tracks = bool(data.get("has_tracks", False))
    detections_raw = data.get("detections", []) if has_tracks else []
    detections: List[Dict[str, Any]] = []
    for det in detections_raw:
        detections.append(
            {
                "x": float(det.get("x", 0.0)),
                "y": float(det.get("y", 0.0)),
                "mass": float(det.get("mass", 0.0)),
                "ecc": float(det.get("ecc", 0.0)),
                "size": float(det.get("size", 0.0)),
                "signal": float(det.get("signal", 0.0)),
            }
        )
    detection_count = int(data.get("detection_count", len(detections))) if has_tracks else 0
    return {
        "has_tracks": has_tracks,
        "detections": detections,
        "detection_count": detection_count,
        "latency_ms": int(data.get("latency_ms", 0)),
        "processing_ms": int(data.get("processing_ms", 0)),
        "image_height": int(data.get("image_height", 0)),
        "image_width": int(data.get("image_width", 0)),
    }


class ImageClient:
    def __init__(self, state: AppState) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[ImageExchangeStub] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="image-poll", daemon=True)
        self._last_sequence = 0
        self._active_stream: Optional[Any] = None
        self._last_config_signature: Optional[str] = None
        self._thread.start()

    def connect(self, host: str, port: int) -> None:
        address = f"{host}:{port}"
        options = [
            ("grpc.max_send_message_length", MAX_GRPC_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", MAX_GRPC_MESSAGE_BYTES),
        ]
        channel = grpc.insecure_channel(address, options=options)
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        except grpc.FutureTimeoutError:
            channel.close()
            raise RuntimeError(f"Unable to reach server at {address}")
        stub = ImageExchangeStub(channel)
        self._cancel_active_stream()
        with self._lock:
            if self._channel is not None:
                self._channel.close()
            self._channel = channel
            self._stub = stub
            self._last_sequence = 0
            self._last_config_signature = None
        self._state.set_connection_info(host, port, True)
        self._state.set_status(f"Connected to {address}")
        try:
            self.refresh_tracking_config()
        except Exception as exc:
            LOGGER.warning("Failed to fetch tracking config on connect: %s", exc)
            self._state.set_status(f"Connected with config fetch error: {exc}")
        try:
            self.refresh_storage_config()
        except Exception as exc:
            LOGGER.warning("Failed to fetch storage config on connect: %s", exc)
            self._state.set_status(f"Storage config fetch error: {exc}")

    def disconnect(self) -> None:
        self._cancel_active_stream()
        with self._lock:
            if self._channel is not None:
                self._channel.close()
            self._channel = None
            self._stub = None
            self._last_sequence = 0
            self._last_config_signature = None
        self._state.set_connection_info(self._state.host, self._state.port, False)
        self._state.set_status("Disconnected")

    def shutdown(self) -> None:
        self._stop.set()
        self._cancel_active_stream()
        self._thread.join(timeout=2)
        with self._lock:
            if self._channel is not None:
                self._channel.close()
            self._channel = None
            self._stub = None

    def _cancel_active_stream(self) -> None:
        with self._lock:
            if self._active_stream is not None:
                try:
                    self._active_stream.cancel()
                except Exception:
                    pass
                self._active_stream = None

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            with self._lock:
                stub = self._stub
            if stub is None:
                time.sleep(0.25)
                continue
            try:
                self._consume_stream(stub)
                backoff = 1.0
            except grpc.RpcError as exc:
                if self._stop.is_set():
                    break
                LOGGER.warning("Frame stream failed: %s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)
            except Exception as exc:  # pragma: no cover - defensive catch
                if self._stop.is_set():
                    break
                LOGGER.exception("Unexpected frame stream failure: %s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)

    def _consume_stream(self, stub: ImageExchangeStub) -> None:
        call = stub.StreamFrames(empty_pb2.Empty())
        with self._lock:
            self._active_stream = call
        try:
            for envelope in call:
                if self._stop.is_set():
                    break
                with self._lock:
                    if self._stub is not stub:
                        break
                try:
                    self._handle_envelope(envelope)
                except Exception as exc:  # pragma: no cover - frame processing guard
                    LOGGER.exception("Failed to process streamed frame: %s", exc)
            call.cancel()
        except grpc.RpcError as exc:
            call.cancel()
            if exc.code() == grpc.StatusCode.CANCELLED and (self._stop.is_set() or self._stub is not stub):
                return
            raise
        finally:
            with self._lock:
                if self._active_stream is call:
                    self._active_stream = None

    def _handle_envelope(self, envelope: Any) -> None:
        metadata_proto = envelope.metadata
        sequence = int(getattr(metadata_proto, "sequence", 0))
        if sequence <= self._last_sequence:
            return
        start = time.perf_counter()
        image_format = (envelope.image_format or "jpeg").lower()
        raw_bytes = bytes(envelope.image_data)
        width = int(getattr(metadata_proto, "image_width", 0))
        height = int(getattr(metadata_proto, "image_height", 0))
        image_uint8, image_float = self._decode_frame(raw_bytes, image_format, width, height)
        metadata = {
            "sequence": sequence,
            "filename": getattr(metadata_proto, "filename", ""),
            "timestamp_ms": int(getattr(metadata_proto, "timestamp_ms", 0)),
            "source": getattr(metadata_proto, "source", ""),
            "image_format": image_format,
        }
        # Attach storage telemetry so the dashboard reports server-side saving work.
        metadata.update(
            {
                "storage_ms": float(getattr(metadata_proto, "storage_ms", 0.0)),
                "storage_saved": bool(getattr(metadata_proto, "storage_saved", False)),
                "storage_kind": str(getattr(metadata_proto, "storage_kind", "")),
                "storage_path": str(getattr(metadata_proto, "storage_path", "")),
                "storage_codec": str(getattr(metadata_proto, "storage_codec", "")),
                "storage_ratio": float(getattr(metadata_proto, "storage_ratio", 0.0)),
                "storage_bytes_in": int(getattr(metadata_proto, "storage_bytes_in", 0)),
                "storage_bytes_out": int(getattr(metadata_proto, "storage_bytes_out", 0)),
                "storage_throttle_ms": float(getattr(metadata_proto, "storage_throttle_ms", 0.0)),
                "storage_message": str(getattr(metadata_proto, "storage_message", "")),
            }
        )
        detections = [
            {
                "x": float(det.x),
                "y": float(det.y),
                "mass": float(det.mass),
                "ecc": float(det.ecc),
                "size": float(det.size),
                "signal": float(det.signal),
            }
            for det in envelope.detections
        ]
        track_info = {
            "detections": detections,
            "detection_count": int(getattr(envelope, "detection_count", len(detections))),
            "latency_ms": int(getattr(metadata_proto, "latency_ms", 0)),
            "processing_ms": int(getattr(metadata_proto, "processing_ms", 0)),
        }
        with self._state.lock:
            overlay_params = self._state.tracking_params.to_dict()
            show_grid = self._state.show_tile_grid
        timestamp_s = time.time()
        self._state.update_latest_frame(
            metadata,
            raw_bytes,
            image_float,
            image_uint8,
            track_info,
            image_format,
            overlay_params,
            show_grid,
            timestamp_s,
        )
        render_latency_ms = (time.perf_counter() - start) * 1000.0
        self._state.update_latency_metrics(0.0, render_latency_ms)
        self._last_sequence = sequence

        if envelope.HasField("tracking_config_snapshot"):
            snapshot_dict = json_format.MessageToDict(
                envelope.tracking_config_snapshot, preserving_proto_field_name=True
            )
            signature = json.dumps(snapshot_dict, sort_keys=True)
            if signature != self._last_config_signature:
                self._state.update_tracking_params(snapshot_dict, from_remote=True)
                self._last_config_signature = signature

    def _decode_frame(
        self,
        payload: bytes,
        image_format: str,
        width: int,
        height: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        fmt = (image_format or "jpeg").lower()
        decoded: Optional[np.ndarray] = None
        if fmt in {"jpeg", "jpg", "png"} and cv2 is not None:
            buffer = np.frombuffer(payload, dtype=np.uint8)
            decoded = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)  # type: ignore[attr-defined]
            if decoded is None:
                raise RuntimeError("OpenCV failed to decode image data")
        elif fmt in {"tiff", "tif"} and tifffile is not None:
            with tifffile.TiffFile(io.BytesIO(payload)) as tif:  # type: ignore[arg-type]
                decoded = tif.asarray()
        elif fmt in {"raw", "raw8"}:
            if width <= 0 or height <= 0:
                raise ValueError("Raw frame missing image dimensions")
            decoded = np.frombuffer(payload, dtype=np.uint8).reshape(height, width)
        else:  # fallback to Pillow if available
            try:
                from PIL import Image  # type: ignore
            except ImportError as exc:
                raise RuntimeError(f"Unable to decode image format {fmt!r}") from exc
            with Image.open(io.BytesIO(payload)) as img:  # type: ignore[name-defined]
                decoded = np.array(img.convert("L"))

        if decoded is None:
            raise RuntimeError(f"Unable to decode image format {fmt!r}")
        decoded_arr = cast(np.ndarray, decoded)
        if decoded_arr.ndim > 2:
            decoded_arr = np.mean(decoded_arr, axis=-1)
        if decoded_arr.dtype != np.uint8:
            decoded_arr = _normalize_to_uint8(decoded_arr)
        decoded_uint8 = np.ascontiguousarray(decoded_arr.astype(np.uint8, copy=False))
        image_float = decoded_uint8.astype(np.float32)
        return decoded_uint8, image_float

    def refresh_tracking_config(self) -> None:
        with self._lock:
            stub = self._stub
        if stub is None:
            raise RuntimeError("Not connected")
        response = stub.GetTrackingConfig(empty_pb2.Empty(), timeout=5)
        config = json_format.MessageToDict(response, preserving_proto_field_name=True)
        self._state.update_tracking_params(config, from_remote=True)

    def refresh_storage_config(self) -> Dict[str, Any]:
        with self._lock:
            stub = self._stub
        if stub is None:
            raise RuntimeError("Not connected")
        response = stub.GetStorageConfig(empty_pb2.Empty(), timeout=5)
        data = json_format.MessageToDict(response, preserving_proto_field_name=True)
        data["enabled"] = bool(response.enabled)
        data["target_fps"] = float(response.target_fps)
        data["image_format"] = str(response.image_format)
        data["output_dir"] = str(response.output_dir)
        data["hdf5_enabled"] = bool(response.hdf5_enabled)
        data["hdf5_path"] = str(response.hdf5_path)
        self._state.set_storage_config(data, from_remote=True)
        return data

    def update_tracking_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            stub = self._stub
        if stub is None:
            raise RuntimeError("Not connected")
        request = struct_pb2.Struct()
        json_format.ParseDict(payload, request)
        response = stub.UpdateTrackingConfig(request, timeout=5)
        data = json_format.MessageToDict(response, preserving_proto_field_name=True)
        if not data.get("ok", False):
            raise RuntimeError(data.get("error", "tracking update failed"))
        config = data.get("config", {})
        if isinstance(config, dict):
            self._state.update_tracking_params(config, from_remote=True)
        return config

    def update_storage_config(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            stub = self._stub
        if stub is None:
            raise RuntimeError("Not connected")
        current = {
            "enabled": self._state.auto_save_raw,
            "target_fps": self._state.storage_target_fps,
            "image_format": self._state.storage_image_format,
            "output_dir": str(self._state.raw_save_dir),
            "hdf5_enabled": self._state.save_to_hdf5,
            "hdf5_path": str(self._state.hdf5_path) if self._state.hdf5_path else "",
        }
        current.update(overrides)
        request = StorageConfig()
        request.enabled = bool(current["enabled"])
        request.target_fps = float(current["target_fps"])
        request.image_format = "native"
        request.output_dir = str(current["output_dir"])
        request.hdf5_enabled = bool(current["hdf5_enabled"])
        request.hdf5_path = str(current["hdf5_path"] or "")
        response = stub.UpdateStorageConfig(request, timeout=5)
        data = json_format.MessageToDict(response, preserving_proto_field_name=True)
        data["enabled"] = bool(response.enabled)
        data["target_fps"] = float(response.target_fps)
        data["image_format"] = str(response.image_format)
        data["output_dir"] = str(response.output_dir)
        data["hdf5_enabled"] = bool(response.hdf5_enabled)
        data["hdf5_path"] = str(response.hdf5_path)
        self._state.set_storage_config(data, from_remote=True)
        return data




@dataclass
class UIIds:
    texture_registry: str
    image_texture: str
    image_widget: str
    status_text: str
    sequence_text: str
    detection_text: str
    latency_text: str
    processing_text: str
    request_latency_text: str
    render_latency_text: str
    tracking_params_path_text: str
    metrics_window: str
    history_limit_slider: str
    latency_plot: str
    latency_x_axis: str
    latency_y_axis: str
    latency_series: str
    processing_plot: str
    processing_x_axis: str
    processing_y_axis: str
    processing_series: str
    render_plot: str
    render_x_axis: str
    render_y_axis: str
    render_series: str
    save_plot: str
    save_x_axis: str
    save_y_axis: str
    save_series: str
    compression_plot: str
    compression_x_axis: str
    compression_y_axis: str
    compression_series: str
    features_plot: str
    features_x_axis: str
    features_y_axis: str
    features_series: str
    save_text: str
    storage_ratio_text: str
    storage_codec_text: str
    storage_bytes_text: str
    storage_throttle_text: str
    storage_message_text: str
    storage_format_text: str
    host_input: str
    port_input: str
    connect_button: str
    disconnect_button: str
    display_mode_combo: str
    tile_grid_checkbox: str
    auto_save_raw_checkbox: str
    auto_save_overlay_checkbox: str
    save_hdf5_checkbox: str
    raw_dir_display: str
    overlay_dir_display: str
    hdf5_path_display: str
    raw_dir_dialog: str
    overlay_dir_dialog: str
    tracking_save_dialog: str
    tracking_load_dialog: str
    save_overlay_button: str
    zoom_slider: str
    use_colormap_checkbox: str
    mass_cutoff_input: str
    below_color_picker: str
    above_color_picker: str
    circle_scale_slider: str
    tracking_apply_button: str
    tracking_reset_button: str
    tracking_inputs: Dict[str, str]
    storage_target_fps_input: str


def create_ui(state: AppState, client: ImageClient) -> UIIds:
    dpg.create_context()
    dpg.configure_app(docking=True, docking_space=True)
    with dpg.texture_registry() as texture_registry:
        init_view = state._ensure_texture_buffer(DEFAULT_TEXTURE_SIZE[0], DEFAULT_TEXTURE_SIZE[1])
        init_view[:, :, 0:3].fill(0.0)
        init_view[:, :, 3].fill(1.0)
        image_texture = dpg.add_raw_texture(
            DEFAULT_TEXTURE_SIZE[0],
            DEFAULT_TEXTURE_SIZE[1],
            state.texture_buffer,
            format=dpg.mvFormat_Float_rgba,
            tag="image_texture_0",
        )
    state.set_texture_info("image_texture_0", DEFAULT_TEXTURE_SIZE)

    host_input = "host_input"
    port_input = "port_input"
    display_mode_combo = "display_mode_combo"
    tile_grid_checkbox = "tile_grid_checkbox"
    auto_save_raw_checkbox = "auto_save_raw_checkbox"
    auto_save_overlay_checkbox = "auto_save_overlay_checkbox"
    save_hdf5_checkbox = "save_hdf5_checkbox"
    raw_dir_display = "raw_dir_display"
    overlay_dir_display = "overlay_dir_display"
    hdf5_path_display = "hdf5_path_display"
    raw_dir_dialog = "raw_dir_dialog"
    overlay_dir_dialog = "overlay_dir_dialog"
    tracking_save_dialog = "tracking_save_dialog"
    tracking_load_dialog = "tracking_load_dialog"
    save_overlay_button = "save_overlay_button"
    storage_target_fps_input = "storage_target_fps_input"
    storage_format_text = "storage_format_text"
    status_text = "status_text"
    sequence_text = "sequence_text"
    detection_text = "detection_text"
    latency_text = "latency_text"
    processing_text = "processing_text"
    request_latency_text = "request_latency_text"
    render_latency_text = "render_latency_text"
    save_text = "save_text"
    storage_ratio_text = "storage_ratio_text"
    storage_codec_text = "storage_codec_text"
    storage_bytes_text = "storage_bytes_text"
    storage_throttle_text = "storage_throttle_text"
    storage_message_text = "storage_message_text"
    tracking_params_path_text = "tracking_params_path_text"
    zoom_slider = "zoom_slider"
    metrics_window = "metrics_window"
    history_limit_slider = "metrics_history_slider"
    latency_plot = "latency_plot"
    latency_x_axis = "latency_x_axis"
    latency_y_axis = "latency_y_axis"
    latency_series = "latency_series"
    processing_plot = "processing_plot"
    processing_x_axis = "processing_x_axis"
    processing_y_axis = "processing_y_axis"
    processing_series = "processing_series"
    render_plot = "render_plot"
    render_x_axis = "render_x_axis"
    render_y_axis = "render_y_axis"
    render_series = "render_series"
    save_plot = "save_plot"
    save_x_axis = "save_x_axis"
    save_y_axis = "save_y_axis"
    save_series = "save_series"
    compression_plot = "compression_plot"
    compression_x_axis = "compression_x_axis"
    compression_y_axis = "compression_y_axis"
    compression_series = "compression_series"
    features_plot = "features_plot"
    features_x_axis = "features_x_axis"
    features_y_axis = "features_y_axis"
    features_series = "features_series"
    use_colormap_checkbox = "use_colormap_checkbox"
    mass_cutoff_input = "mass_cutoff_input"
    below_color_picker = "below_color_picker"
    above_color_picker = "above_color_picker"
    circle_scale_slider = "circle_scale_slider"
    tracking_inputs: Dict[str, str] = {}

    with dpg.file_dialog(directory_selector=True, show=False, callback=_on_raw_dir_selected, tag=raw_dir_dialog):
        dpg.add_file_extension("")
    with dpg.file_dialog(directory_selector=True, show=False, callback=_on_overlay_dir_selected, tag=overlay_dir_dialog):
        dpg.add_file_extension("")
    with dpg.file_dialog(show=False, callback=_on_tracking_save_selected, tag=tracking_save_dialog, default_filename="tracking_params.json"):
        dpg.add_file_extension(".json")
        dpg.add_file_extension(".*")
    with dpg.file_dialog(show=False, callback=_on_tracking_load_selected, tag=tracking_load_dialog):
        dpg.add_file_extension(".json")
        dpg.add_file_extension(".*")

    with dpg.window(tag="main_dockspace", label="DockSpace", pos=(0, 0), width=1500, height=900, no_title_bar=True, no_move=True, no_resize=True, no_close=True):
        pass

    with dpg.window(label="Connection", pos=(10, 10), width=300, height=180, tag="connection_window"):
        dpg.add_text("Server")
        dpg.add_input_text(label="Host", default_value=state.host, tag=host_input)
        dpg.add_input_int(label="Port", default_value=state.port, tag=port_input)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Connect", tag="connect_button", callback=_on_connect_clicked, user_data=(state, client, host_input, port_input))
            dpg.add_button(label="Disconnect", tag="disconnect_button", callback=_on_disconnect_clicked, user_data=(state, client))

    with dpg.window(label="Display Controls", pos=(320, 10), width=260, height=330, tag="display_window"):
        dpg.add_text("Display Mode")
        dpg.add_combo(
            label="##display_mode",
            items=["overlay", "raw"],
            default_value=state.display_mode,
            tag=display_mode_combo,
            callback=_on_display_mode_changed,
            user_data=(state,),
        )
        dpg.add_checkbox(label="Show Tile Grid", default_value=state.show_tile_grid, tag=tile_grid_checkbox, callback=_on_tile_grid_toggled, user_data=(state,))
        dpg.add_slider_float(
            label="Display Scale",
            default_value=state.zoom,
            min_value=0.1,
            max_value=4.0,
            format="%.2f",
            tag=zoom_slider,
            callback=_on_zoom_changed,
            user_data=(state,),
        )
        dpg.add_separator()
        dpg.add_text("Overlay Appearance")
        dpg.add_checkbox(
            label="Use Mass Colormap",
            default_value=state.use_mass_colormap,
            tag=use_colormap_checkbox,
            callback=_on_use_colormap_toggled,
            user_data=(state,),
        )
        dpg.add_input_float(
            label="Mass Cutoff",
            default_value=state.mass_cutoff,
            format="%.2f",
            tag=mass_cutoff_input,
            callback=_on_mass_cutoff_changed,
            user_data=(state,),
            enabled=not state.use_mass_colormap,
        )
        dpg.add_color_edit(
            label="Below Cutoff",
            default_value=_rgb_to_dpg_color(state.cutoff_below_color),
            tag=below_color_picker,
            no_alpha=True,
            callback=_on_cutoff_color_changed,
            user_data=(state, "below"),
            enabled=not state.use_mass_colormap,
        )
        dpg.add_color_edit(
            label="Above Cutoff",
            default_value=_rgb_to_dpg_color(state.cutoff_above_color),
            tag=above_color_picker,
            no_alpha=True,
            callback=_on_cutoff_color_changed,
            user_data=(state, "above"),
            enabled=not state.use_mass_colormap,
        )
        dpg.add_slider_float(
            label="Circle Size Scale",
            default_value=state.circle_size_scale,
            min_value=0.25,
            max_value=3.0,
            format="%.2f",
            tag=circle_scale_slider,
            callback=_on_circle_scale_changed,
            user_data=(state,),
        )

    with dpg.window(label="Saving & Capture", pos=(590, 10), width=360, height=320, tag="saving_window"):
        dpg.add_checkbox(
            label="Server File Saving",
            default_value=state.auto_save_raw,
            tag=auto_save_raw_checkbox,
            callback=_on_auto_save_raw_toggled,
            user_data=(state, client),
        )
        dpg.add_input_float(
            label="Target FPS",
            default_value=state.storage_target_fps,
            min_value=0.0,
            max_value=240.0,
            format="%.2f",
            step=0.1,
            tag=storage_target_fps_input,
            callback=_on_storage_fps_changed,
            user_data=(state, client),
        )
        dpg.add_text(
            f"Storage format: {state.storage_image_format} (TIFF files + HDF5 bitshuffle)",
            tag=storage_format_text,
        )
        dpg.add_checkbox(
            label="Server HDF5 Recording",
            default_value=state.save_to_hdf5,
            tag=save_hdf5_checkbox,
            callback=_on_save_hdf5_toggled,
            user_data=(state, client),
            enabled=True,
        )
        if h5py is None:
            with dpg.tooltip(save_hdf5_checkbox):
                dpg.add_text("Install h5py to enable HDF5 saving")
        dpg.add_spacer(height=4)
        dpg.add_button(label="Select Raw Folder", callback=_open_dialog, user_data=raw_dir_dialog)
        dpg.add_input_text(label="Raw Folder", default_value=str(state.raw_save_dir), readonly=True, tag=raw_dir_display)
        dpg.add_input_text(
            label="HDF5 File",
            default_value=str(state.hdf5_path) if state.hdf5_path else "(auto)",
            readonly=True,
            tag=hdf5_path_display,
        )
        dpg.add_button(label="Select Overlay Folder", callback=_open_dialog, user_data=overlay_dir_dialog)
        dpg.add_input_text(label="Overlay Folder", default_value=str(state.overlay_save_dir), readonly=True, tag=overlay_dir_display)
        dpg.add_spacer(height=4)
        dpg.add_button(label="Save Current Overlay", tag=save_overlay_button, callback=_on_save_overlay_clicked, user_data=(state,))

    with dpg.window(label="Status", pos=(960, 10), width=320, height=300, tag="stats_window"):
        dpg.add_text("Status", tag=status_text)
        dpg.add_text("Sequence", tag=sequence_text)
        dpg.add_text("Detections", tag=detection_text)
        dpg.add_separator()
        dpg.add_text("Frame Latency", tag=latency_text)
        dpg.add_text("Processing", tag=processing_text)
        dpg.add_text("Request Latency", tag=request_latency_text)
        dpg.add_text("Render Prep", tag=render_latency_text)
        dpg.add_text("Last Save", tag=save_text)
        dpg.add_text("Compression", tag=storage_ratio_text)
        dpg.add_text("Codec", tag=storage_codec_text)
        dpg.add_text("Bytes", tag=storage_bytes_text)
        dpg.add_text("Throttle", tag=storage_throttle_text)
        dpg.add_text("Save Message", tag=storage_message_text, wrap=300)

    with dpg.window(label="Performance Metrics", pos=(1290, 10), width=360, height=420, tag=metrics_window):
        dpg.add_slider_int(
            label="History Points",
            default_value=state.metrics_history_limit,
            min_value=100,
            max_value=50_000,
            format="%d samples",
            tag=history_limit_slider,
            callback=_on_history_limit_changed,
            user_data=(state,),
        )
        dpg.add_separator()
        with dpg.child_window(width=-1, height=-1, border=False):
            with dpg.plot(label="Frame Latency (ms)", height=130, width=-1, tag=latency_plot):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag=latency_x_axis)
                latency_axis_y = dpg.add_plot_axis(dpg.mvYAxis, label="Latency", tag=latency_y_axis)
                dpg.add_line_series([], [], parent=latency_axis_y, tag=latency_series)
            dpg.add_spacer(height=6)
            with dpg.plot(label="Processing (ms)", height=130, width=-1, tag=processing_plot):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag=processing_x_axis)
                processing_axis_y = dpg.add_plot_axis(dpg.mvYAxis, label="Processing", tag=processing_y_axis)
                dpg.add_line_series([], [], parent=processing_axis_y, tag=processing_series)
            dpg.add_spacer(height=6)
            with dpg.plot(label="Render Prep (ms)", height=130, width=-1, tag=render_plot):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag=render_x_axis)
                render_axis_y = dpg.add_plot_axis(dpg.mvYAxis, label="Render", tag=render_y_axis)
                dpg.add_line_series([], [], parent=render_axis_y, tag=render_series)
            dpg.add_spacer(height=6)
            with dpg.plot(label="Save Duration (ms)", height=130, width=-1, tag=save_plot):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag=save_x_axis)
                save_axis_y = dpg.add_plot_axis(dpg.mvYAxis, label="Duration", tag=save_y_axis)
                dpg.add_line_series([], [], parent=save_axis_y, tag=save_series)
            dpg.add_spacer(height=6)
            with dpg.plot(label="Compression (%)", height=130, width=-1, tag=compression_plot):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag=compression_x_axis)
                compression_axis_y = dpg.add_plot_axis(dpg.mvYAxis, label="Ratio", tag=compression_y_axis)
                dpg.add_line_series([], [], parent=compression_axis_y, tag=compression_series)
            dpg.add_spacer(height=6)
            with dpg.plot(label="Detections", height=130, width=-1, tag=features_plot):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag=features_x_axis)
                features_axis_y = dpg.add_plot_axis(dpg.mvYAxis, label="Count", tag=features_y_axis)
                dpg.add_line_series([], [], parent=features_axis_y, tag=features_series)

    with dpg.window(label="Image Viewer", pos=(10, 230), width=940, height=640, tag="image_window"):
        dpg.add_text("Latest Frame")
        with dpg.child_window(width=-1, height=-1, border=True):
            image_widget = dpg.add_image(image_texture, tag="image_widget")

    with dpg.window(label="Tracking Parameters", pos=(960, 230), width=320, height=640, tag="tracking_window"):
        dpg.add_text("JSON Presets")
        with dpg.group(horizontal=True):
            dpg.add_button(label="Load JSON...", callback=_open_dialog, user_data=tracking_load_dialog)
            dpg.add_button(label="Save JSON...", callback=_open_dialog, user_data=tracking_save_dialog)
        dpg.add_text("Last JSON: (none)", tag=tracking_params_path_text, wrap=300)
        dpg.add_separator()
        dpg.add_text("Server Parameters")
        with dpg.child_window(width=-1, height=440, border=True):
            for field_name, label, widget_type in [
                ("diameter", "Diameter", int),
                ("separation", "Separation", int),
                ("percentile", "Percentile", float),
                ("minmass", "Min Mass", float),
                ("maxmass", "Max Mass", float),
                ("pixel_threshold", "Pixel Threshold", float),
                ("preprocess", "Enable Preprocess", bool),
                ("lshort", "Bandpass lshort", int),
                ("llong", "Bandpass llong", int),
                ("min_ecc", "Min Eccentricity", float),
                ("max_ecc", "Max Eccentricity", float),
                ("refine", "Refine Iterations", int),
                ("tile_width", "Tile Width", int),
                ("tile_height", "Tile Height", int),
                ("tile_overlap", "Tile Overlap", int),
                ("max_workers", "Max Workers", int),
            ]:
                tag = f"tracking_{field_name}"
                value = state.tracking_params.to_dict()[field_name]
                if widget_type is bool:
                    dpg.add_checkbox(label=label, default_value=bool(value), tag=tag)
                elif widget_type is int:
                    dpg.add_input_int(label=label, default_value=int(value), tag=tag)
                else:
                    dpg.add_input_float(label=label, default_value=float(value), tag=tag)
                tracking_inputs[field_name] = tag
        dpg.add_separator()
        dpg.add_button(label="Apply To Server", tag="tracking_apply_button", callback=_on_tracking_apply, user_data=(state, client, tracking_inputs))
        dpg.add_button(label="Reset To Defaults", tag="tracking_reset_button", callback=_on_tracking_reset, user_data=(state, client, tracking_inputs))

    dpg.create_viewport(title="Tweezer Tracking Dashboard", width=1700, height=920)
    dpg.setup_dearpygui()
    dpg.set_primary_window("main_dockspace", True)
    dpg.show_viewport()

    return UIIds(
        texture_registry=texture_registry,
        image_texture=image_texture,
        image_widget="image_widget",
        status_text=status_text,
        sequence_text=sequence_text,
        detection_text=detection_text,
        latency_text=latency_text,
        processing_text=processing_text,
        request_latency_text=request_latency_text,
        render_latency_text=render_latency_text,
        tracking_params_path_text=tracking_params_path_text,
        metrics_window=metrics_window,
        history_limit_slider=history_limit_slider,
        latency_plot=latency_plot,
        latency_x_axis=latency_x_axis,
        latency_y_axis=latency_y_axis,
        latency_series=latency_series,
        processing_plot=processing_plot,
        processing_x_axis=processing_x_axis,
        processing_y_axis=processing_y_axis,
        processing_series=processing_series,
        render_plot=render_plot,
        render_x_axis=render_x_axis,
        render_y_axis=render_y_axis,
        render_series=render_series,
        save_plot=save_plot,
        save_x_axis=save_x_axis,
        save_y_axis=save_y_axis,
        save_series=save_series,
    compression_plot=compression_plot,
    compression_x_axis=compression_x_axis,
    compression_y_axis=compression_y_axis,
    compression_series=compression_series,
        features_plot=features_plot,
        features_x_axis=features_x_axis,
        features_y_axis=features_y_axis,
        features_series=features_series,
        save_text=save_text,
    storage_ratio_text=storage_ratio_text,
    storage_codec_text=storage_codec_text,
    storage_bytes_text=storage_bytes_text,
    storage_throttle_text=storage_throttle_text,
    storage_message_text=storage_message_text,
    storage_format_text=storage_format_text,
        host_input=host_input,
        port_input=port_input,
        connect_button="connect_button",
        disconnect_button="disconnect_button",
        display_mode_combo=display_mode_combo,
        tile_grid_checkbox=tile_grid_checkbox,
        auto_save_raw_checkbox=auto_save_raw_checkbox,
        auto_save_overlay_checkbox=auto_save_overlay_checkbox,
        save_hdf5_checkbox=save_hdf5_checkbox,
        raw_dir_display=raw_dir_display,
        overlay_dir_display=overlay_dir_display,
        hdf5_path_display=hdf5_path_display,
        raw_dir_dialog=raw_dir_dialog,
        overlay_dir_dialog=overlay_dir_dialog,
        tracking_save_dialog=tracking_save_dialog,
        tracking_load_dialog=tracking_load_dialog,
        save_overlay_button=save_overlay_button,
        zoom_slider=zoom_slider,
        use_colormap_checkbox=use_colormap_checkbox,
        mass_cutoff_input=mass_cutoff_input,
        below_color_picker=below_color_picker,
        above_color_picker=above_color_picker,
        circle_scale_slider=circle_scale_slider,
        tracking_apply_button="tracking_apply_button",
        tracking_reset_button="tracking_reset_button",
    tracking_inputs=tracking_inputs,
    storage_target_fps_input=storage_target_fps_input,
    )


def update_ui(state: AppState, ids: UIIds) -> None:
    state.rebuild_overlay_if_needed()
    snapshot = state.get_render_snapshot()
    field_updates = snapshot.pop("field_updates", {})
    for key, value in field_updates.items():
        if key == "host":
            dpg.set_value(ids.host_input, value)
        elif key == "port":
            dpg.set_value(ids.port_input, int(value))
        elif key == "raw_dir":
            dpg.set_value(ids.raw_dir_display, value)
        elif key == "overlay_dir":
            dpg.set_value(ids.overlay_dir_display, value)
        elif key == "auto_save_raw":
            dpg.set_value(ids.auto_save_raw_checkbox, bool(value))
        elif key == "auto_save_overlay":
            dpg.set_value(ids.auto_save_overlay_checkbox, bool(value))
        elif key == "save_to_hdf5":
            dpg.set_value(ids.save_hdf5_checkbox, bool(value))
        elif key == "storage_target_fps":
            try:
                dpg.set_value(ids.storage_target_fps_input, float(value))
            except (TypeError, ValueError):
                pass
        elif key == "display_mode":
            dpg.set_value(ids.display_mode_combo, value)
        elif key == "tile_grid":
            dpg.set_value(ids.tile_grid_checkbox, bool(value))
        elif key == "use_mass_colormap":
            flag = bool(value)
            dpg.set_value(ids.use_colormap_checkbox, flag)
        elif key == "mass_cutoff":
            dpg.set_value(ids.mass_cutoff_input, float(value))
        elif key == "below_cutoff_color":
            color = _coerce_rgb_tuple(value, state.cutoff_below_color)
            dpg.set_value(ids.below_color_picker, _rgb_to_dpg_color(color))
        elif key == "above_cutoff_color":
            color = _coerce_rgb_tuple(value, state.cutoff_above_color)
            dpg.set_value(ids.above_color_picker, _rgb_to_dpg_color(color))
        elif key == "circle_size_scale":
            dpg.set_value(ids.circle_scale_slider, float(value))
        elif key == "metrics_history_limit":
            dpg.set_value(ids.history_limit_slider, int(value))
        elif key == "hdf5_path":
            display = str(value) if value else "(auto)"
            dpg.set_value(ids.hdf5_path_display, display)
        elif key == "tracking_params":
            params: Dict[str, Any] = value
            for field, tag in ids.tracking_inputs.items():
                if field not in params:
                    continue
                dpg.set_value(tag, params[field])
        elif key == "tracking_params_path":
            path_text = str(value)
            display = path_text if path_text else "(none)"
            dpg.set_value(ids.tracking_params_path_text, f"Last JSON: {display}")
    status_prefix = "Connected" if snapshot["connected"] else "Disconnected"
    status_message = snapshot.get("status_message") or ""
    combined_status = status_prefix if not status_message else f"{status_prefix} | {status_message}"
    dpg.set_value(ids.status_text, combined_status)
    sequence_line = f"Sequence {snapshot['sequence']} | {snapshot['filename']}"
    dpg.set_value(ids.sequence_text, sequence_line)
    zoom = float(snapshot.get("zoom", 1.0))
    display_dims = snapshot.get("texture_size") or (0, 0)
    if isinstance(display_dims, (list, tuple)) and len(display_dims) == 2:
        width_px, height_px = int(display_dims[0]), int(display_dims[1])
    else:
        width_px, height_px = 0, 0
    detail_suffix = ""
    if width_px > 0 and height_px > 0:
        detail_suffix = f" | Display {width_px}x{height_px} px @ {zoom:.2f}x"
    detection_line = f"Detections: {snapshot['detection_count']}{detail_suffix}"
    dpg.set_value(ids.detection_text, detection_line)
    dpg.set_value(ids.latency_text, f"Frame latency (server): {snapshot['latency_ms']} ms")
    dpg.set_value(ids.processing_text, f"Processing: {snapshot['processing_ms']} ms")
    request_latency = float(snapshot.get("request_latency_ms", 0.0))
    render_latency = float(snapshot.get("render_latency_ms", 0.0))
    dpg.set_value(ids.request_latency_text, f"Request latency: {request_latency:.1f} ms")
    dpg.set_value(ids.render_latency_text, f"Render prep: {render_latency:.1f} ms")
    dpg.set_value(ids.save_hdf5_checkbox, bool(snapshot.get("save_to_hdf5", state.save_to_hdf5)))
    hdf5_display = snapshot.get("hdf5_path") or "(auto)"
    if not isinstance(hdf5_display, str):
        hdf5_display = str(hdf5_display)
    dpg.set_value(ids.hdf5_path_display, hdf5_display)
    save_duration = float(snapshot.get("save_duration_ms", state.latest_save_duration_ms))
    save_kind = str(snapshot.get("save_kind", state.latest_save_kind or "idle"))
    kind_display = {
        "raw-hdf5": "raw (HDF5)",
        "raw-file": "raw (file)",
        "raw-error": "raw error",
        "overlay": "overlay",
        "overlay-error": "overlay error",
    }.get(save_kind, save_kind)
    dpg.set_value(ids.save_text, f"Last save ({kind_display}): {save_duration:.1f} ms")
    storage_ratio = float(snapshot.get("storage_ratio", state.latest_storage_ratio))
    storage_codec = str(snapshot.get("storage_codec", state.latest_storage_codec)) or "n/a"
    bytes_in = int(snapshot.get("storage_bytes_in", state.latest_storage_bytes_in))
    bytes_out = int(snapshot.get("storage_bytes_out", state.latest_storage_bytes_out))
    throttle_ms = float(snapshot.get("storage_throttle_ms", state.latest_throttle_ms))
    storage_message = str(snapshot.get("storage_message", state.latest_storage_message) or "")
    dpg.set_value(ids.storage_ratio_text, f"Compression: {storage_ratio:.1f}%")
    dpg.set_value(ids.storage_codec_text, f"Codec: {storage_codec}")
    dpg.set_value(ids.storage_bytes_text, f"Bytes: {bytes_out:,} / {bytes_in:,}")
    dpg.set_value(ids.storage_throttle_text, f"Throttle: {throttle_ms:.1f} ms")
    message_display = storage_message if storage_message else "(none)"
    dpg.set_value(ids.storage_message_text, f"Save Message: {message_display}")
    storage_format = str(snapshot.get("storage_format", state.storage_image_format) or "native")
    dpg.set_value(ids.storage_format_text, f"Storage format: {storage_format} (TIFF files + HDF5 bitshuffle)")
    use_colormap = bool(snapshot.get("use_mass_colormap", True))
    dpg.set_value(ids.use_colormap_checkbox, use_colormap)
    dpg.configure_item(ids.mass_cutoff_input, enabled=not use_colormap)
    dpg.configure_item(ids.below_color_picker, enabled=not use_colormap)
    dpg.configure_item(ids.above_color_picker, enabled=not use_colormap)
    mass_cutoff_value = float(snapshot.get("mass_cutoff", state.mass_cutoff))
    dpg.set_value(ids.mass_cutoff_input, mass_cutoff_value)
    below_color_value = _coerce_rgb_tuple(snapshot.get("below_cutoff_color"), state.cutoff_below_color)
    above_color_value = _coerce_rgb_tuple(snapshot.get("above_cutoff_color"), state.cutoff_above_color)
    dpg.set_value(ids.below_color_picker, _rgb_to_dpg_color(below_color_value))
    dpg.set_value(ids.above_color_picker, _rgb_to_dpg_color(above_color_value))
    circle_scale_value = float(snapshot.get("circle_size_scale", state.circle_size_scale))
    dpg.set_value(ids.circle_scale_slider, circle_scale_value)
    metrics = snapshot.get("metrics_history") or {}
    timestamps = metrics.get("timestamps") or []
    latency_values = metrics.get("latency") or []
    processing_values = metrics.get("processing") or []
    render_values = metrics.get("render") or []
    feature_values = metrics.get("features") or []
    save_metrics = metrics.get("save") or {}
    save_times = save_metrics.get("timestamps") or []
    save_values = save_metrics.get("durations") or []
    compression_metrics = metrics.get("compression") or {}
    compression_times = compression_metrics.get("timestamps") or []
    compression_values = compression_metrics.get("ratios") or []

    def _series_xy(values: List[float], time_values: List[float]) -> Tuple[List[float], List[float]]:
        if not values or not time_values:
            return ([], [])
        if len(values) != len(time_values):
            length = min(len(values), len(time_values))
            values = [float(v) for v in values[-length:]]
            time_subset = time_values[-length:]
        else:
            values = [float(v) for v in values]
            time_subset = time_values
        first = time_subset[0]
        x_vals = [float(t - first) for t in time_subset]
        return (x_vals, values)

    latency_xy = _series_xy(latency_values, timestamps)
    processing_xy = _series_xy(processing_values, timestamps)
    render_xy = _series_xy(render_values, timestamps)
    features_xy = _series_xy(feature_values, timestamps)
    save_xy = _series_xy(save_values, save_times)
    compression_xy = _series_xy(compression_values, compression_times)

    def _update_series(series_id: str, x_axis_id: str, y_axis_id: str, xy: Tuple[List[float], List[float]]) -> None:
        x_vals, y_vals = xy
        if not dpg.does_item_exist(series_id):
            return
        dpg.set_value(series_id, [x_vals, y_vals])
        if x_vals and dpg.does_item_exist(x_axis_id):
            dpg.fit_axis_data(x_axis_id)
        if y_vals and dpg.does_item_exist(y_axis_id):
            dpg.fit_axis_data(y_axis_id)

    _update_series(ids.latency_series, ids.latency_x_axis, ids.latency_y_axis, latency_xy)
    _update_series(ids.processing_series, ids.processing_x_axis, ids.processing_y_axis, processing_xy)
    _update_series(ids.render_series, ids.render_x_axis, ids.render_y_axis, render_xy)
    _update_series(ids.save_series, ids.save_x_axis, ids.save_y_axis, save_xy)
    _update_series(ids.compression_series, ids.compression_x_axis, ids.compression_y_axis, compression_xy)
    _update_series(ids.features_series, ids.features_x_axis, ids.features_y_axis, features_xy)
    params_path = snapshot.get("tracking_params_path") or ""
    display_path = params_path if params_path else "(none)"
    dpg.set_value(ids.tracking_params_path_text, f"Last JSON: {display_path}")
    dpg.set_value(ids.zoom_slider, zoom)
    texture_array = snapshot.get("texture_array")
    if texture_array is not None:
        display_array = _resample_for_display(texture_array, zoom)
        payload, width, height = state._texture_payload_from_image(display_array)
        current_tag = snapshot.get("texture_tag")
        current_size = snapshot.get("texture_size", (0, 0))
        if current_tag is None or (width, height) != tuple(current_size):
            new_tag = f"image_texture_{width}x{height}_{int(time.time()*1000)}"
            if current_tag and dpg.does_item_exist(current_tag):
                dpg.delete_item(current_tag)
            dpg.add_raw_texture(
                width,
                height,
                payload,
                format=dpg.mvFormat_Float_rgba,
                tag=new_tag,
                parent=ids.texture_registry,
            )
            dpg.configure_item(ids.image_widget, texture_tag=new_tag)
            state.set_texture_info(new_tag, (width, height))
        else:
            dpg.set_value(current_tag, payload)
        dpg.configure_item(ids.image_widget, width=width, height=height)


def _on_connect_clicked(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient, str, str]) -> None:
    state, client, host_tag, port_tag = user_data
    host = dpg.get_value(host_tag).strip()
    port = int(dpg.get_value(port_tag))
    try:
        client.connect(host, port)
    except Exception as exc:
        LOGGER.error("Connect failed: %s", exc)
        state.set_status(f"Connect failed: {exc}")


def _on_disconnect_clicked(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient]) -> None:
    state, client = user_data
    client.disconnect()
    state.set_status("Disconnected")
def _on_display_mode_changed(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    state.set_display_mode(str(app_data))


def _on_tile_grid_toggled(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    state.set_show_tile_grid(bool(app_data))


def _on_use_colormap_toggled(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    state.set_use_mass_colormap(bool(app_data))


def _on_mass_cutoff_changed(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    try:
        state.set_mass_cutoff(float(app_data))
    except (TypeError, ValueError):
        state.set_mass_cutoff(0.0)


def _on_cutoff_color_changed(sender: int, app_data: Sequence[float], user_data: Tuple[AppState, str]) -> None:
    state, which = user_data
    rgb = _dpg_color_to_rgb(app_data)
    if which == "below":
        state.set_cutoff_colors(below=rgb)
    else:
        state.set_cutoff_colors(above=rgb)


def _on_circle_scale_changed(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    try:
        state.set_circle_size_scale(float(app_data))
    except (TypeError, ValueError):
        state.set_circle_size_scale(1.0)


def _on_auto_save_raw_toggled(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient]) -> None:
    state, client = user_data
    desired = bool(app_data)
    try:
        client.update_storage_config({"enabled": desired})
    except Exception as exc:
        LOGGER.error("Failed to toggle storage saving: %s", exc)
        state.set_status(f"Storage toggle failed: {exc}")
        dpg.set_value(sender, state.auto_save_raw)
        return



def _on_auto_save_overlay_toggled(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    state.set_auto_save_overlay(bool(app_data))


def _on_storage_fps_changed(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient]) -> None:
    state, client = user_data
    try:
        value = max(0.0, float(app_data))
    except (TypeError, ValueError):
        value = state.storage_target_fps
    try:
        client.update_storage_config({"target_fps": value})
    except Exception as exc:
        LOGGER.error("Failed to update storage FPS: %s", exc)
        state.set_status(f"Storage FPS update failed: {exc}")
        dpg.set_value(sender, state.storage_target_fps)
        return


def _on_save_hdf5_toggled(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient]) -> None:
    state, client = user_data
    desired = bool(app_data)
    try:
        client.update_storage_config({"hdf5_enabled": desired})
    except Exception as exc:
        LOGGER.error("Failed to toggle HDF5 saving: %s", exc)
        state.set_status(f"HDF5 toggle failed: {exc}")
        dpg.set_value(sender, state.save_to_hdf5)


def _on_zoom_changed(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    state.set_zoom(float(app_data))


def _on_history_limit_changed(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    state.set_metrics_history_limit(int(app_data))


def _on_raw_dir_selected(sender: int, app_data: Dict[str, Any], user_data: Any) -> None:
    directory = app_data.get("file_path_name")
    if directory:
        state = _get_global_state()
        state.set_directories(raw_dir=directory)
        state.schedule_field_update("raw_dir", directory)


def _on_overlay_dir_selected(sender: int, app_data: Dict[str, Any], user_data: Any) -> None:
    directory = app_data.get("file_path_name")
    if directory:
        state = _get_global_state()
        state.set_directories(overlay_dir=directory)
        state.schedule_field_update("overlay_dir", directory)


def _open_dialog(sender: int, app_data: Any, user_data: str) -> None:
    dpg.show_item(user_data)


def _on_tracking_save_selected(sender: int, app_data: Dict[str, Any], user_data: Any) -> None:
    path_name = app_data.get("file_path_name")
    if not path_name:
        return
    state = _get_global_state()
    try:
        state.save_tracking_params_to_file(Path(path_name))
    except Exception as exc:
        LOGGER.error("Failed to save tracking parameters: %s", exc)
        state.set_status(f"Tracking save failed: {exc}")


def _on_tracking_load_selected(sender: int, app_data: Dict[str, Any], user_data: Any) -> None:
    path_name = app_data.get("file_path_name")
    if not path_name:
        return
    state = _get_global_state()
    try:
        state.load_tracking_params_from_file(Path(path_name))
    except Exception as exc:
        LOGGER.error("Failed to load tracking parameters: %s", exc)
        state.set_status(f"Tracking load failed: {exc}")

def _on_save_overlay_clicked(sender: int, app_data: Any, user_data: Tuple[AppState]) -> None:
    (state,) = user_data
    path = state.save_overlay_frame()
    if path is None:
        state.set_status("No overlay frame available to save")


def _on_tracking_apply(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient, Dict[str, str]]) -> None:
    state, client, mapping = user_data
    payload: Dict[str, Any] = {}
    for field, tag in mapping.items():
        payload[field] = dpg.get_value(tag)
    try:
        client.update_tracking_config(payload)
    except Exception as exc:
        LOGGER.error("Failed to update tracking parameters: %s", exc)
        state.set_status(f"Tracking update failed: {exc}")
    else:
        state.set_status("Tracking parameters updated")


def _on_tracking_reset(sender: int, app_data: Any, user_data: Tuple[AppState, ImageClient, Dict[str, str]]) -> None:
    state, client, mapping = user_data
    defaults = TrackingParameters().to_dict()
    for field, tag in mapping.items():
        default_value = defaults[field]
        dpg.set_value(tag, default_value)
    try:
        client.update_tracking_config(defaults)
    except Exception as exc:
        LOGGER.error("Failed to reset tracking parameters: %s", exc)
        state.set_status(f"Tracking reset failed: {exc}")
    else:
        state.set_status("Tracking parameters reset to defaults")


_GLOBAL_STATE: Optional[AppState] = None
_GLOBAL_CLIENT: Optional[ImageClient] = None


def _get_global_state() -> AppState:
    if _GLOBAL_STATE is None:
        raise RuntimeError("AppState not initialised")
    return _GLOBAL_STATE


def _get_global_client() -> ImageClient:
    if _GLOBAL_CLIENT is None:
        raise RuntimeError("ImageClient not initialised")
    return _GLOBAL_CLIENT


def run_gui(state: AppState, ids: UIIds) -> None:
    try:
        while dpg.is_dearpygui_running():
            try:
                update_ui(state, ids)
            except Exception as exc:  # pragma: no cover - defensive guard for UI loop
                LOGGER.exception("UI update failed: %s", exc)
            dpg.render_dearpygui_frame()
    finally:
        dpg.destroy_context()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DearPyGui dashboard for the Tweezer tracking image server")
    parser.add_argument("--host", default="127.0.0.1", help="Image server host")
    parser.add_argument("--port", type=int, default=50052, help="Image server port")
    parser.add_argument("--auto-connect", action="store_true", help="Connect to the image server on startup")
    parser.add_argument("--display-scale", type=float, default=DEFAULT_DISPLAY_SCALE, help="Initial display scale for rendering (0.1-4.0)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    state = AppState(host=args.host, port=args.port)
    state.set_zoom(args.display_scale)
    global _GLOBAL_STATE, _GLOBAL_CLIENT
    _GLOBAL_STATE = state

    client = ImageClient(state)
    _GLOBAL_CLIENT = client

    ids = create_ui(state, client)

    if args.auto_connect:
        try:
            client.connect(args.host, args.port)
        except Exception as exc:
            LOGGER.error("Auto-connect failed: %s", exc)
            state.set_status(f"Auto-connect failed: {exc}")

    try:
        run_gui(state, ids)
    except KeyboardInterrupt:
        LOGGER.info("GUI interrupted by user")
    finally:
        state.shutdown()
        client.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
