"""Monitor a directory for new TIFF images and compress them to lossless JPEG XL."""

from __future__ import annotations

import argparse
import atexit
import ctypes
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, List, Optional, Sequence, Set, Tuple, Union, cast


try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "The 'watchdog' package is required. Install it with 'pip install watchdog'."
    ) from exc


TIFF_SUFFIXES = {".tif", ".tiff"}


def _parse_cpu_list(spec: str) -> List[int]:
    values: List[int] = []
    seen: Set[int] = set()
    for raw_part in spec.split(","):
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
                    values.append(cpu)
            continue
        try:
            cpu_idx = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid CPU index '{part}'") from exc
        if cpu_idx < 0:
            raise argparse.ArgumentTypeError("CPU indices must be non-negative")
        if cpu_idx not in seen:
            seen.add(cpu_idx)
            values.append(cpu_idx)
    if not values:
        raise argparse.ArgumentTypeError("CPU affinity list cannot be empty")
    return values


def _cpu_affinity_arg(value: str) -> List[int]:
    return _parse_cpu_list(value)


def apply_process_cpu_affinity(cpu_ids: Sequence[int]) -> None:
    unique = []
    seen: Set[int] = set()
    for cpu in cpu_ids:
        idx = int(cpu)
        if idx < 0:
            raise ValueError("CPU indices must be non-negative")
        if idx in seen:
            continue
        seen.add(idx)
        unique.append(idx)
    if not unique:
        raise ValueError("CPU affinity set cannot be empty")

    pid = os.getpid()

    sched_setaffinity = getattr(os, "sched_setaffinity", None)
    if callable(sched_setaffinity):
        try:
            sched_setaffinity(pid, set(unique))
            logging.debug("Pinned PID %d to CPUs %s via sched_setaffinity", pid, unique)
            return
        except Exception as exc:  # noqa: BLE001
            logging.debug("sched_setaffinity failed for CPUs %s: %s", unique, exc)

    try:
        import psutil  # type: ignore

        try:
            psutil.Process(pid).cpu_affinity(unique)
            logging.debug("Pinned PID %d to CPUs %s via psutil", pid, unique)
            return
        except Exception as exc:  # noqa: BLE001
            logging.debug("psutil cpu_affinity failed for CPUs %s: %s", unique, exc)
    except ImportError:
        pass

    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            mask = 0
            for cpu in unique:
                mask |= 1 << cpu
            handle = kernel32.GetCurrentProcess()
            if kernel32.SetProcessAffinityMask(handle, mask) == 0:
                raise ctypes.WinError()
            logging.debug(
                "Pinned PID %d to CPUs %s via SetProcessAffinityMask", pid, unique
            )
            return
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "Failed to set process affinity mask for CPUs %s: %s", unique, exc
            )
            return

    logging.warning("Could not set CPU affinity for PID %d to %s", pid, unique)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor a directory for new TIFF images and compress them to JPEG XL in "
            "lossless mode using multiple worker processes."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Directory to watch for incoming TIFF images.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Directory where JPEG XL files will be written.",
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="Number of worker processes to use for compression.",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=None,
        help=(
            "Maximum number of images to process before exiting. "
            "If omitted, the watcher runs indefinitely."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help=(
            "Deprecated; retained for backwards compatibility with older scripts."
        ),
    )
    parser.add_argument(
        "--stability-seconds",
        type=float,
        default=1.0,
        help=(
            "Minimum age for a file (in seconds) before it is considered ready. "
            "Helps avoid reading files that are still being written."
        ),
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=100,
        help="JPEG XL quality (0-100). Use 100 for lossless compression.",
    )
    parser.add_argument(
        "--effort",
        type=int,
        default=7,
        help="JPEG XL encoder effort (1-10). Higher values compress more but take longer.",
    )
    parser.add_argument(
        "--decoding-speed",
        type=int,
        default=0,
        help="JPEG XL decoding speed hint (0-4). Higher values trade compression ratio for decoding speed.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    parser.add_argument(
        "--cpu-affinity",
        type=_cpu_affinity_arg,
        default=None,
        help=(
            "Optional comma-separated list or ranges of CPU indices reserved for worker "
            "processes (e.g. '0,2-3')."
        ),
    )
    args = parser.parse_args()

    if not 0 <= args.quality <= 100:
        parser.error("--quality must be between 0 and 100")
    if not 1 <= args.effort <= 10:
        parser.error("--effort must be between 1 and 10")
    if not 0 <= args.decoding_speed <= 4:
        parser.error("--decoding-speed must be between 0 and 4")

    return args


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_image_with_opencv(src_path: Path) -> Tuple[Any, str, int]:
    try:
        import importlib

        cv2 = cast(Any, importlib.import_module("cv2"))
        np = cast(Any, importlib.import_module("numpy"))
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "OpenCV (cv2) and NumPy are required for compression. Install them with 'pip install opencv-python-headless numpy'."
        ) from exc

    image = cv2.imread(str(src_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"OpenCV failed to decode {src_path}")

    if image.ndim == 2:
        processed = image
        colorspace = "L"
    elif image.ndim == 3:
        channels = image.shape[2]
        if channels == 1:
            processed = image[:, :, 0]
            colorspace = "L"
        elif channels == 3:
            processed = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            colorspace = "RGB"
        elif channels == 4:
            processed = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
            colorspace = "RGBA"
        else:
            raise RuntimeError(
                f"Unsupported channel count ({channels}) in {src_path}; expected 1, 3, or 4 channels."
            )
    else:
        raise RuntimeError(f"Unsupported image shape {image.shape} in {src_path}")

    dtype = processed.dtype
    if dtype == np.uint8:
        bit_depth = 8
    elif dtype == np.uint16:
        bit_depth = 16
    else:
        raise RuntimeError(
            f"Unsupported TIFF data type {dtype}; only uint8/uint16 are handled without modification."
        )

    contiguous = np.ascontiguousarray(processed)
    return contiguous, colorspace, bit_depth


def encode_with_jxlpy(
    image: Any,
    *,
    colorspace: str,
    bit_depth: int,
    output_path: Path,
    quality: int,
    effort: int,
    decoding_speed: int,
) -> None:
    try:
        import importlib

        jxlpy = importlib.import_module("jxlpy")
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "The 'jxlpy' package is required for JPEG XL encoding. Install it with 'pip install jxlpy'."
        ) from exc

    height, width = image.shape[:2]
    encoder_kwargs: dict[str, Any] = {
        "quality": quality,
        "size": (width, height),
        "colorspace": colorspace,
        "bit_depth": bit_depth,
        "effort": effort,
        "decoding_speed": decoding_speed,
        "use_container": True,
    }
    if "A" in colorspace:
        encoder_kwargs["alpha_bit_depth"] = bit_depth

    encoder = jxlpy.JXLPyEncoder(**encoder_kwargs)
    try:
        encoder.add_frame(image.tobytes())
        data = encoder.get_output()
    finally:
        try:
            encoder.close()
        except Exception:  # noqa: BLE001
            pass

    output_path.write_bytes(data)


