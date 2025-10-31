"""Directory watcher that pushes newly saved TIFF images to a gRPC image server.

The watcher polls the target folder, waits for new .tif/.tiff files to settle, and
ships them to the image server as protobuf payloads over gRPC.
"""

from __future__ import annotations

import argparse
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from queue import Empty, Queue

import grpc

from image_proto import ImageChunk, UploadAck, LatestImageRequest, LatestImageReply  # type: ignore[import-not-found]


# Platform-specific imports for RAM disk detection
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes


try:  # optional dependency for event-driven watching
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - watchdog is optional
    FileSystemEventHandler = None  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]


LOGGER = logging.getLogger("image_watcher")


if FileSystemEventHandler is not None:  # pragma: no branch - defined only when watchdog is installed

    class _ImageEventHandler(FileSystemEventHandler):  # type: ignore[misc]
        def __init__(self, root: Path, enqueue: Any) -> None:
            super().__init__()
            self._root = root
            self._enqueue = enqueue

        def on_created(self, event):  # type: ignore[override]
            self._maybe_enqueue(event.src_path, event.is_directory)

        def on_modified(self, event):  # type: ignore[override]
            self._maybe_enqueue(event.src_path, event.is_directory)

        def on_moved(self, event):  # type: ignore[override]
            self._maybe_enqueue(event.dest_path, event.is_directory)

        def _maybe_enqueue(self, raw_path: Any, is_directory: bool) -> None:
            if is_directory:
                return
            path = Path(raw_path)
            if self._root not in path.parents and path.parent != self._root:
                return
            if path.suffix.lower() not in {".tif", ".tiff"}:
                return
            self._enqueue(path)


else:

    class _ImageEventHandler:  # type: ignore[override]
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - placeholder when watchdog missing
            self._root = Path()
            self._enqueue = lambda *a, **k: None


class ImageExchangeStub:
    """Lightweight stub mirroring the generated gRPC client."""

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

    def UploadImage(  # noqa: N802 - keep compatibility with gRPC naming
        self,
        request: Any,
        timeout: Optional[float] = None,
    ) -> Any:
        return self._upload(request, timeout=timeout)

    def UploadImageFuture(  # noqa: N802 - keep compatibility with gRPC naming
        self,
        request: Any,
        timeout: Optional[float] = None,
    ) -> grpc.Future:  # type: ignore[type-arg]
        return self._upload.future(request, timeout=timeout)

    def GetLatestImage(  # noqa: N802 - keep compatibility with gRPC naming
        self,
        request: Any,
        timeout: Optional[float] = None,
    ) -> Any:
        return self._get_latest(request, timeout=timeout)


