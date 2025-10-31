"""gRPC image server that stores TIFF images and runs trackpy-based detection."""

from __future__ import annotations

import argparse
import atexit
import io
import logging
import multiprocessing as mp
import os
import itertools
import math
from enum import Enum
import queue
import threading
import time
from concurrent import futures
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import grpc
import numpy as np
from google.protobuf import empty_pb2, json_format, struct_pb2

from image_proto import (  # type: ignore[import-not-found]
    FrameEnvelope,
    ImageChunk,
    LatestImageReply,
    LatestImageRequest,
    StorageConfig,
    TrackDetectionProto,
    UploadAck,
)

# Limit math libraries to a single thread inside each worker to avoid oversubscription.
_THREAD_ENV_KEYS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)
for _env_key in _THREAD_ENV_KEYS:
    os.environ[_env_key] = "1"
_THREAD_ENV_OVERRIDES = {key: os.environ[key] for key in _THREAD_ENV_KEYS}

ENABLE_HDF5_STORAGE_IMPROVEMENTS = True  # Toggle enhanced HDF5 compression and batching.


def _parse_cpu_list_arg(value: str) -> List[int]:
    cpus: List[int] = []
    seen: Set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            try:
                start = int(start_str)
                end = int(end_str)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"Invalid CPU range '{part}'") from exc
            if start < 0 or end < 0 or end < start:
                raise argparse.ArgumentTypeError(f"Invalid CPU range '{part}'")
            for cpu in range(start, end + 1):
                if cpu not in seen:
                    seen.add(cpu)
                    cpus.append(cpu)
            continue
        try:
            cpu_idx = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid CPU index '{part}'") from exc
        if cpu_idx < 0:
            raise argparse.ArgumentTypeError("CPU indices must be non-negative")
        if cpu_idx not in seen:
            seen.add(cpu_idx)
            cpus.append(cpu_idx)
    if not cpus:
        raise argparse.ArgumentTypeError("CPU list cannot be empty")
    return cpus


def _sanitize_cpu_ids(cpu_ids: Optional[Sequence[int]]) -> Optional[List[int]]:
    if cpu_ids is None:
        return None
    seen: Set[int] = set()
    unique: List[int] = []
    for cpu in cpu_ids:
        idx = int(cpu)
        if idx < 0:
            raise ValueError("CPU indices must be non-negative")
        if idx in seen:
            continue
        seen.add(idx)
        unique.append(idx)
    return unique or None


def _coerce_cpu_list(value: Any) -> Optional[List[int]]:
    if value is None:
        return None
    if isinstance(value, str):
        return _sanitize_cpu_ids(_parse_cpu_list_arg(value))
    if isinstance(value, Iterable):
        try:
            return _sanitize_cpu_ids([int(item) for item in value])
        except (TypeError, ValueError) as exc:
            raise ValueError("CPU list entries must be integers") from exc
    try:
        return _sanitize_cpu_ids([int(value)])
    except (TypeError, ValueError) as exc:
        raise ValueError("Unable to interpret CPU affinity value") from exc


try:
    import tifffile
except ImportError:  # pragma: no cover - tifffile is optional
    tifffile = None  # type: ignore[assignment]

try:
    import trackpy as tp
except ImportError as exc:  # pragma: no cover - tracking requires trackpy
    raise RuntimeError("trackpy is required to run ImageServer_with_track") from exc

try:
    import cv2
except ImportError:  # pragma: no cover - OpenCV optional
    cv2 = None  # type: ignore[assignment]

try:
    from turbojpeg import TurboJPEG, TJPF_GRAY, TJSAMP_GRAY  # type: ignore[import]
except ImportError:  # pragma: no cover - turbojpeg optional but preferred
    TurboJPEG = None  # type: ignore[assignment]
    TJPF_GRAY = None  # type: ignore[assignment]
    TJSAMP_GRAY = None  # type: ignore[assignment]


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

try:
    import numba as nb
except ImportError:  # pragma: no cover - numba optional
    nb = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover - psutil optional
    psutil = None  # type: ignore[assignment]

try:
    import h5py
except ImportError:  # pragma: no cover - h5py optional
    h5py = None  # type: ignore[assignment]

try:
    import hdf5plugin  # type: ignore[import]
except ImportError:  # pragma: no cover - hdf5plugin optional
    hdf5plugin = None  # type: ignore[assignment]


LOGGER = logging.getLogger("image_server")
TRACK_LOGGER = logging.getLogger("image_server.tracking")


_HDF5_ENHANCED_FLUSH_INTERVAL_DEFAULT = 8
_HDF5_LEGACY_FLUSH_INTERVAL_DEFAULT = 1
_HDF5_DEFAULT_FLUSH = (
    _HDF5_ENHANCED_FLUSH_INTERVAL_DEFAULT if ENABLE_HDF5_STORAGE_IMPROVEMENTS else _HDF5_LEGACY_FLUSH_INTERVAL_DEFAULT
)
try:
    _HDF5_FLUSH_INTERVAL = max(0, int(os.environ.get("TWEEZER_HDF5_FLUSH_INTERVAL", str(_HDF5_DEFAULT_FLUSH))))
except ValueError:
    _HDF5_FLUSH_INTERVAL = _HDF5_DEFAULT_FLUSH

try:
    _HDF5_BITSHUFFLE_CLEVEL = int(os.environ.get("TWEEZER_HDF5_CLEVEL", "7"))
except ValueError:
    _HDF5_BITSHUFFLE_CLEVEL = 7
_HDF5_BITSHUFFLE_CLEVEL = max(0, min(9, _HDF5_BITSHUFFLE_CLEVEL))

_BITSHUFFLE_CODECS: Tuple[str, ...] = ("lz4", "zstd")

_STORAGE_QUEUE_LIMIT = max(4, int(os.environ.get("TWEEZER_STORAGE_QUEUE", "4")))


_UNIQUE_PATH_COUNTERS: Dict[Tuple[Path, str], int] = {}

# print the configs
print(f"HDF5_FLUSH_INTERVAL: {_HDF5_FLUSH_INTERVAL}")
print(f"HDF5_BITSHUFFLE_CLEVEL: {_HDF5_BITSHUFFLE_CLEVEL}")
print(f"BITSHUFFLE_CODECS: {_BITSHUFFLE_CODECS}")
print(f"STORAGE_QUEUE_LIMIT: {_STORAGE_QUEUE_LIMIT}")
print(f"ENABLE_HDF5_STORAGE_IMPROVEMENTS: {ENABLE_HDF5_STORAGE_IMPROVEMENTS}")



def _build_hdf5_compression_settings(dtype: np.dtype) -> Tuple[Dict[str, Any], str]:
    np_dtype = np.dtype(dtype)
    if hdf5plugin is None:
        LOGGER.warning("hdf5plugin not available; storing HDF5 frames without bitshuffle compression")
        return {}, "uncompressed"

    attempts: List[Tuple[Dict[str, Any], str]] = []
    for codec in _BITSHUFFLE_CODECS:
        attempts.append(
            (
                {"dtype": np_dtype, "cname": codec, "clevel": _HDF5_BITSHUFFLE_CLEVEL},
                f"bitshuffle({codec}, clevel={_HDF5_BITSHUFFLE_CLEVEL})",
            )
        )
        attempts.append(
            (
                {"dtype": np_dtype, "cname": codec},
                f"bitshuffle({codec})",
            )
        )
        attempts.append(
            (
                {"cname": codec},
                f"bitshuffle({codec})",
            )
        )

    last_error: Optional[Exception] = None
    for kwargs, label in attempts:
        try:
            return hdf5plugin.Bitshuffle(**kwargs), label  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover - codec-specific failures
            last_error = exc

    if last_error is not None:
        LOGGER.warning("Failed to configure bitshuffle compression; falling back to uncompressed frames: %s", last_error)
    return {}, "uncompressed"


_STREAM_FORMAT_DEFAULT = os.environ.get("TWEEZER_STREAM_FORMAT", "raw8").strip().lower() or "raw8"
_JPEG_QUALITY_DEFAULT = min(95, max(40, int(os.environ.get("TWEEZER_JPEG_QUALITY", "85"))))
_ENCODER_THREADS_DEFAULT = max(1, int(os.environ.get("TWEEZER_JPEG_THREADS", "2")))


class _TurboJpegEncoder:
    """Wrapper for TurboJPEG that bounds parallel encodes."""

    def __init__(self, threads: int, quality: int) -> None:
        self._quality = quality
        self._threads = max(1, threads)
        self._pool = None  # type: Optional[futures.ThreadPoolExecutor]
        self._turbo = None  # type: Optional[Any]
        if TurboJPEG is None:
            return
        try:
            self._turbo = TurboJPEG()
        except Exception:  # pragma: no cover - defensive
            TRACK_LOGGER.exception("Failed to initialise TurboJPEG; falling back to OpenCV")
            self._turbo = None
            return
        self._pool = futures.ThreadPoolExecutor(max_workers=self._threads)
        atexit.register(self.shutdown)

    def available(self) -> bool:
        return self._turbo is not None

    def encode(self, image: np.ndarray) -> bytes:
        if self._turbo is None:
            raise RuntimeError("TurboJPEG not available")

        def _job() -> bytes:
            return self._turbo.encode(  # type: ignore[operator]
                image,
                quality=self._quality,
                pixel_format=TJPF_GRAY,
                subsampling=TJSAMP_GRAY,
            )

        if self._pool is None:
            return _job()
        future = self._pool.submit(_job)
        return future.result()

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