def compress_task(
    src: str,
    dst_dir: str,
    quality: int,
    effort: int,
    decoding_speed: int,
) -> str:
    src_path = Path(src)
    output_dir = Path(dst_dir)
    output_path = output_dir / f"{src_path.stem}.jxl"

    src_stat = src_path.stat()
    if output_path.exists():
        out_stat = output_path.stat()
        if out_stat.st_mtime >= src_stat.st_mtime:
            logging.debug("Skipping %s; existing output is up-to-date", src_path)
            return str(output_path)

    image, colorspace, bit_depth = load_image_with_opencv(src_path)
    encode_with_jxlpy(
        image,
        colorspace=colorspace,
        bit_depth=bit_depth,
        output_path=output_path,
        quality=quality,
        effort=effort,
        decoding_speed=decoding_speed,
    )

    logging.debug(
        "Encoded %s via jxlpy (colorspace=%s, bit_depth=%d, quality=%d, effort=%d, decoding_speed=%d)",
        src_path,
        colorspace,
        bit_depth,
        quality,
        effort,
        decoding_speed,
    )

    result = str(output_path)

    try:
        src_path.unlink()
        logging.debug("Deleted original file %s after compression", src_path)
    except FileNotFoundError:
        logging.debug("Original file %s already removed after compression", src_path)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to delete original file %s: %s", src_path, exc)

    return result


def is_ready_for_processing(
    path: Path,
    stability_seconds: float,
    stat_result: Optional[os.stat_result] = None,
) -> bool:
    if stat_result is None:
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return False

    if stat_result.st_size <= 0:
        return False

    if stability_seconds <= 0:
        return True

    age = time.time() - stat_result.st_mtime
    return age >= stability_seconds


def discover_candidates(
    directory: Path,
    known_paths: Set[Path],
    stability_seconds: float,
) -> Set[Path]:
    candidates: Set[Path] = set()
    try:
        with os.scandir(directory) as scanner:
            for entry in scanner:
                if not entry.is_file(follow_symlinks=False):
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix not in TIFF_SUFFIXES:
                    continue
                path = Path(entry.path)
                if path in known_paths:
                    continue
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not is_ready_for_processing(path, stability_seconds, stat_result):
                    continue
                candidates.add(path)
    except FileNotFoundError:
        logging.warning("Input directory %s does not exist yet.", directory)
    return candidates