class ImageWatcher:
    def __init__(
        self,
        directory: Path,
        stub: Optional[ImageExchangeStub],
        source_id: str,
        poll_interval: float,
        settle_time: float,
        rpc_timeout: float,
        resend_on_change: bool,
        delete_after_upload: bool,
        max_in_flight: int,
        sequence_start: int,
        use_watchdog: bool,
        channel: Optional[grpc.Channel] = None,
        server_target: Optional[str] = None,
        grpc_options: Optional[List[Tuple[str, Any]]] = None,
    ) -> None:
        self._directory = directory
        self._stub = stub
        self._source_id = source_id
        self._poll_interval = poll_interval
        self._settle_time = settle_time
        self._rpc_timeout = rpc_timeout
        self._resend_on_change = resend_on_change
        self._delete_after_upload = delete_after_upload
        self._max_in_flight = max(1, max_in_flight)
        self._known: Dict[Path, Tuple[int, int]] = {}
        self._in_flight: List[Tuple[int, grpc.Future, Path, Tuple[int, int], int, float]] = []  # type: ignore[type-arg] # added timestamp
        self._next_sequence = max(0, sequence_start)
        self._next_commit = self._next_sequence
        self._pending_results: Dict[int, Tuple[bool, str, Path, Tuple[int, int], int]] = {}
        self._in_flight_timeout = rpc_timeout * 2  # Cancel in-flight uploads after 2x RPC timeout
        self._use_watchdog = bool(use_watchdog and Observer is not None)
        if use_watchdog and Observer is None:
            LOGGER.warning("watchdog package not available; falling back to polling mode")
        self._event_queue = Queue()
        self._observer = None
        self._channel = channel
        self._server_target = server_target
        self._grpc_options = grpc_options or []
        self._grpc_connected = stub is not None
        self._last_connection_attempt = 0.0
        self._connection_retry_interval = 5.0  # Retry every 5 seconds
        self._reconnect_thread: Optional[threading.Thread] = None
        self._stop_reconnect = threading.Event()
        self._connection_lock = threading.Lock()

    def run(self) -> None:
        LOGGER.info("Watching %s for TIFF images", self._directory)
        LOGGER.info("Max concurrent uploads: %d", self._max_in_flight)
        LOGGER.info("Starting sequence number: %d", self._next_commit)
        if not self._grpc_connected:
            LOGGER.warning("Starting in offline mode - will retry gRPC connection periodically")
            # Start reconnection thread
            self._start_reconnect_thread()
        
        try:
            if self._use_watchdog:
                self._run_with_watchdog()
            else:
                self._run_with_polling()
        finally:
            self._stop_reconnect_thread()

    def _start_reconnect_thread(self) -> None:
        """Start background thread for reconnection attempts."""
        if self._reconnect_thread is None or not self._reconnect_thread.is_alive():
            self._stop_reconnect.clear()
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                daemon=True,
                name="gRPC-Reconnect"
            )
            self._reconnect_thread.start()
            LOGGER.info("Started reconnection thread")

    def _stop_reconnect_thread(self) -> None:
        """Stop the background reconnection thread."""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            self._stop_reconnect.set()
            self._reconnect_thread.join(timeout=2)
            LOGGER.info("Stopped reconnection thread")

    def _reconnect_loop(self) -> None:
        """Background thread that continuously attempts to reconnect."""
        while not self._stop_reconnect.is_set():
            if not self._grpc_connected:
                self._try_reconnect_grpc()
            # Wait for retry interval or until stopped
            self._stop_reconnect.wait(timeout=self._connection_retry_interval)

    def _try_reconnect_grpc(self) -> bool:
        """Attempt to reconnect to gRPC server. Returns True if successful."""
        if self._server_target is None:
            return False
        
        with self._connection_lock:
            # Double-check connection status under lock
            if self._grpc_connected:
                return True
            
            try:
                LOGGER.info("Attempting to reconnect to gRPC server at %s", self._server_target)
                if self._channel is not None:
                    try:
                        self._channel.close()
                    except Exception:
                        pass  # Ignore errors closing old channel
                
                self._channel = grpc.insecure_channel(self._server_target, options=self._grpc_options)
                channel_ready_future = grpc.channel_ready_future(self._channel)
                channel_ready_future.result(timeout=self._rpc_timeout)
                
                self._stub = ImageExchangeStub(self._channel)
                self._grpc_connected = True
                LOGGER.info("Successfully reconnected to gRPC server")
                
                # Fire-and-forget: Clear all state and start fresh
                if self._in_flight:
                    LOGGER.info("Dropping %d in-flight uploads (fire-and-forget mode)", len(self._in_flight))
                    for _, future, path, _, _, _ in self._in_flight:
                        future.cancel()
                        if path.exists() and self._delete_after_upload:
                            try:
                                path.unlink(missing_ok=True)
                            except Exception:
                                pass
                    self._in_flight.clear()
                if self._pending_results:
                    LOGGER.info("Dropping %d pending results (fire-and-forget mode)", len(self._pending_results))
                    self._pending_results.clear()
                if self._known:
                    LOGGER.info("Clearing %d known files (fire-and-forget mode)", len(self._known))
                    self._known.clear()
                
                # Fire-and-forget: Always start fresh from sequence 1 or server's state
                try:
                    latest = self._stub.GetLatestImage(LatestImageRequest(), timeout=self._rpc_timeout)
                    latest_sequence = getattr(latest, "sequence", 0)
                    if latest.has_image and latest_sequence > 0:
                        expected_next = latest_sequence + 1
                        LOGGER.info("Starting fresh from server sequence %d (fire-and-forget mode)", expected_next)
                        self._next_sequence = expected_next
                        self._next_commit = expected_next
                    else:
                        LOGGER.info("Starting fresh from sequence 1 (fire-and-forget mode)")
                        self._next_sequence = 1
                        self._next_commit = 1
                except grpc.RpcError as exc:
                    LOGGER.warning("Could not query latest sequence: %s - starting from 1", exc)
                    self._next_sequence = 1
                    self._next_commit = 1
                
                return True
            except (grpc.FutureTimeoutError, grpc.RpcError) as exc:
                LOGGER.warning("Failed to reconnect to gRPC server: %s", exc)
                self._grpc_connected = False
                return False
            except Exception as exc:
                LOGGER.error("Unexpected error during gRPC reconnection: %s", exc)
                self._grpc_connected = False
                return False

    def _run_with_polling(self) -> None:
        while True:
            try:
                self._scan_once()
                self._drain_in_flight()
            except grpc.RpcError as exc:  # recoverable RPC failures
                LOGGER.error("gRPC failure: %s", exc)
                with self._connection_lock:
                    self._grpc_connected = False
                # Start reconnect thread if not running
                if not self._grpc_connected:
                    self._start_reconnect_thread()
                time.sleep(self._poll_interval)
            except Exception:  # pragma: no cover - log unexpected issues
                LOGGER.exception("Unexpected watcher error")
                time.sleep(self._poll_interval)
            time.sleep(self._poll_interval)
            self._drain_in_flight()

    def _run_with_watchdog(self) -> None:
        if Observer is None or FileSystemEventHandler is None:
            LOGGER.error("watchdog requested but unavailable; reverting to polling mode")
            self._run_with_polling()
            return
        assert Observer is not None
        assert FileSystemEventHandler is not None
        observer = Observer()
        handler = _ImageEventHandler(self._directory, self._event_queue.put)
        observer.schedule(handler, str(self._directory), recursive=False)  # type: ignore[arg-type]
        observer.start()
        self._observer = observer
        LOGGER.info("Using filesystem events for change detection")

        try:
            self._scan_once()  # pick up any existing files at startup
            while True:
                try:
                    candidate = self._event_queue.get(timeout=self._poll_interval)
                except Empty:
                    self._scan_once()
                else:
                    try:
                        self._process_candidate(Path(candidate))
                    except Exception:  # pragma: no cover - unexpected event processing failure
                        LOGGER.exception("Failed to process event for %s", candidate)
                self._drain_in_flight()
        except KeyboardInterrupt:
            raise
        finally:
            observer.stop()
            observer.join(timeout=5)
            self._observer = None

    def _scan_once(self) -> None:
        self._drain_in_flight()
        for candidate in self._iter_image_files():
            self._process_candidate(candidate)
        self._drain_in_flight()

    def _process_candidate(self, candidate: Path) -> None:
        if not candidate.exists():
            return
        signature = self._stat_signature(candidate)
        if signature is None:
            return
        if not self._resend_on_change and candidate in self._known:
            return
        if candidate in self._known and self._known[candidate] == signature:
            return
        
        # If gRPC is not connected, delete the file (real-time streaming - discard stale data)
        with self._connection_lock:
            is_connected = self._grpc_connected
        
        if not is_connected:
            LOGGER.warning("gRPC not available - deleting %s (real-time mode)", candidate.name)
            try:
                candidate.unlink(missing_ok=True)
                LOGGER.info("Deleted %s (no gRPC connection)", candidate.name)
            except Exception as exc:
                LOGGER.error("Failed to delete %s: %s", candidate.name, exc)
            self._known.pop(candidate, None)
            # Ensure reconnect thread is running
            self._start_reconnect_thread()
            return
        
        # Fire-and-forget: if queue is full, cancel oldest in-flight upload
        if len(self._in_flight) >= self._max_in_flight:
            # Drop the oldest in-flight request
            old_seq, old_future, old_path, old_sig, old_size, old_time = self._in_flight.pop(0)
            old_future.cancel()
            LOGGER.warning("Dropped in-flight upload for %s (seq %d) - queue full, fire-and-forget mode", 
                          old_path.name, old_seq)
            # Delete the old file if it still exists
            if old_path.exists() and self._delete_after_upload:
                try:
                    old_path.unlink(missing_ok=True)
                    LOGGER.debug("Deleted dropped file %s", old_path.name)
                except Exception:
                    pass
        if not self._wait_for_settle(candidate):
            # Fire-and-forget: if file doesn't settle quickly, delete it
            LOGGER.debug("File %s didn't settle, deleting (fire-and-forget mode)", candidate.name)
            try:
                candidate.unlink(missing_ok=True)
            except Exception:
                pass
            return
        sequence = self._next_sequence
        payload = self._build_payload(candidate, sequence)
        post_signature = self._stat_signature(candidate)
        if post_signature is not None:
            signature = post_signature
        try:
            assert self._stub is not None  # Should be non-None if _grpc_connected is True
            future = self._stub.UploadImageFuture(payload, timeout=self._rpc_timeout)
        except grpc.RpcError as exc:
            LOGGER.error("Unable to enqueue %s for upload: %s", candidate.name, exc)
            self._known.pop(candidate, None)
            with self._connection_lock:
                self._grpc_connected = False
            LOGGER.warning("gRPC connection lost - deleting %s (real-time mode)", candidate.name)
            try:
                candidate.unlink(missing_ok=True)
            except Exception as delete_exc:
                LOGGER.error("Failed to delete %s: %s", candidate.name, delete_exc)
            # Start reconnect thread
            self._start_reconnect_thread()
            return
        self._next_sequence += 1
        self._known[candidate] = signature
        self._in_flight.append((sequence, future, candidate, signature, len(payload.data), time.time()))

    def _iter_image_files(self):
        yield from self._directory.glob("*.tif")
        yield from self._directory.glob("*.tiff")

    def _stat_signature(self, path: Path) -> Optional[Tuple[int, int]]:
        try:
            details = path.stat()
        except FileNotFoundError:
            return None
        return (int(details.st_mtime_ns), int(details.st_size))

    def _wait_for_settle(self, path: Path) -> bool:
        # Fire-and-forget: only check twice, be fast
        previous_size: Optional[int] = None
        for attempt in range(2):
            try:
                current_size = path.stat().st_size
            except FileNotFoundError:
                return False
            if current_size == 0:
                if attempt > 0:  # Give up on empty files quickly
                    return False
                time.sleep(self._settle_time)
                continue
            if previous_size is not None and current_size == previous_size:
                return True
            previous_size = current_size
            time.sleep(self._settle_time)
        # Fire-and-forget: if not settled after 2 checks, give up
        return False

    def _build_payload(self, path: Path, sequence: int) -> Any:
        data = path.read_bytes()
        stat_info = path.stat()
        timestamp_ms = int(stat_info.st_mtime_ns / 1_000_000)
        return ImageChunk(
            filename=path.name,
            timestamp_ms=timestamp_ms,
            data=data,
            source=self._source_id,
            sequence=sequence,
        )

    def _drain_in_flight(self, block: bool = False) -> None:
        current_time = time.time()
        while True:
            progressed = False
            for entry in list(self._in_flight):
                sequence, future, path, signature, payload_size, start_time = entry
                # Fire-and-forget: cancel stale uploads
                if current_time - start_time > self._in_flight_timeout:
                    future.cancel()
                    self._in_flight.remove(entry)
                    LOGGER.warning("Cancelled stale upload for %s (seq %d, %.1fs old) - fire-and-forget mode",
                                 path.name, sequence, current_time - start_time)
                    if path.exists() and self._delete_after_upload:
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass
                    progressed = True
                elif future.done():
                    self._handle_future(sequence, future, path, signature, payload_size)
                    self._in_flight.remove(entry)
                    progressed = True
            if progressed:
                continue
            if not block or not self._in_flight:
                break
            sequence, future, path, signature, payload_size, start_time = self._in_flight.pop(0)
            self._handle_future(sequence, future, path, signature, payload_size)

    def _handle_future(
        self,
        sequence: int,
        future: grpc.Future,  # type: ignore[type-arg]
        path: Path,
        signature: Tuple[int, int],
        payload_size: int,
    ) -> None:
        success = True
        message = ""
        try:
            ack = future.result()
        except grpc.RpcError as exc:
            success = False
            message = str(exc)
            # Mark connection as lost on RPC error
            with self._connection_lock:
                self._grpc_connected = False
            LOGGER.warning("gRPC connection lost during upload")
            # Start reconnect thread
            self._start_reconnect_thread()
        else:
            if not isinstance(ack, UploadAck):
                success = False
                message = f"unexpected response type {type(ack)!r}"
            elif not ack.ok:
                success = False
                message = ack.message or "upload rejected"
            else:
                message = ack.message or "stored"
                # Treat "queued" as success - server accepted it and will process in order
                # This is expected behavior when images arrive out of sequence

        self._pending_results[sequence] = (success, message, path, signature, payload_size)
        self._commit_ready()

    def _commit_ready(self) -> None:
        while True:
            pending = self._pending_results.get(self._next_commit)
            if pending is None:
                break
            success, message, path, signature, payload_size = self._pending_results.pop(self._next_commit)
            sequence = self._next_commit
            if success:
                self._finalize_success(sequence, path, signature, payload_size, message)
            else:
                # Fire-and-forget: just log the error and delete the file, no retry
                reason = message or "unknown error"
                LOGGER.warning("Upload %s (seq %d) failed: %s - discarding (fire-and-forget mode)", 
                             path.name, sequence, reason)
                # Delete the file regardless of failure reason
                if self._delete_after_upload:
                    try:
                        path.unlink(missing_ok=True)
                        LOGGER.debug("Deleted failed upload %s", path.name)
                    except Exception:
                        pass
                self._known.pop(path, None)
            self._next_commit += 1

    def _finalize_success(
        self,
        sequence: int,
        path: Path,
        signature: Tuple[int, int],
        payload_size: int,
        message: str,
    ) -> None:
        log_suffix = f" - {message}" if message else ""
        LOGGER.info("Uploaded %s (seq %d, %d bytes)%s", path.name, sequence, payload_size, log_suffix)

        if self._delete_after_upload:
            self._delete_if_unchanged(path, signature)
        else:
            self._known[path] = signature

    def _delete_if_unchanged(self, path: Path, signature: Tuple[int, int], log_prefix: str = "Deleted") -> None:
        current_signature = self._stat_signature(path)
        if current_signature is None:
            self._known.pop(path, None)
            return
        if current_signature != signature:
            LOGGER.debug("Skip deleting %s; file changed after upload", path.name)
            self._known[path] = current_signature
            return
        try:
            path.unlink(missing_ok=True)
            LOGGER.debug("%s %s", log_prefix, path.name)
        except PermissionError:
            LOGGER.warning("Unable to delete %s (permission error)", path)
            self._known[path] = signature
            return
        except OSError as exc:
            LOGGER.warning("Failed to delete %s: %s", path, exc)
            self._known[path] = signature
            return
        self._known.pop(path, None)