_TURBO_ENCODER = _TurboJpegEncoder(_ENCODER_THREADS_DEFAULT, _JPEG_QUALITY_DEFAULT)


class ImageExchangeStub:
    def __init__(self, channel: grpc.Channel) -> None:
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
        self._get_storage = channel.unary_unary(
            "/images.ImageExchange/GetStorageConfig",
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=StorageConfig.FromString,
        )
        self._update_storage = channel.unary_unary(
            "/images.ImageExchange/UpdateStorageConfig",
            request_serializer=StorageConfig.SerializeToString,
            response_deserializer=StorageConfig.FromString,
        )

    def UploadImage(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._upload(request, timeout=timeout)

    def UploadImageFuture(self, request: Any, timeout: Optional[float] = None) -> grpc.Future:  # type: ignore[type-arg]
        return self._upload.future(request, timeout=timeout)

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
        return self._get_storage(request, timeout=timeout)

    def UpdateStorageConfig(self, request: Any, timeout: Optional[float] = None) -> Any:  # noqa: N802
        return self._update_storage(request, timeout=timeout)


def add_ImageExchangeServicer_to_server(servicer: Any, server: grpc.Server) -> None:  # noqa: N802
    rpc_method_handlers = {
        "UploadImage": grpc.unary_unary_rpc_method_handler(
            servicer.UploadImage,
            request_deserializer=ImageChunk.FromString,
            response_serializer=UploadAck.SerializeToString,
        ),
        "GetLatestImage": grpc.unary_unary_rpc_method_handler(
            servicer.GetLatestImage,
            request_deserializer=LatestImageRequest.FromString,
            response_serializer=LatestImageReply.SerializeToString,
        ),
        "GetLatestTracks": grpc.unary_unary_rpc_method_handler(
            servicer.GetLatestTracks,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        "GetTrackingConfig": grpc.unary_unary_rpc_method_handler(
            servicer.GetTrackingConfig,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        "UpdateTrackingConfig": grpc.unary_unary_rpc_method_handler(
            servicer.UpdateTrackingConfig,
            request_deserializer=struct_pb2.Struct.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        "StreamFrames": grpc.unary_stream_rpc_method_handler(
            servicer.StreamFrames,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=FrameEnvelope.SerializeToString,
        ),
        "GetStorageConfig": grpc.unary_unary_rpc_method_handler(
            servicer.GetStorageConfig,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=StorageConfig.SerializeToString,
        ),
        "UpdateStorageConfig": grpc.unary_unary_rpc_method_handler(
            servicer.UpdateStorageConfig,
            request_deserializer=StorageConfig.FromString,
            response_serializer=StorageConfig.SerializeToString,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler("images.ImageExchange", rpc_method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


@dataclass
class CachedImage:
    filename: str
    timestamp_ms: int
    source: str
    data: bytes
    received_at_ms: int
    sequence: int


@dataclass
class DetectionConfig:
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
    tile_width: int = 512
    tile_height: int = 512
    tile_overlap: int = 48
    max_workers: int = 8
    worker_backend: str = "auto"

    def to_dict(self) -> Dict[str, Any]:
        return dict(asdict(self))

    def with_updates(self, updates: Dict[str, Any]) -> DetectionConfig:
        values = self.to_dict()
        for key, value in updates.items():
            if key not in values:
                continue
            current = values[key]
            try:
                if isinstance(current, bool):
                    values[key] = bool(value)
                elif isinstance(current, int):
                    values[key] = int(float(value))
                elif isinstance(current, float):
                    values[key] = float(value)
                else:
                    values[key] = value
            except (TypeError, ValueError):
                TRACK_LOGGER.warning("Unable to coerce %s to %s for %s", value, type(current).__name__, key)
        return DetectionConfig(**values)


@dataclass
class TrackDetection:
    x: float
    y: float
    mass: float
    ecc: float
    size: float
    signal: float


@dataclass
class TrackResult:
    sequence: int
    filename: str
    timestamp_ms: int
    source: str
    detections: List[TrackDetection] = field(default_factory=list)
    image_shape: Tuple[int, int] = (0, 0)
    received_at_ms: int = 0
    processed_at_ms: int = 0
    processing_ms: int = 0
    image: Optional[np.ndarray] = None


@dataclass
class StorageMetrics:
    sequence: int
    duration_ms: float
    kind: str
    path: Optional[Path]
    success: bool
    timestamp_ms: int
    message: str = ""
    bytes_in: int = 0
    bytes_out: int = 0
    compression_ratio: float = 0.0
    codec: str = ""
    throttle_ms: float = 0.0


class _SubscriptionMode(str, Enum):
    FULL = "full"
    IMAGE_ONLY = "image"
    TRACKS_ONLY = "tracks"


@dataclass
class _Subscriber:
    queue: "queue.Queue[bytes]"
    mode: _SubscriptionMode


@dataclass
class StorageState:
    enabled: bool = False
    target_fps: float = 0.0
    image_format: str = "native"
    output_dir: Path = field(default_factory=lambda: Path.cwd() / "recorded_frames")
    hdf5_enabled: bool = False
    hdf5_path: Optional[Path] = None
    last_save_monotonic: float = field(default=0.0, init=False)
    _hdf5_writer: Optional["_Hdf5Writer"] = field(default=None, init=False, repr=False)

    def sanitized_format(self) -> str:
        fmt = (self.image_format or "native").lower()
        if fmt in {"native", "copy", "original", "tif", "tiff"}:
            return "native"
        return "native"

    def to_proto(self):
        cfg = StorageConfig()
        cfg.enabled = bool(self.enabled)
        cfg.target_fps = float(self.target_fps)
        cfg.image_format = self.sanitized_format()
        cfg.output_dir = str(self.output_dir)
        cfg.hdf5_enabled = bool(self.hdf5_enabled)
        cfg.hdf5_path = str(self.hdf5_path) if self.hdf5_path else ""
        # Legacy fields retained for backwards compatibility but unused by the server.
        setattr(cfg, "tiff_compression", "none")
        setattr(cfg, "tiff_compression_level", 0)
        setattr(cfg, "png_compression_level", 0)
        setattr(cfg, "bit_depth", "auto")
        return cfg

    def update_from_proto(self, proto) -> None:
        prev_hdf5_enabled = self.hdf5_enabled
        prev_hdf5_path = self.hdf5_path
        self.enabled = bool(getattr(proto, "enabled", self.enabled))
        try:
            self.target_fps = max(0.0, float(getattr(proto, "target_fps", self.target_fps)))
        except (TypeError, ValueError):
            self.target_fps = 0.0
        fmt = (getattr(proto, "image_format", None) or self.image_format or "native").lower()
        if fmt not in {"native", "copy", "original", "jpeg", "jpg", "png", "tiff", "tif", "raw", "raw8"}:
            fmt = self.image_format
        self.image_format = fmt
        output_dir_raw = getattr(proto, "output_dir", None) or str(self.output_dir)
        output_path = Path(output_dir_raw).expanduser()
        try:
            self.output_dir = output_path.resolve()
        except FileNotFoundError:
            self.output_dir = output_path
        self.hdf5_enabled = bool(getattr(proto, "hdf5_enabled", self.hdf5_enabled))
        raw_path = getattr(proto, "hdf5_path", "") or ""
        if raw_path:
            path_obj = Path(raw_path).expanduser()
            try:
                path_obj = path_obj.resolve()
            except FileNotFoundError:
                pass
            self.hdf5_path = path_obj
        else:
            self.hdf5_path = None
        if (prev_hdf5_enabled and not self.hdf5_enabled) or (prev_hdf5_path != self.hdf5_path):
            self.close_hdf5()

    def close_hdf5(self) -> None:
        if self._hdf5_writer is not None:
            try:
                self._hdf5_writer.close()
            finally:
                self._hdf5_writer = None

    def shutdown(self) -> None:
        self.close_hdf5()

    def get_hdf5_writer(self) -> "_Hdf5Writer":
        if self._hdf5_writer is None:
            self._hdf5_writer = _Hdf5Writer()
        return self._hdf5_writer

    def ensure_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def effective_hdf5_path(self) -> Path:
        if self.hdf5_path is not None:
            return self.hdf5_path
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        candidate = self.output_dir / f"raw_frames_{timestamp}.h5"
        candidate = _ensure_unique_path(candidate)
        self.hdf5_path = candidate
        return candidate

    def append_hdf5(self, image: CachedImage, array: np.ndarray, image_format: str) -> Tuple[Path, int, int]:
        if h5py is None:
            raise RuntimeError("h5py is required for HDF5 saving")
        if array.ndim < 2:
            raise RuntimeError("HDF5 saving requires at least a 2D array")
        prepared = np.ascontiguousarray(array)
        shape = (int(prepared.shape[0]), int(prepared.shape[1]))
        writer = self.get_hdf5_writer()
        path = self.effective_hdf5_path()
        writer.ensure(path, shape, prepared.dtype)
        index = writer.append(
            {
                "sequence": image.sequence,
                "timestamp_ms": image.timestamp_ms,
                "filename": image.filename,
                "source": image.source,
                "image_format": image_format,
            },
            prepared,
            path=path,
            shape=shape,
            dtype=prepared.dtype,
        )
        return path, index, int(prepared.nbytes)


class _Hdf5Writer:
    def __init__(self) -> None:
        self._file: Optional[Any] = None
        self._frames: Optional[Any] = None
        self._metadata: Optional[Any] = None
        self._metadata_dtype: Optional[np.dtype] = None
        self._shape: Optional[Tuple[int, int]] = None
        self._dtype: Optional[np.dtype] = None
        self._path: Optional[Path] = None
        self._count: int = 0
        self._compression_label: Optional[str] = None
        self._flush_interval = max(0, int(_HDF5_FLUSH_INTERVAL))
        self._writes_since_flush = 0

    def ensure(self, path: Path, shape: Tuple[int, int], dtype: np.dtype) -> None:
        if h5py is None:
            raise RuntimeError("h5py is required for HDF5 saving")
        if (
            self._file is not None
            and self._path == path
            and self._shape == shape
            and self._dtype == dtype
            and self._frames is not None
            and self._metadata is not None
            and self._metadata_dtype is not None
        ):
            return
        self.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = h5py.File(str(path), "a")  # type: ignore[arg-type]
        self._path = path
        self._shape = shape
        self._dtype = dtype
        if "frames" in self._file:
            frames_ds = self._file["frames"]
            if frames_ds.shape[1:] != shape:
                raise RuntimeError(
                    f"Existing HDF5 dataset shape {frames_ds.shape[1:]} does not match current frame {shape}"
                )
            if frames_ds.dtype != dtype:
                raise RuntimeError(
                    f"Existing HDF5 dataset dtype {frames_ds.dtype} does not match current frame {dtype}"
                )
            self._compression_label = getattr(frames_ds, "compression", self._compression_label)
        else:
            compression_kwargs, compression_label = _build_hdf5_compression_settings(dtype)
            create_kwargs = dict(compression_kwargs)
            create_kwargs.pop("dtype", None)
            frames_ds = self._file.create_dataset(
                "frames",
                shape=(0, shape[0], shape[1]),
                maxshape=(None, shape[0], shape[1]),
                dtype=dtype,
                chunks=(1, shape[0], shape[1]),
                **create_kwargs,
            )
            self._compression_label = compression_label
            if ENABLE_HDF5_STORAGE_IMPROVEMENTS:
                LOGGER.info(
                    "HDF5 frames dataset ready with %s compression (flush_interval=%d)",
                    compression_label,
                    self._flush_interval,
                )
        self._frames = frames_ds
        if "metadata" in self._file:
            metadata_ds = self._file["metadata"]
            metadata_dtype = metadata_ds.dtype
        else:
            filename_dtype = h5py.string_dtype(encoding="utf-8", length=256)
            source_dtype = h5py.string_dtype(encoding="utf-8", length=128)
            fmt_dtype = h5py.string_dtype(encoding="utf-8", length=32)
            metadata_dtype = np.dtype(
                [
                    ("sequence", np.int64),
                    ("timestamp_ms", np.int64),
                    ("filename", filename_dtype),
                    ("source", source_dtype),
                    ("format", fmt_dtype),
                ]
            )
            metadata_ds = self._file.create_dataset(
                "metadata",
                shape=(0,),
                maxshape=(None,),
                dtype=metadata_dtype,
                chunks=True,
            )
        self._metadata = metadata_ds
        self._metadata_dtype = metadata_dtype
        try:
            self._count = int(metadata_ds.shape[0])
        except Exception:
            self._count = 0
        self._writes_since_flush = 0

    def append(
        self,
        metadata: Dict[str, Any],
        array: np.ndarray,
        *,
        path: Optional[Path] = None,
        shape: Optional[Tuple[int, int]] = None,
        dtype: Optional[np.dtype] = None,
    ) -> int:
        if array.ndim < 2:
            raise RuntimeError("HDF5 writer requires at least 2D input")
        if (
            self._file is None
            or self._frames is None
            or self._metadata is None
            or self._metadata_dtype is None
        ):
            if path is not None and shape is not None and dtype is not None:
                self.ensure(path, shape, dtype)
            elif self._path is not None and self._shape is not None and self._dtype is not None:
                # Attempt to recover lazily by re-opening the dataset with cached parameters.
                self.ensure(self._path, self._shape, self._dtype)
            if (
                self._file is None
                or self._frames is None
                or self._metadata is None
                or self._metadata_dtype is None
            ):
                LOGGER.error(
                    "HDF5 writer recovery failed: passed_path=%s cached_path=%s file=%s frames=%s metadata=%s metadata_dtype=%s",
                    path,
                    self._path,
                    "set" if self._file is not None else "None",
                    "set" if self._frames is not None else "None",
                    "set" if self._metadata is not None else "None",
                    self._metadata_dtype,
                )
                raise RuntimeError("HDF5 writer not initialised")
        frames_ds = self._frames
        metadata_ds = self._metadata
        index = int(self._count)
        frames_ds.resize((index + 1, frames_ds.shape[1], frames_ds.shape[2]))
        frames_ds[index, :, :] = np.ascontiguousarray(array)
        metadata_ds.resize((index + 1,))
        entry = np.zeros((), dtype=self._metadata_dtype)
        entry["sequence"] = int(metadata.get("sequence", index))
        entry["timestamp_ms"] = int(metadata.get("timestamp_ms", 0))
        entry["filename"] = str(metadata.get("filename", ""))
        entry["source"] = str(metadata.get("source", ""))
        entry["format"] = str(metadata.get("image_format", ""))
        metadata_ds[index] = entry
        self._writes_since_flush += 1
        if self._flush_interval and self._writes_since_flush >= self._flush_interval:
            try:
                self._file.flush()
            except Exception:
                pass
            self._writes_since_flush = 0
        self._count = index + 1
        return index

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
        self._file = None
        self._frames = None
        self._metadata = None
        self._metadata_dtype = None
        self._shape = None
        self._dtype = None
        self._path = None
        self._count = 0
        self._compression_label = None
        self._writes_since_flush = 0



@dataclass
class TileTask:
    buffer: Optional[bytes]
    array: Optional[np.ndarray]
    shape: Tuple[int, int]
    dtype: str
    offset_x: int
    offset_y: int
    config: DetectionConfig


def _detect_available_cpu_ids() -> List[int]:
    if psutil is not None:
        try:
            affinity = psutil.Process().cpu_affinity()
            if affinity:
                return sorted(int(cpu) for cpu in affinity)
        except (AttributeError, NotImplementedError):  # pragma: no cover - platform specific
            pass
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if callable(sched_getaffinity):
        try:
            return sorted(int(cpu) for cpu in sched_getaffinity(0))  # type: ignore[misc]
        except (OSError, TypeError):  # pragma: no cover - platform specific
            pass
    count = os.cpu_count() or 1
    return list(range(count))


def _pin_current_process_to_cpu(cpu_id: int) -> None:
    try:
        if psutil is not None:
            psutil.Process().cpu_affinity([int(cpu_id)])
            return
    except Exception:  # pragma: no cover - fallback paths
        TRACK_LOGGER.exception("Failed to set cpu_affinity via psutil for cpu=%s", cpu_id)
    sched_setaffinity = getattr(os, "sched_setaffinity", None)
    if callable(sched_setaffinity):
        try:
            sched_setaffinity(0, {int(cpu_id)})
            return
        except Exception:  # pragma: no cover - sched_setaffinity unavailable
            TRACK_LOGGER.debug("Unable to pin worker to cpu=%s", cpu_id)


def _tracking_worker_initializer(cpu_queue: Optional[Any], env_overrides: Dict[str, str]) -> None:
    for key, value in env_overrides.items():
        os.environ[key] = value
    cpu_id: Optional[int] = None
    if cpu_queue is not None:
        try:
            cpu_id = cpu_queue.get()
        except Exception:  # pragma: no cover - queue retrieval failures
            cpu_id = None
    if cpu_id is not None:
        _pin_current_process_to_cpu(cpu_id)


def _ensure_float32(array: np.ndarray) -> np.ndarray:
    if array.dtype == np.float32:
        return array
    if array.dtype == np.float64:
        return array.astype(np.float32)
    return array.astype(np.float32)


def _decode_image_bytes(data: bytes) -> np.ndarray:
    if tifffile is not None:
        with tifffile.TiffFile(io.BytesIO(data)) as tif:
            array = tif.asarray()
    else:  # pragma: no cover - pillow fallback
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - pillow required when tifffile missing
            raise RuntimeError("Pillow or tifffile must be installed to decode TIFF images") from exc
        with Image.open(io.BytesIO(data)) as img:
            array = np.array(img)
    if array.ndim > 2:
        array = np.mean(array, axis=-1)
    return _ensure_float32(np.ascontiguousarray(array))


def _decode_image_bytes_preserve(data: bytes) -> np.ndarray:
    if tifffile is None:
        raise RuntimeError("tifffile is required for archival decoding")
    with tifffile.TiffFile(io.BytesIO(data)) as tif:
        array = tif.asarray()
    if array.ndim > 2 and array.shape[-1] == 1:
        array = np.squeeze(array, axis=-1)
    return np.ascontiguousarray(array)


def _iter_tiles(image: np.ndarray, config: DetectionConfig, *, for_process_pool: bool) -> Iterable[TileTask]:
    height, width = image.shape[:2]
    step_y = max(1, config.tile_height)
    step_x = max(1, config.tile_width)
    overlap = max(0, config.tile_overlap)
    for y0 in range(0, height, step_y):
        y1 = min(height, y0 + step_y + overlap)
        for x0 in range(0, width, step_x):
            x1 = min(width, x0 + step_x + overlap)
            tile_view = image[y0:y1, x0:x1]
            if for_process_pool:
                tile = np.ascontiguousarray(tile_view)
                tile_buffer: Optional[bytes] = tile.tobytes()
                tile_array: Optional[np.ndarray] = None
            else:
                tile = np.require(tile_view, requirements=("C_CONTIGUOUS",))
                tile_buffer = None
                tile_array = tile
            tile_shape: Tuple[int, int] = (int(tile.shape[0]), int(tile.shape[1]))
            yield TileTask(
                buffer=tile_buffer,
                array=tile_array,
                shape=tile_shape,
                dtype=str(tile.dtype),
                offset_x=x0,
                offset_y=y0,
                config=config,
            )


def _apply_filters(df: Any, tile_image: np.ndarray, config: DetectionConfig) -> Any:
    if df.empty:
        return df
    if config.maxmass > 0:
        df = df[df["mass"] <= config.maxmass]
    if config.min_ecc >= 0:
        df = df[df["ecc"] >= config.min_ecc]
    if config.max_ecc >= 0:
        df = df[df["ecc"] <= config.max_ecc]
    if config.pixel_threshold > 0:
        y_coords = np.clip(np.rint(df["y"].to_numpy()), 0, tile_image.shape[0] - 1).astype(int)
        x_coords = np.clip(np.rint(df["x"].to_numpy()), 0, tile_image.shape[1] - 1).astype(int)
        intensities = tile_image[y_coords, x_coords]
        mask = intensities >= config.pixel_threshold
        if not mask.all():
            df = df.loc[mask]
    return df


def _process_tile(task: TileTask) -> List[TrackDetection]:
    if task.array is not None:
        array = task.array
    else:
        if task.buffer is None:
            raise ValueError("TileTask missing image data")
        array = np.frombuffer(task.buffer, dtype=np.dtype(task.dtype)).reshape(task.shape)
    config = task.config
    work_image = array
    if config.preprocess:
        # Bandpass filters away background noise before locating features.
        work_image = tp.bandpass(work_image, config.lshort, config.llong)
    df = tp.locate(
        work_image,
        diameter=config.diameter,
        separation=config.separation,
        percentile=int(round(config.percentile)),
        minmass=config.minmass,
        max_iterations=config.refine,
        engine="numba",
    )
    df = _apply_filters(df, work_image, config)
    if df.empty:
        return []
    x_vals = df["x"].to_numpy(dtype=float) + task.offset_x
    y_vals = df["y"].to_numpy(dtype=float) + task.offset_y
    mass = df.get("mass", df["x"] * 0.0).to_numpy(dtype=float)
    ecc = df.get("ecc", df["x"] * 0.0).to_numpy(dtype=float)
    size = df.get("size", df["x"] * 0.0).to_numpy(dtype=float)
    signal = df.get("signal", df["x"] * 0.0).to_numpy(dtype=float)
    return [
        TrackDetection(
            x=float(x_vals[idx]),
            y=float(y_vals[idx]),
            mass=float(mass[idx]),
            ecc=float(ecc[idx]),
            size=float(size[idx]),
            signal=float(signal[idx]),
        )
        for idx in range(len(df))
    ]


def _normalize_to_uint8(image: np.ndarray) -> np.ndarray:
    if image.ndim != 2:
        raise ValueError("Only 2D grayscale images supported")
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


def _encode_image_bytes(
    image_uint8: np.ndarray,
    fmt: str,
    *,
    png_level: Optional[int] = None,
    tiff_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str]:
    fmt_normalized = (fmt or "jpeg").lower()
    contiguous = np.ascontiguousarray(image_uint8)

    if fmt_normalized in {"raw", "raw8"}:
        return contiguous.tobytes(), "raw8"

    if fmt_normalized in {"jpeg", "jpg"}:
        if _TURBO_ENCODER.available():
            try:
                payload = _TURBO_ENCODER.encode(contiguous)
                return payload, "jpeg"
            except Exception:  # pragma: no cover - defensive
                TRACK_LOGGER.exception("TurboJPEG encode failed; falling back to OpenCV")
        if cv2 is None:
            raise RuntimeError("OpenCV or TurboJPEG required for JPEG encoding")
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(_JPEG_QUALITY_DEFAULT)]  # type: ignore[attr-defined]
        success, buffer = cv2.imencode(".jpg", contiguous, params)  # type: ignore[attr-defined]
        if not success:
            raise RuntimeError("OpenCV failed to encode JPEG")
        return buffer.tobytes(), "jpeg"

    if fmt_normalized == "png":
        if cv2 is None:
            raise RuntimeError("OpenCV is required for PNG encoding")
        params: List[int] = []
        if png_level is not None:
            level = int(max(0, min(9, png_level)))
            params = [int(cv2.IMWRITE_PNG_COMPRESSION), level]  # type: ignore[attr-defined]
        success, buffer = cv2.imencode(".png", contiguous, params)  # type: ignore[attr-defined]
        if not success:
            raise RuntimeError("OpenCV failed to encode PNG")
        return buffer.tobytes(), "png"

    if fmt_normalized in {"tif", "tiff"}:
        if tifffile is None:
            raise RuntimeError("tifffile is required for TIFF encoding")
        with io.BytesIO() as bio:
            kwargs = dict(tiff_kwargs or {})
            tifffile.imwrite(bio, contiguous, **kwargs)
            return bio.getvalue(), "tiff"

    raise ValueError(f"Unsupported image format '{fmt_normalized}'")


def _prepare_stream_payload(image_uint8: np.ndarray, fmt: str) -> Tuple[bytes, str]:
    fmt_normalized = (fmt or _STREAM_FORMAT_DEFAULT).lower()
    if fmt_normalized not in {"raw", "raw8", "jpeg", "jpg", "png", "tif", "tiff"}:
        TRACK_LOGGER.warning("Unknown stream format '%s'; defaulting to raw8", fmt_normalized)
        fmt_normalized = "raw8"
    return _encode_image_bytes(image_uint8, fmt_normalized)


def _ensure_unique_path(path: Path, *, sequence_hint: Optional[int] = None) -> Path:
    """Return a unique path by appending an incrementing suffix when needed."""

    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    key_parent: Path
    try:
        key_parent = parent.resolve()
    except FileNotFoundError:
        key_parent = parent
    key = (key_parent, stem)

    try:
        base_exists = path.exists()
    except OSError:
        base_exists = False

    next_counter = _UNIQUE_PATH_COUNTERS.get(key, 2)
    if sequence_hint is not None and sequence_hint > 0:
        next_counter = max(next_counter, sequence_hint + 1)

    if not base_exists:
        _UNIQUE_PATH_COUNTERS[key] = next_counter
        return path

    counter = max(2, next_counter)
    while True:
        candidate = parent / f"{stem}_{counter - 1}{suffix}"
        try:
            exists = candidate.exists()
        except OSError:
            exists = False
        if not exists:
            _UNIQUE_PATH_COUNTERS[key] = counter + 1
            return candidate
        counter += 1


class TrackingManager:
    def __init__(
        self,
        config: DetectionConfig,
        on_result: Optional[Callable[[CachedImage, TrackResult], None]] = None,
        allowed_cpu_ids: Optional[Sequence[int]] = None,
    ) -> None:
        self._config = replace(config)
        auto_available = _detect_available_cpu_ids()
        configured = _sanitize_cpu_ids(allowed_cpu_ids)
        if configured is not None:
            missing = sorted(set(configured) - set(auto_available))
            if missing:
                raise ValueError(f"Requested tracker CPU indices {missing} are not available to this process")
            TRACK_LOGGER.info(
                "Restricting tracking workers to CPUs: %s",
                ",".join(str(cpu) for cpu in configured),
            )
        self._configured_cpu_ids = configured
        self._available_cpu_ids = list(configured) if configured is not None else auto_available
        self._env_overrides = dict(_THREAD_ENV_OVERRIDES)
        self._restart_count = 0
        self._last_restart_ms = 0
        self._worker_count = self._bounded_worker_count(config.max_workers)
        self._config.max_workers = self._worker_count
        self._backend = self._select_backend(self._config.worker_backend)
        self._use_process_pool = False
        self._pool = self._create_pool()
        self._lock = threading.Condition()
        self._pending: Optional[CachedImage] = None
        self._latest: Optional[TrackResult] = None
        self._stopped = False
        self._on_result: Optional[Callable[[CachedImage, TrackResult], None]] = on_result
        self._thread = threading.Thread(target=self._loop, name="tracking-loop", daemon=True)
        self._thread.start()

    def enqueue(self, image: CachedImage) -> None:
        with self._lock:
            replaced = self._pending is not None
            self._pending = image
            self._lock.notify()
        if replaced:
            TRACK_LOGGER.debug("Replacing pending tracking job with seq=%d", image.sequence)

    def get_latest(self) -> Optional[TrackResult]:
        with self._lock:
            return self._latest

    def shutdown(self) -> None:
        with self._lock:
            self._stopped = True
            self._lock.notify()
        self._thread.join(timeout=5)
        self._pool.shutdown(cancel_futures=True)

    def get_config_dict(self) -> Dict[str, Any]:
        with self._lock:
            config = self._config.to_dict()
            config["allowed_cpus"] = (
                list(self._configured_cpu_ids) if self._configured_cpu_ids is not None else None
            )
            config["active_cpus"] = list(self._available_cpu_ids)
            return config

    def update_config(self, updates: Dict[str, Any]) -> DetectionConfig:
        updates = dict(updates)
        cpu_override_raw = updates.pop("allowed_cpus", None)
        cpu_override: Optional[List[int]] = None
        if cpu_override_raw is not None:
            cpu_override = _coerce_cpu_list(cpu_override_raw)

        restart_pool = False
        with self._lock:
            detected = _detect_available_cpu_ids()
            if cpu_override is not None:
                missing = sorted(set(cpu_override) - set(detected))
                if missing:
                    raise ValueError(
                        f"Requested tracker CPU indices {missing} are not available to this process"
                    )
                if self._configured_cpu_ids != cpu_override:
                    TRACK_LOGGER.info(
                        "Restricting tracking workers to CPUs: %s",
                        ",".join(str(cpu) for cpu in cpu_override),
                    )
                    restart_pool = True
                self._configured_cpu_ids = cpu_override
            if self._configured_cpu_ids is not None:
                effective = [cpu for cpu in self._configured_cpu_ids if cpu in detected]
                if not effective:
                    TRACK_LOGGER.warning(
                        "Configured tracker CPUs %s are unavailable; falling back to detected set %s",
                        self._configured_cpu_ids,
                        detected,
                    )
                    self._available_cpu_ids = detected
                else:
                    if len(effective) < len(self._configured_cpu_ids):
                        TRACK_LOGGER.warning(
                            "Subset of configured tracker CPUs %s will be used: %s",
                            self._configured_cpu_ids,
                            effective,
                        )
                    self._available_cpu_ids = effective
            else:
                self._available_cpu_ids = detected
            new_config = self._config.with_updates(updates)
            new_worker_count = self._bounded_worker_count(new_config.max_workers)
            if new_worker_count != self._worker_count:
                self._worker_count = new_worker_count
                new_config.max_workers = self._worker_count
                restart_pool = True
            self._config = new_config
            new_backend = self._select_backend(new_config.worker_backend)
            if new_backend != self._backend:
                self._backend = new_backend
                restart_pool = True
        if restart_pool:
            self._restart_pool()
        TRACK_LOGGER.info("Tracking configuration updated: %s", new_config)
        return new_config

    def set_result_callback(self, callback: Optional[Callable[[CachedImage, TrackResult], None]]) -> None:
        with self._lock:
            self._on_result = callback

    def get_restart_stats(self) -> Tuple[int, int]:
        with self._lock:
            return self._restart_count, self._last_restart_ms

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self._stopped and self._pending is None:
                    self._lock.wait()
                if self._stopped:
                    return
                image = self._pending
                self._pending = None
            if image is None:
                continue
            start = time.perf_counter()
            try:
                result = self._process_image(image)
            except Exception:  # pragma: no cover - defensive logging around tracking errors
                TRACK_LOGGER.exception("Tracking failed for %s (seq=%d)", image.filename, image.sequence)
                continue
            end = time.perf_counter()
            result.processed_at_ms = int(time.time() * 1000)
            result.processing_ms = int((end - start) * 1000)
            callback: Optional[Callable[[CachedImage, TrackResult], None]]
            with self._lock:
                self._latest = result
                callback = self._on_result
            TRACK_LOGGER.info(
                "Tracked %d points in %s (seq=%d) in %d ms",
                len(result.detections),
                image.filename,
                image.sequence,
                result.processing_ms,
            )
            if callback is not None:
                try:
                    callback(image, result)
                except Exception:  # pragma: no cover - defensive callback guard
                    TRACK_LOGGER.exception("Result callback failed for seq=%d", image.sequence)

    def _bounded_worker_count(self, requested: int) -> int:
        desired = max(1, int(requested or 1))
        cpu_capacity = max(1, len(self._available_cpu_ids) or (os.cpu_count() or 1))
        if desired > cpu_capacity:
            TRACK_LOGGER.info("Clamping tracking workers from %d to cpu capacity %d", desired, cpu_capacity)
        return max(1, min(desired, cpu_capacity))

    def _select_backend(self, requested: str) -> str:
        kind = (requested or "auto").lower()
        if kind == "auto":
            # Default to process workers for consistency unless the user explicitly opts into threads.
            return "process"
        if kind in {"thread", "process"}:
            return kind
        TRACK_LOGGER.warning("Unknown worker backend '%s'; defaulting to process pool", requested)
        return "process"

    def _create_pool(self) -> futures.Executor:
        backend = self._backend
        self._use_process_pool = backend == "process"
        if self._use_process_pool:
            context = mp.get_context("spawn")
            affinity_queue = self._build_affinity_queue(context)
            pool = futures.ProcessPoolExecutor(
                max_workers=self._worker_count,
                mp_context=context,
                initializer=_tracking_worker_initializer,
                initargs=(affinity_queue, self._env_overrides),
            )
            TRACK_LOGGER.info("Tracking worker pool started (process, workers=%d)", self._worker_count)
            return pool
        pool = futures.ThreadPoolExecutor(max_workers=self._worker_count)
        TRACK_LOGGER.info("Tracking worker pool started (thread, workers=%d)", self._worker_count)
        return pool

    def _build_affinity_queue(self, context: Any) -> Optional[Any]:
        pinning_supported = psutil is not None or callable(getattr(os, "sched_setaffinity", None))
        if not pinning_supported:
            return None
        if not self._available_cpu_ids:
            return None
        if self._worker_count <= 0:
            return None
        if len(self._available_cpu_ids) >= self._worker_count:
            assignments = self._available_cpu_ids[: self._worker_count]
        else:
            assignments = list(itertools.islice(itertools.cycle(self._available_cpu_ids), self._worker_count))
        queue_obj = context.SimpleQueue()
        for cpu_id in assignments:
            queue_obj.put(int(cpu_id))
        return queue_obj

    def _restart_pool(self) -> None:
        TRACK_LOGGER.warning("Restarting tracking worker pool using backend=%s", self._backend)
        self._restart_count += 1
        self._last_restart_ms = int(time.time() * 1000)
        try:
            self._pool.shutdown(cancel_futures=True)
        except Exception:  # pragma: no cover - defensive cleanup
            TRACK_LOGGER.exception("Error while shutting down tracking pool")
        self._pool = self._create_pool()

    def _run_tiles(self, tasks: List[TileTask]) -> List[TrackDetection]:
        if not tasks:
            return []
        if len(tasks) == 1:
            return _process_tile(tasks[0])
        detections: List[TrackDetection] = []
        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                chunk = 1 if not self._use_process_pool else max(1, len(tasks) // max(1, self._worker_count * 4))
                for tile_detections in self._pool.map(_process_tile, tasks, chunksize=chunk):
                    if tile_detections:
                        detections.extend(tile_detections)
                return detections
            except BrokenProcessPool:
                if not self._use_process_pool:
                    raise
                TRACK_LOGGER.error("Tracking worker crashed during map; attempt %d", attempts)
                self._restart_pool()
            except Exception:
                TRACK_LOGGER.exception("Unexpected error during tile processing; attempt %d", attempts)
                if self._use_process_pool:
                    self._restart_pool()
                else:
                    break
        TRACK_LOGGER.error("Falling back to inline tile processing after repeated failures")
        for task in tasks:
            detections.extend(_process_tile(task))
        return detections

    def _process_image(self, image: CachedImage) -> TrackResult:
        # Decode once per committed frame; downstream workers receive tiles.
        decoded = _decode_image_bytes(image.data)
        with self._lock:
            config = replace(self._config)
        tasks = list(_iter_tiles(decoded, config, for_process_pool=self._use_process_pool))
        detections = self._run_tiles(tasks)
        image_shape: Tuple[int, int] = (int(decoded.shape[0]), int(decoded.shape[1]))
        result = TrackResult(
            sequence=image.sequence,
            filename=image.filename,
            timestamp_ms=image.timestamp_ms,
            source=image.source,
            detections=detections,
            image_shape=image_shape,
            received_at_ms=image.received_at_ms,
        )
        result.image = decoded
        return result


class ImageExchangeServicer:
    def __init__(self, tracker: TrackingManager) -> None:
        self._lock = threading.RLock()
        self._latest: Optional[CachedImage] = None
        self._next_sequence: int = 1
        self._pending: Dict[int, CachedImage] = {}
        self._tracker = tracker
        self._storage = StorageState()
        self._storage_lock = threading.RLock()
        self._storage_metrics: Dict[int, StorageMetrics] = {}
        self._subscribers: Dict[int, _Subscriber] = {}
        self._subscriber_counter = 0
        self._latest_envelopes_serialized: Dict[_SubscriptionMode, Optional[bytes]] = {
            _SubscriptionMode.FULL: None,
            _SubscriptionMode.IMAGE_ONLY: None,
            _SubscriptionMode.TRACKS_ONLY: None,
        }
        # Persist frames off-thread to avoid blocking gRPC handlers on disk I/O.
        self._storage_queue: "queue.Queue[Optional[CachedImage]]" = queue.Queue(maxsize=_STORAGE_QUEUE_LIMIT)
        self._storage_stop = threading.Event()
        self._storage_thread: Optional[threading.Thread] = threading.Thread(
            target=self._storage_loop,
            name="storage-writer",
            daemon=True,
        )
        self._storage_thread.start()

        allowed_formats = {"raw8", "raw", "jpeg", "jpg", "png", "tif", "tiff"}
        preferred_stream = _STREAM_FORMAT_DEFAULT if _STREAM_FORMAT_DEFAULT in allowed_formats else "raw8"

        if preferred_stream in {"jpeg", "jpg"} and (not _TURBO_ENCODER.available() and cv2 is None):
            LOGGER.warning("JPEG stream requested but no encoder available; defaulting to raw8")
            preferred_stream = "raw8"
        if preferred_stream == "png" and cv2 is None:
            LOGGER.warning("PNG stream requested but OpenCV missing; defaulting to raw8")
            preferred_stream = "raw8"
        if preferred_stream in {"tif", "tiff"} and tifffile is None:
            LOGGER.warning("TIFF stream requested but tifffile missing; defaulting to raw8")
            preferred_stream = "raw8"

        self._stream_format = preferred_stream

        if cv2 is None and tifffile is not None:
            self._storage.image_format = "tiff"
            LOGGER.warning("OpenCV unavailable; defaulting storage to TIFF")
        elif cv2 is None and tifffile is None:
            LOGGER.warning("No encoder libraries detected; disable storage or install OpenCV/tifffile")
        self._tracker.set_result_callback(self.on_track_result)
        atexit.register(self.shutdown)

    def UploadImage(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        sequence = int(getattr(request, "sequence", 0))
        cached = CachedImage(
            filename=request.filename,
            timestamp_ms=int(request.timestamp_ms),
            source=request.source or "",
            data=bytes(request.data),
            received_at_ms=int(time.time() * 1000),
            sequence=sequence,
        )
        with self._lock:
            if cached.sequence <= 0:
                cached.sequence = self._next_sequence
            if cached.sequence < self._next_sequence:
                LOGGER.warning(
                    "Ignoring stale image %s (seq %d < %d)",
                    cached.filename,
                    cached.sequence,
                    self._next_sequence,
                )
                return UploadAck(ok=False, message="sequence_out_of_order")
            if cached.sequence in self._pending:
                LOGGER.warning("Duplicate sequence %d for %s", cached.sequence, cached.filename)
                return UploadAck(ok=False, message="sequence_duplicate")

            self._pending[cached.sequence] = cached
            stored_sequences: List[CachedImage] = []
            while self._next_sequence in self._pending:
                committed = self._pending.pop(self._next_sequence)
                self._latest = committed
                stored_sequences.append(committed)
                self._next_sequence += 1
            next_expected = self._next_sequence

        for committed in stored_sequences:
            LOGGER.info(
                "Committed %s (seq %d, %d bytes) from %s; queued for storage",
                committed.filename,
                committed.sequence,
                len(committed.data),
                committed.source or "unknown",
            )
            self._enqueue_storage(committed)
            self._tracker.enqueue(committed)

        if any(committed.sequence == cached.sequence for committed in stored_sequences):
            return UploadAck(ok=True, message="stored")
        LOGGER.debug(
            "Queued %s (seq %d, %d bytes) awaiting sequence %d",
            cached.filename,
            cached.sequence,
            len(cached.data),
            next_expected,
        )
        return UploadAck(ok=True, message="queued")

    @staticmethod
    def _resolve_stream_mode(raw: Optional[str]) -> Tuple[_SubscriptionMode, bool]:
        if raw is None:
            return _SubscriptionMode.FULL, True
        normalized = raw.strip().lower()
        if not normalized:
            return _SubscriptionMode.FULL, True
        if normalized in {"image", "image-only", "image_only", "images"}:
            return _SubscriptionMode.IMAGE_ONLY, True
        if normalized in {"tracks", "track", "tracks-only", "tracks_only", "detections", "detections-only"}:
            return _SubscriptionMode.TRACKS_ONLY, True
        return _SubscriptionMode.FULL, False

    def _stream_mode_from_metadata(self, context: grpc.ServicerContext) -> _SubscriptionMode:
        raw_value: Optional[str] = None
        try:
            metadata = context.invocation_metadata()
        except Exception:
            metadata = ()  # pragma: no cover - defensive fallback
        for item in metadata:
            key = getattr(item, "key", "").lower()
            if key in {"stream-mode", "stream_mode", "streammode"}:
                raw_value = getattr(item, "value", None)
                break
        mode, recognized = self._resolve_stream_mode(raw_value)
        if raw_value and not recognized:
            LOGGER.warning("Unknown stream mode '%s'; defaulting to full payload", raw_value)
        return mode

    def StreamFrames(self, request: Any, context: grpc.ServicerContext) -> Iterable[Any]:  # noqa: N802
        mode = self._stream_mode_from_metadata(context)
        subscriber_id, subscriber = self._register_subscriber(mode)
        try:
            initial = None
            with self._lock:
                initial = self._latest_envelopes_serialized.get(mode)
            if initial is not None:
                envelope = FrameEnvelope()
                envelope.ParseFromString(initial)
                yield envelope
            while context.is_active():
                try:
                    payload = subscriber.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                envelope = FrameEnvelope()
                envelope.ParseFromString(payload)
                yield envelope
        finally:
            self._unregister_subscriber(subscriber_id)

    def GetLatestImage(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        with self._lock:
            cached = self._latest
        if cached is None:
            return LatestImageReply(has_image=False)
        return LatestImageReply(
            has_image=True,
            filename=cached.filename,
            timestamp_ms=cached.timestamp_ms,
            source=cached.source,
            data=cached.data,
            sequence=cached.sequence,
        )

    def GetLatestTracks(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        result = self._tracker.get_latest()
        payload = struct_pb2.Struct()
        if result is None:
            payload.update({"has_tracks": False})
            return payload
        latency_ms = 0
        if result.received_at_ms and result.processed_at_ms:
            latency_ms = max(0, result.processed_at_ms - result.received_at_ms)
        restart_count, last_restart_ms = self._tracker.get_restart_stats()
        payload.update(
            {
                "has_tracks": True,
                "sequence": int(result.sequence),
                "filename": result.filename,
                "timestamp_ms": int(result.timestamp_ms),
                "source": result.source,
                "received_at_ms": int(result.received_at_ms),
                "processed_at_ms": int(result.processed_at_ms),
                "processing_ms": int(result.processing_ms),
                "latency_ms": int(latency_ms),
                "image_height": int(result.image_shape[0] if result.image_shape else 0),
                "image_width": int(result.image_shape[1] if result.image_shape else 0),
                "detection_count": int(len(result.detections)),
                "tracking_restart_count": int(restart_count),
                "tracking_last_restart_ms": int(last_restart_ms),
                "detections": [
                    {
                        "x": float(det.x),
                        "y": float(det.y),
                        "mass": float(det.mass),
                        "ecc": float(det.ecc),
                        "size": float(det.size),
                        "signal": float(det.signal),
                    }
                    for det in result.detections
                ],
            }
        )
        return payload

    def GetTrackingConfig(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        config_struct = struct_pb2.Struct()
        json_format.ParseDict(self._tracker.get_config_dict(), config_struct)
        return config_struct

    def UpdateTrackingConfig(self, request: struct_pb2.Struct, context: grpc.ServicerContext) -> Any:  # noqa: N802
        updates = json_format.MessageToDict(request, preserving_proto_field_name=True)
        try:
            new_config = self._tracker.update_config(updates)
            payload = {"ok": True, "config": new_config.to_dict()}
        except Exception as exc:  # pragma: no cover - defensive catch
            TRACK_LOGGER.exception("Failed to update tracking configuration")
            payload = {"ok": False, "error": str(exc)}
        response = struct_pb2.Struct()
        json_format.ParseDict(payload, response)
        return response

    def GetStorageConfig(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        with self._storage_lock:
            return self._storage.to_proto()

    def UpdateStorageConfig(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        with self._storage_lock:
            self._storage.update_from_proto(request)
            updated = self._storage.to_proto()
        LOGGER.info(
            "Storage config updated: enabled=%s hdf5=%s target_fps=%.2f format=%s output=%s hdf5_path=%s",
            updated.enabled,
            updated.hdf5_enabled,
            updated.target_fps,
            updated.image_format,
            updated.output_dir,
            updated.hdf5_path,
        )
        return updated

    def on_track_result(self, image: CachedImage, result: TrackResult) -> None:
        if result.image is None:
            return
        try:
            preserved = _decode_image_bytes_preserve(image.data)
        except Exception:
            LOGGER.exception("Failed to decode preserved image data for streaming (seq=%d)", result.sequence)
            preserved = result.image
        try:
            uint8_image = _normalize_to_uint8(preserved)
        except Exception:
            LOGGER.exception("Failed to convert image for streaming (seq=%d)", result.sequence)
            return
        try:
            image_bytes, image_format = _prepare_stream_payload(uint8_image, self._stream_format)
        except Exception:
            LOGGER.exception("Failed to encode image for streaming (seq=%d)", result.sequence)
            return

        envelope = FrameEnvelope()
        metadata = envelope.metadata
        metadata.filename = result.filename
        metadata.sequence = int(result.sequence)
        metadata.timestamp_ms = int(result.timestamp_ms)
        metadata.source = result.source
        metadata.image_width = int(result.image_shape[1] if result.image_shape else 0)
        metadata.image_height = int(result.image_shape[0] if result.image_shape else 0)
        metadata.processing_ms = int(result.processing_ms)
        latency_ms = 0
        if result.received_at_ms and result.processed_at_ms:
            latency_ms = max(0, result.processed_at_ms - result.received_at_ms)
        metadata.latency_ms = int(latency_ms)
        metrics = self._pop_storage_metrics(result.sequence)
        if metrics is not None:
            metadata.storage_ms = int(round(metrics.duration_ms))
            metadata.storage_saved = bool(metrics.success)
            metadata.storage_kind = metrics.kind
            metadata.storage_path = str(metrics.path) if metrics.path is not None else ""
            if hasattr(metadata, "storage_ratio"):
                metadata.storage_ratio = float(metrics.compression_ratio)
            if hasattr(metadata, "storage_bytes_in"):
                metadata.storage_bytes_in = int(metrics.bytes_in)
            if hasattr(metadata, "storage_bytes_out"):
                metadata.storage_bytes_out = int(metrics.bytes_out)
            if hasattr(metadata, "storage_codec"):
                metadata.storage_codec = metrics.codec
            if hasattr(metadata, "storage_throttle_ms"):
                metadata.storage_throttle_ms = float(metrics.throttle_ms)
            if hasattr(metadata, "storage_message"):
                metadata.storage_message = metrics.message
        else:
            metadata.storage_ms = 0
            metadata.storage_saved = False
            metadata.storage_kind = "pending"
            metadata.storage_path = ""
            if hasattr(metadata, "storage_ratio"):
                metadata.storage_ratio = 0.0
            if hasattr(metadata, "storage_bytes_in"):
                metadata.storage_bytes_in = 0
            if hasattr(metadata, "storage_bytes_out"):
                metadata.storage_bytes_out = 0
            if hasattr(metadata, "storage_codec"):
                metadata.storage_codec = ""
            if hasattr(metadata, "storage_throttle_ms"):
                metadata.storage_throttle_ms = 0.0
            if hasattr(metadata, "storage_message"):
                metadata.storage_message = ""

        envelope.image_data = image_bytes
        envelope.image_format = image_format
        envelope.detection_count = len(result.detections)
        envelope.has_tracks = bool(result.detections)
        for detection in result.detections:
            proto = envelope.detections.add()
            proto.x = detection.x
            proto.y = detection.y
            proto.mass = detection.mass
            proto.ecc = detection.ecc
            proto.size = detection.size
            proto.signal = detection.signal

        tracking_field = envelope.DESCRIPTOR.fields_by_name.get("tracking_config_snapshot")
        if tracking_field is not None:
            # Populate the snapshot directly on the embedded Struct instance to avoid type mismatches.
            envelope.tracking_config_snapshot.Clear()
            json_format.ParseDict(self._tracker.get_config_dict(), envelope.tracking_config_snapshot)

        envelope_full = envelope
        serialized_full = envelope_full.SerializeToString()

        envelope_image_only = FrameEnvelope()
        envelope_image_only.CopyFrom(envelope_full)
        envelope_image_only.ClearField("detections")
        envelope_image_only.detection_count = 0
        envelope_image_only.has_tracks = False
        serialized_image_only = envelope_image_only.SerializeToString()

        envelope_tracks_only = FrameEnvelope()
        envelope_tracks_only.CopyFrom(envelope_full)
        envelope_tracks_only.image_data = b""
        envelope_tracks_only.image_format = ""
        serialized_tracks_only = envelope_tracks_only.SerializeToString()

        serialized_by_mode = {
            _SubscriptionMode.FULL: serialized_full,
            _SubscriptionMode.IMAGE_ONLY: serialized_image_only,
            _SubscriptionMode.TRACKS_ONLY: serialized_tracks_only,
        }

        with self._lock:
            for mode, payload in serialized_by_mode.items():
                self._latest_envelopes_serialized[mode] = payload
            subscribers = list(self._subscribers.values())

        for subscriber in subscribers:
            payload = serialized_by_mode.get(subscriber.mode, serialized_full)
            self._publish_to_subscription(subscriber.queue, payload)

        result.image = None

    def _publish_to_subscription(self, subscription: "queue.Queue[bytes]", payload: bytes) -> None:
        try:
            subscription.put_nowait(payload)
        except queue.Full:
            try:
                subscription.get_nowait()
            except queue.Empty:
                pass
            try:
                subscription.put_nowait(payload)
            except queue.Full:
                LOGGER.debug("Dropping frame for a slow subscriber")

    def _register_subscriber(self, mode: _SubscriptionMode) -> Tuple[int, _Subscriber]:
        subscription_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=4)
        subscriber = _Subscriber(queue=subscription_queue, mode=mode)
        with self._lock:
            self._subscriber_counter += 1
            subscriber_id = self._subscriber_counter
            self._subscribers[subscriber_id] = subscriber
            total = len(self._subscribers)
        LOGGER.info(
            "Registered frame subscriber %d (mode=%s, total=%d)",
            subscriber_id,
            subscriber.mode.value,
            total,
        )
        return subscriber_id, subscriber

    def _unregister_subscriber(self, subscriber_id: int) -> None:
        with self._lock:
            removed = self._subscribers.pop(subscriber_id, None)
            total = len(self._subscribers)
        if removed is not None:
            LOGGER.info(
                "Removed frame subscriber %d (mode=%s, total=%d)",
                subscriber_id,
                removed.mode.value,
                total,
            )

    def _record_storage_drop(self, image: CachedImage, *, kind: str, message: str) -> None:
        metrics = StorageMetrics(
            sequence=image.sequence,
            duration_ms=0.0,
            kind=kind,
            path=None,
            success=False,
            timestamp_ms=image.received_at_ms,
            message=message,
            bytes_in=len(image.data),
            bytes_out=0,
            compression_ratio=0.0,
            codec="",
            throttle_ms=0.0,
        )
        with self._storage_lock:
            self._storage_metrics[image.sequence] = metrics

    def _enqueue_storage(self, image: CachedImage) -> None:
        if self._storage_stop.is_set():
            LOGGER.warning("Storage worker stopped; dropping frame seq=%d", image.sequence)
            self._record_storage_drop(image, kind="queue-drop", message="storage worker stopped")
            return
        try:
            self._storage_queue.put_nowait(image)
        except queue.Full:
            LOGGER.error("Storage queue full; dropping frame seq=%d", image.sequence)
            self._record_storage_drop(image, kind="queue-drop", message="storage queue full")

    def _storage_loop(self) -> None:
        while True:
            try:
                item = self._storage_queue.get(timeout=0.5)
            except queue.Empty:
                if self._storage_stop.is_set():
                    break
                continue
            if item is None:
                self._storage_queue.task_done()
                break
            image = item
            try:
                metrics = self._save_received_image(image)
                self._log_image_receipt(image, metrics)
            except Exception:
                LOGGER.exception("Storage worker failed for seq=%d", image.sequence)
            finally:
                self._storage_queue.task_done()

    def _write_file_storage_locked(
        self,
        storage: StorageState,
        image: CachedImage,
        image_format: str,
        decoded_for_hdf5: Optional[np.ndarray],
    ) -> Tuple[Optional[Path], int, str]:
        storage.ensure_output_dir()
        target_dir = storage.output_dir
        stem = Path(image.filename or f"frame_{image.sequence:06d}").stem
        original_suffix = Path(image.filename).suffix if image.filename else ""
        if not original_suffix:
            original_suffix = ".bin"
        suffix_map = {
            "native": original_suffix,
            "tiff": ".tiff",
            "tif": ".tiff",
        }
        suffix = suffix_map.get(image_format, original_suffix)
        path = _ensure_unique_path(target_dir / f"{stem}{suffix}", sequence_hint=image.sequence)

        bytes_written = 0
        codec_label = "native"
        decoded_original = decoded_for_hdf5

        if image_format in {"native", "tiff", "tif"}:
            if image.data:
                path.write_bytes(image.data)
                bytes_written = len(image.data)
                codec_label = "native"
            else:
                if decoded_original is None:
                    decoded_original = _decode_image_bytes_preserve(image.data)
                if tifffile is None:
                    raise RuntimeError("tifffile is required to re-encode TIFF data")
                with io.BytesIO() as bio:
                    tifffile.imwrite(bio, np.ascontiguousarray(decoded_original))
                    payload = bio.getvalue()
                path.write_bytes(payload)
                bytes_written = len(payload)
                codec_label = "tiff"
        else:
            if decoded_original is None:
                decoded_original = _decode_image_bytes_preserve(image.data)
            if tifffile is None:
                raise RuntimeError("tifffile is required to convert frames to TIFF")
            with io.BytesIO() as bio:
                tifffile.imwrite(bio, np.ascontiguousarray(decoded_original))
                payload = bio.getvalue()
            path.write_bytes(payload)
            bytes_written = len(payload)
            codec_label = "tiff"

        LOGGER.info(
            "Saved frame seq=%d to %s (%s, %d bytes)",
            image.sequence,
            path,
            codec_label,
            bytes_written,
        )
        return path, bytes_written, codec_label

    def _pop_storage_metrics(self, sequence: int) -> Optional[StorageMetrics]:
        with self._storage_lock:
            return self._storage_metrics.pop(sequence, None)

    def shutdown(self) -> None:
        if self._storage_thread is not None and self._storage_thread.is_alive():
            self._storage_stop.set()
            try:
                self._storage_queue.join()
            except Exception:
                LOGGER.exception("Storage queue join failed during shutdown")
            try:
                self._storage_queue.put_nowait(None)
            except queue.Full:
                try:
                    self._storage_queue.get_nowait()
                    self._storage_queue.task_done()
                    LOGGER.warning("Dropped pending storage frame during shutdown to flush sentinel")
                except queue.Empty:
                    pass
                self._storage_queue.put(None)
            self._storage_thread.join(timeout=5)
            self._storage_thread = None
        else:
            self._storage_stop.set()
        with self._storage_lock:
            self._storage.shutdown()

    def _save_received_image(self, image: CachedImage) -> StorageMetrics:
        start_time = time.perf_counter()
        bytes_in = len(image.data)
        file_bytes_out = 0
        hdf5_bytes_out = 0
        file_codec = ""
        saved_path: Optional[Path] = None
        wait_ms = 0.0
        operations: List[str] = []
        message = ""
        success = False

        with self._storage_lock:
            storage_enabled = self._storage.enabled
            hdf5_enabled = self._storage.hdf5_enabled
            image_format = self._storage.sanitized_format()
            target_fps = self._storage.target_fps
            last_monotonic = self._storage.last_save_monotonic

            if not storage_enabled and not hdf5_enabled:
                metrics = StorageMetrics(
                    sequence=image.sequence,
                    duration_ms=0.0,
                    kind="disabled",
                    path=None,
                    success=False,
                    timestamp_ms=image.received_at_ms,
                )
                self._storage_metrics[image.sequence] = metrics
                return metrics

            wait_seconds = 0.0
            if storage_enabled and target_fps > 0 and last_monotonic > 0:
                interval = 1.0 / max(target_fps, 1e-6)
                next_allowed = last_monotonic + interval
                now = time.perf_counter()
                if next_allowed > now:
                    wait_seconds = next_allowed - now
                    wait_ms = wait_seconds * 1000.0
            if wait_seconds > 0.0:
                operations.append("throttle")
                time.sleep(wait_seconds)

            decoded_for_hdf5: Optional[np.ndarray] = None

            try:
                if storage_enabled:
                    self._storage.ensure_output_dir()
                if hdf5_enabled:
                    decoded_for_hdf5 = _decode_image_bytes_preserve(image.data)
                if storage_enabled:
                    file_path, file_bytes_out, file_codec = self._write_file_storage_locked(
                        self._storage,
                        image,
                        image_format,
                        decoded_for_hdf5,
                    )
                    if file_path is not None and file_bytes_out >= 0:
                        operations.append(f"file-{file_codec}")
                        saved_path = file_path
                        success = True
                if hdf5_enabled:
                    if decoded_for_hdf5 is None:
                        decoded_for_hdf5 = _decode_image_bytes_preserve(image.data)
                    try:
                        hdf5_path, _, hdf5_bytes_out = self._storage.append_hdf5(image, decoded_for_hdf5, image_format)
                        operations.append("hdf5")
                        if saved_path is None:
                            saved_path = hdf5_path
                        success = True
                    except Exception:
                        operations.append("hdf5-error")
                        raise
                if success and storage_enabled:
                    self._storage.last_save_monotonic = time.perf_counter()
            except Exception as exc:
                message = str(exc)
                LOGGER.exception("Failed to write frame seq=%d", image.sequence)
                operations.append("error")
                success = False

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        kind = "+".join(operations) if operations else "skipped"
        bytes_out = file_bytes_out or hdf5_bytes_out
        compression_ratio = (file_bytes_out / bytes_in) if (bytes_in and file_bytes_out) else 0.0
        if file_bytes_out and bytes_in:
            LOGGER.info(
                "Compression seq=%d codec=%s ratio=%.1f%% (%d -> %d bytes)",
                image.sequence,
                file_codec or image_format,
                compression_ratio * 100.0,
                bytes_in,
                file_bytes_out,
            )

        metrics = StorageMetrics(
            sequence=image.sequence,
            duration_ms=duration_ms,
            kind=kind,
            path=saved_path,
            success=success,
            timestamp_ms=image.received_at_ms,
            message=message,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            compression_ratio=compression_ratio,
            codec=file_codec or image_format,
            throttle_ms=wait_ms,
        )
        with self._storage_lock:
            self._storage_metrics[image.sequence] = metrics
        return metrics

    def _log_image_receipt(self, image: CachedImage, metrics: StorageMetrics) -> None:
        with self._storage_lock:
            log_dir = self._storage.output_dir
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            LOGGER.exception("Failed to prepare log directory for seq=%d", image.sequence)
            return

        try:
            timestamp = datetime.fromtimestamp(image.received_at_ms / 1000.0, timezone.utc)
            timestamp_str = timestamp.isoformat().replace("+00:00", "Z")
        except Exception:
            timestamp_str = str(image.received_at_ms)

        filename = Path(image.filename).name if image.filename else f"frame_{image.sequence:06d}"
        saved_name = metrics.path.name if metrics.path is not None else ""
        log_line = (
            f"{timestamp_str}\t{filename}\t{saved_name}\t{metrics.kind}\t"
            f"{metrics.duration_ms:.3f}\t{int(metrics.success)}\n"
        )
        log_path = log_dir / "storage.log"
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(log_line)
        except Exception:
            LOGGER.exception("Failed to append storage log for seq=%d", image.sequence)


def serve(
    host: str,
    port: int,
    max_workers: int,
    max_message_bytes: int,
    tracking_config: DetectionConfig,
    tracker_cpu_ids: Optional[Sequence[int]] = None,
) -> None:
    options = [
        ("grpc.max_send_message_length", max_message_bytes),
        ("grpc.max_receive_message_length", max_message_bytes),
    ]
    tracker = TrackingManager(tracking_config, allowed_cpu_ids=tracker_cpu_ids)
    servicer = ImageExchangeServicer(tracker)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers), options=options)
    add_ImageExchangeServicer_to_server(servicer, server)
    address = f"{host}:{port}"
    server.add_insecure_port(address)
    server.start()
    LOGGER.info("Image server with tracking listening on %s", address)
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        LOGGER.info("Shutting down image server")
    finally:
        servicer.shutdown()
        tracker.shutdown()
        server.stop(5).wait()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the gRPC image server that receives, stores, and tracks TIFF images.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=50052, help="Bind port")
    parser.add_argument("--max-workers", type=int, default=8, help="Thread pool size for gRPC")
    parser.add_argument(
        "--max-message-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="Maximum gRPC message size for send/receive",
    )
    parser.add_argument(
        "--tracker-processes",
        type=int,
        default=min(32, os.cpu_count() or 8),
        help="Maximum number of worker processes/threads used for trackpy tile processing",
    )
    parser.add_argument(
        "--tracker-backend",
        choices=["auto", "thread", "process"],
        default="auto",
        help="Execution backend for tile detection workers",
    )
    parser.add_argument(
        "--tracker-cpus",
        type=_parse_cpu_list_arg,
        default=None,
        help=(
            "Optional comma-separated list or ranges of CPU indices dedicated to tracking workers "
            "(e.g. '4,6-7')."
        ),
    )
    parser.add_argument("--tile-width", type=int, default=512, help="Tile width used for parallel detection")
    parser.add_argument("--tile-height", type=int, default=512, help="Tile height used for parallel detection")
    parser.add_argument("--tile-overlap", type=int, default=48, help="Tile overlap in pixels to avoid border loss")
    parser.add_argument("--track-diameter", type=int, default=21, help="Particle diameter passed to trackpy")
    parser.add_argument("--track-separation", type=int, default=18, help="Minimum separation between particles")
    parser.add_argument("--track-percentile", type=float, default=14.0, help="Brightness percentile threshold")
    parser.add_argument("--track-minmass", type=float, default=100.0, help="Minimum mass filter")
    parser.add_argument(
        "--track-maxmass",
        type=float,
        default=0.0,
        help="Maximum mass filter (0 disables upper bound)",
    )
    parser.add_argument(
        "--track-pixel-threshold",
        type=float,
        default=0.0,
        help="Discard detections whose peak intensity is below this value",
    )
    parser.add_argument(
        "--no-track-preprocess",
        action="store_false",
        dest="track_preprocess",
        default=True,
        help="Disable bandpass pre-filtering before detection",
    )
    parser.add_argument("--track-lshort", type=int, default=1, help="Short length scale for bandpass filter")
    parser.add_argument("--track-llong", type=int, default=21, help="Long length scale for bandpass filter")
    parser.add_argument(
        "--track-min-ecc",
        type=float,
        default=-1.0,
        help="Minimum eccentricity filter (-1 disables lower bound)",
    )
    parser.add_argument(
        "--track-max-ecc",
        type=float,
        default=-1.0,
        help="Maximum eccentricity filter (-1 disables upper bound)",
    )
    parser.add_argument(
        "--track-refine",
        type=int,
        default=1,
        help="Maximum refinement iterations for trackpy.locate",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_args(argv)


def _build_detection_config(args: argparse.Namespace) -> DetectionConfig:
    return DetectionConfig(
        diameter=args.track_diameter,
        separation=args.track_separation,
        percentile=args.track_percentile,
        minmass=args.track_minmass,
        maxmass=args.track_maxmass,
        pixel_threshold=args.track_pixel_threshold,
        preprocess=bool(args.track_preprocess),
        lshort=args.track_lshort,
        llong=args.track_llong,
        min_ecc=args.track_min_ecc,
        max_ecc=args.track_max_ecc,
        refine=args.track_refine,
        tile_width=args.tile_width,
        tile_height=args.tile_height,
        tile_overlap=args.tile_overlap,
        max_workers=args.tracker_processes,
        worker_backend=args.tracker_backend,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = _build_detection_config(args)
    serve(
        host=args.host,
        port=args.port,
        max_workers=args.max_workers,
        max_message_bytes=args.max_message_bytes,
        tracking_config=config,
        tracker_cpu_ids=args.tracker_cpus,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