class TiffEventHandler(FileSystemEventHandler):
    """Dispatch filesystem events for TIFF files into a shared queue."""

    def __init__(self, queue: Queue[Path]) -> None:
        super().__init__()
        self._queue = queue

    def _enqueue_path(self, path_input: Union[str, bytes]) -> None:
        path = Path(os.fsdecode(path_input))
        if path.suffix.lower() not in TIFF_SUFFIXES:
            return
        self._queue.put(path)

    def on_created(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue_path(event.src_path)

    def on_modified(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue_path(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue_path(event.dest_path)


def wait_for_file_stability(path: Path, stability_seconds: float) -> bool:
    """Block until the file looks stable on disk, or disappears."""

    if stability_seconds <= 0:
        return path.exists()

    sleep_interval = max(0.1, min(0.5, stability_seconds / 3))

    while True:
        if is_ready_for_processing(path, stability_seconds):
            return True
        if not path.exists():
            return False
        time.sleep(sleep_interval)


def get_available_cpu_indices() -> List[int]:
    """Return CPU indices available to the current process."""
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if callable(sched_getaffinity):
        try:
            affinity = sched_getaffinity(0)
            affinity_set = cast(Set[int], affinity)
            if affinity_set:
                return sorted(affinity_set)
        except Exception as exc:  # noqa: BLE001
            logging.debug("sched_getaffinity failed: %s", exc)

    try:
        import psutil  # type: ignore

        try:
            affinity = psutil.Process().cpu_affinity()
            affinity_list = cast(List[int], affinity)
            if affinity_list:
                return sorted(affinity_list)
        except Exception as exc:  # noqa: BLE001
            logging.debug("psutil cpu_affinity lookup failed: %s", exc)
    except ImportError:
        pass

    cpu_count = os.cpu_count() or 1
    return list(range(cpu_count))


def set_process_cpu_affinity(cpu_index: int) -> None:
    """Pin the current process to a specific CPU core when possible."""
    pid = os.getpid()

    sched_setaffinity = getattr(os, "sched_setaffinity", None)
    if callable(sched_setaffinity):
        try:
            sched_setaffinity(pid, {cpu_index})
            logging.debug("Pinned PID %d to CPU %d via sched_setaffinity", pid, cpu_index)
            return
        except Exception as exc:  # noqa: BLE001
            logging.warning("sched_setaffinity failed for CPU %d: %s", cpu_index, exc)

    try:
        import psutil  # type: ignore

        try:
            psutil.Process(pid).cpu_affinity([cpu_index])
            logging.debug("Pinned PID %d to CPU %d via psutil", pid, cpu_index)
            return
        except Exception as exc:  # noqa: BLE001
            logging.warning("psutil cpu_affinity failed for CPU %d: %s", cpu_index, exc)
    except ImportError:
        pass

    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            mask = 1 << cpu_index
            handle = kernel32.GetCurrentProcess()
            if kernel32.SetProcessAffinityMask(handle, mask) == 0:
                raise ctypes.WinError()
            logging.debug(
                "Pinned PID %d to CPU %d via SetProcessAffinityMask", pid, cpu_index
            )
            return
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "win32 SetProcessAffinityMask failed for CPU %d: %s", cpu_index, exc
            )
            return

    logging.warning("Could not set CPU affinity for PID %d; continuing without pinning.", pid)


def _release_cpu(cpu_queue, cpu_index: int) -> None:
    try:
        cpu_queue.put(cpu_index)
    except Exception:  # noqa: BLE001
        # Queue may already be closed during interpreter shutdown; ignore.
        pass


def worker_initializer(cpu_queue) -> None:
    cpu_index = cpu_queue.get()
    atexit.register(_release_cpu, cpu_queue, cpu_index)
    try:
        set_process_cpu_affinity(cpu_index)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to set CPU affinity for worker: %s", exc)
    else:
        logging.debug("Worker %s pinned to CPU %d", mp.current_process().name, cpu_index)


def monitor_directory(args: argparse.Namespace) -> None:
    input_dir: Path = args.input.expanduser().resolve()
    output_dir: Path = args.output.expanduser().resolve()

    if not input_dir.exists():
        logging.info("Creating input directory %s", input_dir)
        input_dir.mkdir(parents=True, exist_ok=True)

    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = mp.get_context("spawn")
    pending_paths: Set[Path] = set()
    completion_queue: Queue[Tuple[str, Path, Any]] = Queue()
    known_paths: Set[Path] = set()
    processed_count = 0

    max_images: Optional[int] = args.max_images

    logging.info(
        "Watching %s -> %s with %d worker(s) (max images: %s)",
        input_dir,
        output_dir,
        args.processes,
        "unlimited" if max_images is None else max_images,
    )

    logging.info(
        "Encoder configuration: quality=%d, effort=%d, decoding_speed=%d",
        args.quality,
        args.effort,
        args.decoding_speed,
    )

    if args.poll_interval != 1.0:
        logging.debug(
            "poll_interval argument is ignored when using watchdog-based monitoring."
        )

    available_cpus = get_available_cpu_indices()
    if args.cpu_affinity:
        missing = sorted(set(args.cpu_affinity) - set(available_cpus))
        if missing:
            raise ValueError(
                "Requested CPU indices %s are not available to this process." % missing
            )
        available_cpus = list(args.cpu_affinity)
        try:
            apply_process_cpu_affinity(available_cpus)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to apply CPU affinity %s: %s", available_cpus, exc)
        else:
            logging.info(
                "Restricting compression workers to CPUs: %s",
                ",".join(str(cpu) for cpu in available_cpus),
            )
    if len(available_cpus) < args.processes:
        raise ValueError(
            "Requested %d worker processes but only %d CPU cores are available for pinning."
            % (args.processes, len(available_cpus))
        )

    cpu_queue = ctx.Queue()
    for cpu_index in available_cpus[: args.processes]:
        cpu_queue.put(cpu_index)

    pool = ctx.Pool(
        processes=args.processes,
        initializer=worker_initializer,
        initargs=(cpu_queue,),
    )

    event_queue: Queue[Path] = Queue()
    handler = TiffEventHandler(event_queue)
    observer = Observer()
    observer.schedule(handler, str(input_dir), recursive=False)
    observer.start()

    initial_candidates = discover_candidates(
        input_dir,
        set(),
        args.stability_seconds,
    )
    for candidate in sorted(initial_candidates):
        event_queue.put(candidate)

    def _success_callback_factory(src_path: Path):
        def _callback(result: str) -> None:
            completion_queue.put(("success", src_path, result))

        return _callback

    def _error_callback_factory(src_path: Path):
        def _callback(exc: BaseException) -> None:
            completion_queue.put(("error", src_path, exc))

        return _callback

    try:
        while True:
            # Drain completed work first to keep the pipeline responsive.
            while True:
                try:
                    status, src_path, payload = completion_queue.get_nowait()
                except Empty:
                    break

                pending_paths.discard(src_path)
                known_paths.discard(src_path)

                if status == "success":
                    output_path = Path(str(payload))
                    processed_count += 1
                    logging.info("Compressed %s -> %s", src_path, output_path)
                else:
                    logging.error("Failed to compress %s: %s", src_path, payload)
                    if src_path.exists():
                        event_queue.put(src_path)

            if max_images is not None and processed_count >= max_images:
                if not pending_paths:
                    logging.info("Reached the requested number of images (%d).", processed_count)
                    break

            try:
                candidate = event_queue.get(timeout=0.5)
            except Empty:
                continue

            if candidate in known_paths:
                continue
            if not candidate.exists():
                continue
            if max_images is not None and processed_count + len(pending_paths) >= max_images:
                continue
            if not wait_for_file_stability(candidate, args.stability_seconds):
                continue

            success_cb = _success_callback_factory(candidate)
            error_cb = _error_callback_factory(candidate)
            pool.apply_async(
                compress_task,
                (
                    str(candidate),
                    str(output_dir),
                    args.quality,
                    args.effort,
                    args.decoding_speed,
                ),
                callback=success_cb,
                error_callback=error_cb,
            )
            pending_paths.add(candidate)
            known_paths.add(candidate)
            logging.debug("Queued %s for compression", candidate)
    except KeyboardInterrupt:
        logging.info("Interrupted by user; shutting down.")
    finally:
        # Ensure we do not leave dangling child processes.
        observer.stop()
        observer.join()
        pool.close()
        pool.join()
        try:
            cpu_queue.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            cpu_queue.join_thread()
        except Exception:  # noqa: BLE001
            pass


def install_signal_handlers() -> None:
    if sys.platform == "win32":
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    install_signal_handlers()
    try:
        monitor_directory(args)
    except Exception as exc:  # noqa: BLE001
        logging.error("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