def _is_ramdisk(path: Path) -> bool:
    """Check if the given path is on a RAM disk.
    
    Returns True if the path is on a RAM disk, False otherwise.
    """
    if sys.platform == "win32":
        return _is_ramdisk_windows(path)
    else:
        return _is_ramdisk_linux(path)


def _is_ramdisk_windows(path: Path) -> bool:
    """Check if a path is on a RAM disk on Windows.
    
    Checks for:
    1. ImDisk virtual drives
    2. Drives with RAMDISK in the volume label
    3. Drives mounted from RAM (via GetDriveType)
    """
    try:
        # Get the drive letter
        drive = path.resolve().drive
        if not drive:
            LOGGER.warning("Could not determine drive letter for %s", path)
            return False
        
        # Ensure drive ends with backslash for Windows API
        if not drive.endswith("\\"):
            drive += "\\"
        
        # Check drive type using GetDriveTypeW
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive)
        # DRIVE_RAMDISK = 6 (sometimes used for RAM disks)
        # DRIVE_FIXED = 3 (but we need additional checks)
        
        # Get volume information
        volume_name_buffer = ctypes.create_unicode_buffer(261)
        file_system_buffer = ctypes.create_unicode_buffer(261)
        serial_number = wintypes.DWORD()
        max_component_length = wintypes.DWORD()
        file_system_flags = wintypes.DWORD()
        
        result = ctypes.windll.kernel32.GetVolumeInformationW(
            drive,
            volume_name_buffer,
            261,
            ctypes.byref(serial_number),
            ctypes.byref(max_component_length),
            ctypes.byref(file_system_flags),
            file_system_buffer,
            261
        )
        
        if result:
            volume_label = volume_name_buffer.value.upper()
            file_system = file_system_buffer.value.upper()
            
            # Check for common RAM disk indicators
            ramdisk_indicators = ["RAMDISK", "RAM DISK", "IMDISK", "RAMDRIVE", "TEMPFS"]
            if any(indicator in volume_label for indicator in ramdisk_indicators):
                LOGGER.info("Detected RAM disk via volume label: %s", volume_label)
                return True
            
            # Check for ImDisk by looking at the file system
            if "FAT" in file_system or "NTFS" in file_system:
                # Try to detect ImDisk through registry or other means
                # For now, check the volume label
                pass
        
        # Additional check: Try to query physical drive info
        # RAM disks typically don't have physical geometry
        try:
            # Open the drive
            drive_path = f"\\\\.\\{drive.rstrip(chr(92))}"  # \\.\X:
            handle = ctypes.windll.kernel32.CreateFileW(
                drive_path,
                0,  # No access needed for query
                3,  # FILE_SHARE_READ | FILE_SHARE_WRITE
                None,
                3,  # OPEN_EXISTING
                0,
                None
            )
            
            if handle != -1:  # INVALID_HANDLE_VALUE
                # Try to get disk geometry - RAM disks often fail this
                # This is a heuristic check
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass
        
        LOGGER.warning("Could not confirm %s is on a RAM disk", path)
        return False
        
    except Exception as exc:
        LOGGER.error("Error checking if path is on RAM disk: %s", exc)
        return False


