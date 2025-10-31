"""Async gRPC service that streams hologram updates to the SLM hardware (old HW path preserved)."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import signal
import sys
import time
from ctypes import *
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

import grpc
import numpy as np
from google.protobuf import timestamp_pb2

import hologram_pb2 as holo_pb2
import hologram_pb2_grpc as holo_pb2_grpc

# -------------------------
# Logging (same style)
# -------------------------
ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs" / "AutoLogs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGER = logging.getLogger("slm.driver.service")
if not LOGGER.handlers:
    file_handler = logging.FileHandler(LOG_DIR / "slm_driver_metrics.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(console_handler)
    LOGGER.setLevel(logging.INFO)

# -------------------------
# Timestamp helpers
# -------------------------
def _timestamp_from_datetime(value: datetime) -> timestamp_pb2.Timestamp:
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(value.astimezone(timezone.utc))
    return ts

def _datetime_from_timestamp(value: timestamp_pb2.Timestamp) -> datetime:
    dt = value.ToDatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# -------------------------
# Global HW state (EXACT names)
# -------------------------
slm_lib = None  # type: Optional[CDLL]

bit_depth = c_uint(12)
num_boards_found = c_uint(0)
constructed_okay = c_uint(-1)
is_nematic_type = c_bool(True)
RAM_write_enable = c_bool(True)
use_GPU = c_bool(True)
max_transients = c_uint(20)
board_number = c_uint(1)
wait_For_Trigger = c_uint(0)
flip_immediate = c_uint(0)
timeout_ms = c_uint(5000)
fork = c_uint(0)
RGB = c_uint(0)

# Output pulse settings
OutputPulseImageFlip = c_uint(0)
OutputPulseImageRefresh = c_uint(0)

is_hologram_generator_initialized = 0

# Dimensions & pixel size (EXACT names)
height = 0
width = 0
depth = 0
Bytes = 1

# Exposed write function (same signature/behavior as old code)
def write_image(image_array: np.ndarray) -> None:
    """Identical to old code: pointer from NumPy, length = height*width*Bytes."""
    global slm_lib, height, width, Bytes
    # Sanity: enforce 8-bit incoming
    if image_array.dtype != np.uint8:
        raise ValueError("Hologram payload must be 8-bit (uint8)")
    if image_array.size != height * width * Bytes:
        raise ValueError(
            f"Image buffer size {image_array.size} does not match {height*width*Bytes} bytes"
        )
    retVal = slm_lib.Write_image(
        board_number,
        image_array.ctypes.data_as(POINTER(c_ubyte)),
        height * width * Bytes,
        wait_For_Trigger,
        flip_immediate,
        OutputPulseImageFlip,
        OutputPulseImageRefresh,
        timeout_ms,
    )
    if retVal == -1:
        LOGGER.error("DMA Failed")
        slm_lib.Delete_SDK()
        raise RuntimeError("DMA Failed")

# -------------------------
# Performance metrics tracking
# -------------------------
class PerformanceMetrics:
    def __init__(self):
        self.images_received = 0
        self.images_written = 0
        self.last_report_time = time.perf_counter()
        self.report_interval = 1.0  # Report every second
        
    def record_image_received(self):
        self.images_received += 1
        
    def record_image_written(self):
        self.images_written += 1
        
    def check_and_report(self):
        now = time.perf_counter()
        elapsed = now - self.last_report_time
        if elapsed >= self.report_interval:
            images_per_sec = self.images_received / elapsed
            writes_per_sec = self.images_written / elapsed
            LOGGER.info(
                "Performance: %.2f images/sec received, %.2f Write_image/sec",
                images_per_sec,
                writes_per_sec
            )
            # Reset counters
            self.images_received = 0
            self.images_written = 0
            self.last_report_time = now

# Global metrics instance
perf_metrics = PerformanceMetrics()

# -------------------------
# gRPC Service (uses write_image exactly)
# -------------------------
class SlmHardwareService(holo_pb2_grpc.DriverServiceServicer):
    async def PushHolograms(
        self,
        request_iterator: AsyncIterator["holo_pb2.HologramFrame"],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator["holo_pb2.UpdateConfirmation"]:
        async for frame in request_iterator:
            perf_metrics.record_image_received()
            start = time.perf_counter()
            # Check dims match current hardware
            w = frame.width or width
            h = frame.height or height
            if w != width or h != height:
                raise ValueError(f"Incoming hologram {w}x{h} != SLM {width}x{height}")
            expected = width * height * Bytes
            payload = frame.hologram or b"\x00" * expected
            if len(payload) != expected:
                raise ValueError(f"Payload size {len(payload)} != expected {expected} for 8-bit image")

            arr = np.frombuffer(payload, dtype=np.uint8, count=expected)
            if not arr.flags.c_contiguous:
                arr = np.ascontiguousarray(arr)

            # EXACT same write path as old code
            write_image(arr)
            perf_metrics.record_image_written()
            perf_metrics.check_and_report()

            apply_ms = max(int((time.perf_counter() - start) * 1000), 0)
            # Minimal metrics fill (compatible with your proto)
            metrics = holo_pb2.Metrics()
            metrics.CopyFrom(frame.metrics)
            metrics.slm_update_ms = apply_ms
            metrics.slm_ack_at.CopyFrom(_timestamp_from_datetime(datetime.now(timezone.utc)))

            conf = holo_pb2.UpdateConfirmation(
                command_id=frame.command_id,
                status="UPDATED",
                detail="SLM applied hologram",
            )
            conf.metrics.CopyFrom(metrics)

            # LOGGER.info(
            #     "command=%s slm_ms=%s",
            #     frame.command_id,
            #     metrics.slm_update_ms if metrics.slm_update_ms else "-",
            # )
            yield conf

# -------------------------
# Server wrapper
# -------------------------
async def serve(bind: str) -> None:
    server = grpc.aio.server()
    holo_pb2_grpc.add_DriverServiceServicer_to_server(SlmHardwareService(), server)
    server.add_insecure_port(bind)
    await server.start()

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)
    except NotImplementedError:
        LOGGER.info("Signal handlers unavailable; rely on Ctrl+C for shutdown")

    try:
        await stop_event.wait()
    finally:
        await server.stop(grace=None)

# -------------------------
# CLI
# -------------------------
def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SLM hardware streaming server (old HW path).")
    p.add_argument("--bind", default="192.168.6.2:50051", help="Bind address (default: %(default)s)")
    p.add_argument("--lut-path", default=r"C:\Program Files\Meadowlark Optics\Blink OverDrive Plus\LUT Files\slm6336_at1064_PCIe.LUT",
                   help="Path to LUT file (default: %(default)s)")
    p.add_argument("--self-test", type=int, default=0, metavar="N",
                   help="If >0, send N random 8-bit frames via the exact old write path and exit.")
    return p.parse_args(list(argv))

# -------------------------
# MAIN — EXACT HW INIT FLOW
# -------------------------
if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    # Load the DLLs EXACTLY as old code
    if os.name == "nt":
        cdll.LoadLibrary(r"C:\Program Files\Meadowlark Optics\Blink OverDrive Plus\SDK\Blink_C_wrapper.dll")
        slm_lib = CDLL(r"C:\Program Files\Meadowlark Optics\Blink OverDrive Plus\SDK\Blink_C_wrapper.dll")

        # Set up function prototypes (EXACT)
        slm_lib.Create_SDK.argtypes = [c_uint, POINTER(c_uint), POINTER(c_uint), c_bool, c_bool, c_bool, c_uint, c_uint]
        slm_lib.Create_SDK.restype = c_void_p

        slm_lib.Load_LUT_file.argtypes = [c_uint, c_char_p]
        slm_lib.Load_LUT_file.restype = c_int

        slm_lib.Get_image_height.argtypes = [c_uint]
        slm_lib.Get_image_height.restype = c_uint

        slm_lib.Get_image_width.argtypes = [c_uint]
        slm_lib.Get_image_width.restype = c_uint

        slm_lib.Get_image_depth.argtypes = [c_uint]
        slm_lib.Get_image_depth.restype = c_uint

        slm_lib.Write_image.argtypes = [c_uint, POINTER(c_ubyte), c_uint, c_uint, c_uint, c_uint, c_uint, c_uint]
        slm_lib.Write_image.restype = c_int

        slm_lib.ImageWriteComplete.argtypes = [c_uint, c_uint]
        slm_lib.ImageWriteComplete.restype = c_int

        slm_lib.Delete_SDK.argtypes = []
        slm_lib.Delete_SDK.restype = c_void_p

        # Initialize SDK (EXACT)
        slm_lib.Create_SDK(
            bit_depth,
            byref(num_boards_found),
            byref(constructed_okay),
            is_nematic_type,
            RAM_write_enable,
            use_GPU,
            max_transients,
            0,
        )

        # Signal handling for cleanup (EXACT spirit)
        def cleanup(signum, frame):
            LOGGER.info("Exit command. Cleaning up resources...")
            try:
                slm_lib.Delete_SDK()
            finally:
                os._exit(0)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT, signal.SIGSEGV, signal.SIGILL):
            signal.signal(sig, cleanup)

        if constructed_okay.value == 0:
            LOGGER.error("Blink SDK did not construct successfully")
            sys.exit(1)

        if num_boards_found.value < 1:
            LOGGER.error("No SLM controller found")
            sys.exit(1)

        LOGGER.info("Blink SDK was successfully constructed")
        LOGGER.info("Found %s SLM controller(s)", num_boards_found.value)

        # Dimensions (EXACT: pass board_number c_uint)
        height = int(slm_lib.Get_image_height(board_number))
        width = int(slm_lib.Get_image_width(board_number))
        depth = int(slm_lib.Get_image_depth(board_number))
        Bytes = depth // 8
        LOGGER.info("Image width: %s, Image height: %s, Image depth: %s, Bytes per pixel: %s", width, height, depth, Bytes)

        # Load LUT file (EXACT)
        lut_file = args.lut_path.encode("utf-8")
        _lut_res = slm_lib.Load_LUT_file(board_number, lut_file)

        # Initialize image arrays (EXACT)
        WFC = np.zeros([width * height * Bytes], np.uint8, "C")

        # Write a blank pattern to the SLM (EXACT)
        def write_image(image_array: np.ndarray) -> None:  # rebind to close over current globals
            global slm_lib, height, width, Bytes
            if image_array.dtype != np.uint8:
                raise ValueError("Hologram payload must be 8-bit (uint8)")
            if image_array.size != height * width * Bytes:
                raise ValueError(
                    f"Image buffer size {image_array.size} does not match {height*width*Bytes} bytes"
                )
            retVal = slm_lib.Write_image(
                board_number,
                image_array.ctypes.data_as(POINTER(c_ubyte)),
                height * width * Bytes,
                wait_For_Trigger,
                flip_immediate,
                OutputPulseImageFlip,
                OutputPulseImageRefresh,
                timeout_ms,
            )
            if retVal == -1:
                LOGGER.error("DMA Failed")
                slm_lib.Delete_SDK()
                sys.exit(1)

        # First blank write (as old code)
        write_image(WFC)
    else:
        LOGGER.warning("Non-Windows platform detected; running without real SLM SDK.")

    # -------------------------
    # SELF-TEST mode
    # -------------------------
    if args.self_test > 0:
        if slm_lib is None:
            LOGGER.warning("Self-test in SIMULATION mode (non-Windows).")
            for i in range(args.self_test):
                time.sleep(0.01)
                LOGGER.info("[SIM] Frame %d applied", i)
            sys.exit(0)

        LOGGER.info("Starting self-test: sending %d random 8-bit frames of size %dx%d", args.self_test, width, height)
        for i in range(args.self_test):
            arr = np.random.randint(0, 256, size=width * height * Bytes, dtype=np.uint8)
            t0 = time.perf_counter()
            write_image(arr)  # EXACT same call as old flow
            dt_ms = (time.perf_counter() - t0) * 1000.0
            LOGGER.info("Frame %d applied in %.3f ms", i, dt_ms)

        # Clean shutdown
        if slm_lib is not None:
            try:
                slm_lib.Delete_SDK()
            finally:
                LOGGER.info("SLM SDK shutdown complete")
        sys.exit(0)

    # -------------------------
    # Server mode
    # -------------------------
    try:
        if slm_lib is None:
            LOGGER.warning("Running gRPC server without real SLM SDK (simulation delay only).")
        asyncio.run(serve(args.bind))
    except KeyboardInterrupt:
        pass
    finally:
        if slm_lib is not None:
            try:
                slm_lib.Delete_SDK()
            finally:
                LOGGER.info("SLM SDK shutdown complete")