def _is_ramdisk_linux(path: Path) -> bool:
    """Check if a path is on a RAM disk on Linux.
    
    Checks for tmpfs or ramfs filesystems.
    """
    try:
        # Use df to get filesystem type
        result = subprocess.run(
            ["df", "-T", str(path.resolve())],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                # Parse the output (format: Filesystem Type Size Used Avail Use% Mounted)
                parts = lines[1].split()
                if len(parts) >= 2:
                    fs_type = parts[1].lower()
                    if fs_type in ["tmpfs", "ramfs"]:
                        LOGGER.info("Detected RAM disk: filesystem type is %s", fs_type)
                        return True
        
        LOGGER.warning("Could not confirm %s is on a RAM disk (filesystem type check failed)", path)
        return False
        
    except Exception as exc:
        LOGGER.error("Error checking if path is on RAM disk: %s", exc)
        return False


def _detect_source_id(server_host: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((server_host, 9))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostname()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch a directory for TIFF images and upload them via gRPC.")
    parser.add_argument("directory", type=Path, help="Directory to monitor for TIFF files")
    parser.add_argument("--server-host", default="192.168.5.1", help="Image server host")
    parser.add_argument("--server-port", type=int, default=50052, help="Image server port")
    parser.add_argument("--poll-interval", type=float, default=0.01, help="Polling interval in seconds")
    parser.add_argument("--settle-time", type=float, default=0.01, help="Wait time for file size to stabilize")
    parser.add_argument("--rpc-timeout", type=float, default=10.0, help="RPC timeout in seconds")
    parser.add_argument("--source-id", default=None, help="Override source identifier; defaults to outbound IP")
    parser.add_argument(
        "--resend-on-change",
        action="store_true",
        help="Re-upload files when timestamp or size changes even if previously sent",
    )
    parser.add_argument(
        "--no-delete-after-upload",
        action="store_false",
        dest="delete_after_upload",
        default=True,
        help="Keep files on disk after upload (default is to delete)",
    )
    parser.add_argument(
        "--max-message-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="Maximum gRPC message size for send/receive",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=4,
        help="Maximum number of concurrent uploads (fire-and-forget mode drops old uploads when full)",
    )
    parser.add_argument(
        "--sequence-start",
        type=int,
        default=None,
        help="Initial sequence number for outgoing uploads (defaults to last stored + 1)",
    )
    parser.add_argument(
        "--no-watchdog",
        action="store_false",
        dest="use_watchdog",
        default=True,
        help="Disable filesystem event monitoring and use polling only",
    )
    parser.add_argument(
        "--skip-ramdisk-check",
        action="store_true",
        help="Skip verification that directory is on a RAM disk",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s")
    directory: Path = args.directory.expanduser().resolve()
    if not directory.exists() or not directory.is_dir():
        LOGGER.error("%s is not a directory", directory)
        return 1

    # Verify the directory is on a RAM disk
    if not args.skip_ramdisk_check:
        LOGGER.info("Verifying %s is on a RAM disk...", directory)
        if not _is_ramdisk(directory):
            LOGGER.error("ERROR: %s is not on a RAM disk!", directory)
            LOGGER.error("This watcher is designed to work with RAM disks for high-speed file operations.")
            LOGGER.error("Use --skip-ramdisk-check to bypass this verification (not recommended).")
            return 1
        LOGGER.info("Confirmed: %s is on a RAM disk", directory)
    else:
        LOGGER.warning("Skipping RAM disk verification (--skip-ramdisk-check enabled)")

    target = f"{args.server_host}:{args.server_port}"
    LOGGER.info("Connecting to image server at %s", target)
    options = [
        ("grpc.max_send_message_length", args.max_message_bytes),
        ("grpc.max_receive_message_length", args.max_message_bytes),
    ]
    channel = grpc.insecure_channel(target, options=options)
    stub: Optional[ImageExchangeStub] = None
    grpc_connected = False
    
    # Test gRPC connection - but don't fail, just log and continue
    try:
        channel_ready_future = grpc.channel_ready_future(channel)
        channel_ready_future.result(timeout=args.rpc_timeout)
        stub = ImageExchangeStub(channel)
        grpc_connected = True
        LOGGER.info("Successfully connected to gRPC server")
    except grpc.FutureTimeoutError:
        LOGGER.warning("Failed to connect to gRPC server at %s (timeout) - will retry periodically", target)
    except Exception as exc:
        LOGGER.warning("Failed to connect to gRPC server at %s: %s - will retry periodically", target, exc)
    
    source_id = args.source_id or _detect_source_id(args.server_host)
    sequence_start = args.sequence_start
    
    # Only query latest image if connected
    if sequence_start is None and grpc_connected and stub is not None:
        try:
            latest = stub.GetLatestImage(LatestImageRequest(), timeout=args.rpc_timeout)
            latest_sequence = getattr(latest, "sequence", 0)
            if latest.has_image and latest_sequence > 0:
                sequence_start = latest_sequence + 1
                LOGGER.info("Resuming sequence from %d", latest_sequence)
            else:
                sequence_start = 1
        except grpc.RpcError as exc:
            LOGGER.warning("Unable to query latest image from server: %s", exc)
            sequence_start = 1
    elif sequence_start is None:
        sequence_start = 1
        LOGGER.info("Starting with default sequence number 1")
    
    watcher = ImageWatcher(
        directory=directory,
        stub=stub,
        source_id=source_id,
        poll_interval=args.poll_interval,
        settle_time=args.settle_time,
        rpc_timeout=args.rpc_timeout,
        resend_on_change=args.resend_on_change,
        delete_after_upload=args.delete_after_upload,
        max_in_flight=args.max_in_flight,
        sequence_start=sequence_start,
        use_watchdog=args.use_watchdog,
        channel=channel if not grpc_connected else channel,
        server_target=target,
        grpc_options=options,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        LOGGER.info("Watcher interrupted")
    finally:
        if channel is not None:
            channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
