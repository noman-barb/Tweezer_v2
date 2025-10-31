"""Hologram generator gRPC service bridging control clients and the SLM driver."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Sequence, Tuple, cast

import grpc
from grpc.aio import StreamStreamCall
from google.protobuf import timestamp_pb2

import hologram_pb2 as holo_pb2
import hologram_pb2_grpc as holo_pb2_grpc

try:
    import cupy as cp
except ImportError as exc:
    raise RuntimeError("cupy is required for the hologram generator service") from exc

# Force GPU index 1 (RTX A4000) and print info
GPU_INDEX = 0
cp.cuda.Device(GPU_INDEX).use()
dev = cp.cuda.Device(GPU_INDEX)
print(f"[GPU SELECTED] Using device {GPU_INDEX}: {cp.cuda.runtime.getDeviceProperties(GPU_INDEX)['name'].decode()}")


from cupyx.scipy.fft import get_fft_plan


ENABLE_MIXED_PRECISION = False
REAL_DTYPE = cp.float16 if ENABLE_MIXED_PRECISION else cp.float32
COMPLEX_DTYPE = cp.complex64


ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs" / "AutoLogs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

METRICS_LOGGER = logging.getLogger("slm.generator.metrics")
if not METRICS_LOGGER.handlers:
    file_handler = logging.FileHandler(LOG_DIR / "slm_generator_metrics.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    METRICS_LOGGER.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    METRICS_LOGGER.addHandler(console_handler)
    METRICS_LOGGER.setLevel(logging.INFO)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_from_datetime(value: datetime) -> timestamp_pb2.Timestamp:
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(value.astimezone(timezone.utc))
    return ts


def _timedelta_to_ms(value: timedelta) -> int:
    return max(int(value.total_seconds() * 1000), 0)


@dataclass
class GeneratorConfig:
    width: int = 512
    height: int = 512
    iterations: int = 50


cuda_code = r"""
extern "C" __global__
void update_phase(const float* amplitude, const float* fft_real, const float* fft_imag,
                  float* output_phase, int rows, int cols) {
    int i = blockIdx.y * blockDim.y + threadIdx.y;
    int j = blockIdx.x * blockDim.x + threadIdx.x;

    if (i < rows && j < cols) {
        int idx = i * cols + j;

        // Calculate phase from real and imaginary parts
        float phase = atan2f(fft_imag[idx], fft_real[idx]);
        output_phase[idx] = phase;
    }
}

extern "C" __global__
void add_spots_kernel(float* hologram, const int* x_idx, const int* y_idx,
                      const float* intensities, int n_spots, int width, int height) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n_spots) {
        int x = x_idx[i];
        int y = y_idx[i];
        if (x >= 0 && x < width && y >= 0 && y < height) {
            atomicAdd(&hologram[y * width + x], intensities[i]);
        }
    }
}
"""


amplitude: Optional[cp.ndarray] = None
fft_real: Optional[cp.ndarray] = None
fft_imag: Optional[cp.ndarray] = None
output_phase: Optional[cp.ndarray] = None
update_phase_kernel: Optional[cp.RawKernel] = None
add_spots_kernel: Optional[cp.RawKernel] = None
threads_per_block: Tuple[int, int] = (0, 0)
blocks_per_grid_x: int = 0
blocks_per_grid_y: int = 0
blocks_per_grid: Tuple[int, int] = (0, 0)
fft_field: Optional[cp.ndarray] = None
fft_plan: Optional[Any] = None
field: Optional[cp.ndarray] = None
phase: Optional[cp.ndarray] = None
exp_phase: Optional[cp.ndarray] = None  # Pre-allocated buffer for exp(1j*phase)


def gerchberg_saxton_cupy(target_intensity, iterations):
    # Initialize amplitude and phase

    global amplitude, fft_real, fft_imag, output_phase, update_phase_kernel, add_spots_kernel, threads_per_block, blocks_per_grid_x, blocks_per_grid_y, blocks_per_grid, fft_field, fft_plan, field, phase, exp_phase

    real_dtype = REAL_DTYPE
    target_intensity = target_intensity.astype(real_dtype, copy=False)

    amp_shape = target_intensity.shape

    needs_init = (
        amplitude is None
        or amplitude.shape != amp_shape
        or amplitude.dtype != real_dtype
    )

    if needs_init:
        amplitude = cp.empty(amp_shape, dtype=real_dtype)
        phase = (cp.random.rand(*amp_shape) * 2 * cp.pi).astype(cp.float32)

        # Allocate space for FFT components and output phase
        fft_real = cp.zeros(amp_shape, dtype=real_dtype)
        fft_imag = cp.zeros(amp_shape, dtype=real_dtype)
        output_phase = cp.zeros(amp_shape, dtype=cp.float32)

        # Compile CUDA kernels
        update_phase_kernel = cp.RawKernel(cuda_code, "update_phase")
        add_spots_kernel = cp.RawKernel(cuda_code, "add_spots_kernel")

        # Define CUDA grid and block sizes
        threads_per_block = (16, 16)
        blocks_per_grid_x = (amp_shape[1] + threads_per_block[0] - 1) // threads_per_block[0]
        blocks_per_grid_y = (amp_shape[0] + threads_per_block[1] - 1) // threads_per_block[1]

        blocks_per_grid = (blocks_per_grid_x, blocks_per_grid_y)

        fft_field = cp.zeros(amp_shape, dtype=COMPLEX_DTYPE)
        # Create FFT plan
        fft_plan = get_fft_plan(fft_field, axes=(0, 1))

        field = cp.zeros(amp_shape, dtype=COMPLEX_DTYPE)
        exp_phase = cp.zeros(amp_shape, dtype=COMPLEX_DTYPE)  # Pre-allocate exp buffer


    assert amplitude is not None
    assert phase is not None
    assert fft_field is not None
    assert fft_plan is not None
    assert field is not None

    amp = cast(cp.ndarray, amplitude)
    ph = cast(cp.ndarray, phase)
    fft = cast(cp.ndarray, fft_field)
    fld = cast(cp.ndarray, field)
    plan = fft_plan

    # Compute amplitude once (not every iteration)
    cp.sqrt(target_intensity, out=amp)
    
    # Warm-start: only randomize on first call, reuse phase from previous iterations
    # This provides much faster convergence for sequential holograms
    # Uncomment next line if you want random start each time:
    # cp.random.rand(*amp.shape, out=ph); ph *= 2 * cp.pi
    
    for _ in range(iterations):
        # Perform FFT - optimized with in-place operations
        # Compute field = amp * exp(1j * phase)
        cp.exp(1j * ph, out=fld)
        fld *= amp

        # FFT using pre-computed plan (fft is overwritten in-place by reference)
        with plan:
            fft[:] = cp.fft.fft2(fld)

        # Use arctan2 instead of angle() - faster and in-place
        cp.arctan2(fft.imag, fft.real, out=ph)

    phase = ph
    return ph


class HologramGeneratorCupy:
    def __init__(self):
        self.width = 0
        self.height = 0
        self.depth = 0
        self.iterations = 0
        self.rgb = False
        self.affine_params = None
        self.stream = cp.cuda.Stream(non_blocking=True)
        self.hologram_buffer = None  # Pre-allocated hologram buffer
        self.add_spots_kernel = None  # CUDA kernel for fast spot placement
        self.gs_input_buffer = None  # Optional mixed-precision staging buffer

    def Initialize_HologramGenerator(self, width, height, depth, iterations, RGB):
        self.width = width
        self.height = height
        self.depth = depth
        self.iterations = iterations
        self.rgb = bool(RGB)
        
        # Pre-allocate hologram buffer
        self.hologram_buffer = cp.zeros((height, width), dtype=cp.float32)

        if ENABLE_MIXED_PRECISION:
            self.gs_input_buffer = cp.empty((height, width), dtype=REAL_DTYPE)
        else:
            self.gs_input_buffer = None
        
        # Compile CUDA kernel for fast spot placement
        self.add_spots_kernel = cp.RawKernel(cuda_code, "add_spots_kernel")

    def CalculateAffinePolynomials(self,
                                CAM_X_0, CAM_Y_0, SLM_X_0, SLM_Y_0,
                                CAM_X_1, CAM_Y_1, SLM_X_1, SLM_Y_1,
                                CAM_X_2, CAM_Y_2, SLM_X_2, SLM_Y_2):
        with self.stream:
            A = cp.array([
                [CAM_X_0, CAM_Y_0, 1],
                [CAM_X_1, CAM_Y_1, 1],
                [CAM_X_2, CAM_Y_2, 1]
            ], dtype=cp.float32)
            Bx = cp.array([SLM_X_0, SLM_X_1, SLM_X_2], dtype=cp.float32)
            By = cp.array([SLM_Y_0, SLM_Y_1, SLM_Y_2], dtype=cp.float32)

            affine_x = cp.linalg.solve(A, Bx)
            affine_y = cp.linalg.solve(A, By)
            self.affine_params = (affine_x, affine_y)

    def Generate_Hologram(self, WFC, x_spots, y_spots, z_spots, I_spots, N_spots, ApplyAffine):
        with self.stream:
            if ApplyAffine and self.affine_params:
                affine_x, affine_y = self.affine_params
                x = affine_x[0] * x_spots + affine_x[1] * y_spots + affine_x[2]
                y = affine_y[0] * x_spots + affine_y[1] * y_spots + affine_y[2]
            else:
                x = x_spots
                y = y_spots

            x_idx = cp.clip(x, 0, self.width - 1).astype(cp.int32)
            y_idx = cp.clip(y, 0, self.height - 1).astype(cp.int32)

            # Use pre-allocated buffer and zero it out
            hologram = self.hologram_buffer
            assert hologram is not None
            hologram.fill(0.0)
            
            # Use custom CUDA kernel for fast spot placement (much faster than cp.add.at)
            n_spots_int = int(N_spots)
            if n_spots_int > 0:
                threads = 256
                blocks = (n_spots_int + threads - 1) // threads
                kernel = self.add_spots_kernel
                if kernel is None:
                    raise RuntimeError("CUDA kernel not initialized")
                kernel(
                    (blocks,), (threads,),
                    (hologram, x_idx, y_idx, I_spots, n_spots_int, self.width, self.height)
                )

            if WFC is not None:
                hologram += WFC
            # Optimized normalization
            max_val = cp.max(hologram)
            if float(max_val) > 0.0:
                hologram *= (1.0 / max_val)

            if ENABLE_MIXED_PRECISION and self.gs_input_buffer is not None:
                gs_input = self.gs_input_buffer
                gs_input[...] = hologram
            else:
                gs_input = hologram

            gs_hologram = gerchberg_saxton_cupy(gs_input, iterations=self.iterations)
            return gs_hologram


class GsEngine:
    """Gerchberg–Saxton engine accelerated with CuPy (legacy layout preserved)."""

    def __init__(self, config: GeneratorConfig):
        self._config = config
        self._generator = HologramGeneratorCupy()
        self._initialize_generator()
        self._warmed = False

    def update_config(self, config: GeneratorConfig) -> None:
        self._config = config
        self._initialize_generator()
        self._warmed = False

    def generate(self, command: holo_pb2.TweezerCommand) -> bytes:
        points = command.points
        if not points:
            return b"\x00" * (self._config.width * self._config.height)

        apply_affine = self._configure_legacy_affine(command.affine)

        with self._generator.stream:
            x = cp.asarray([p.x for p in points], dtype=cp.float32)
            y = cp.asarray([p.y for p in points], dtype=cp.float32)
            z = cp.asarray([p.z for p in points], dtype=cp.float32)
            intensities = cp.asarray([max(p.intensity, 0.0) for p in points], dtype=cp.float32)

            if cp.allclose(intensities, 0.0):
                intensities.fill(1.0)

            # Legacy path expects pixel-space coordinates and applies the three-point affine if requested.
            hologram = self._generator.Generate_Hologram(
                WFC=None,
                x_spots=x,
                y_spots=y,
                z_spots=z,
                I_spots=intensities,
                N_spots=len(points),
                ApplyAffine=apply_affine,
            )

            hologram_min = cp.min(hologram)
            hologram_max = cp.max(hologram)
            
            # Optimized normalization and conversion
            diff = hologram_max - hologram_min                                                   
            if float(diff) > 1e-9:
                # In-place operations for maximum speed
                hologram -= hologram_min
                hologram *= (255.0 / diff)
                cp.clip(hologram, 0, 255, out=hologram)
                image = cp.rint(hologram).astype(cp.uint8)
            else:
                image = cp.zeros(hologram.shape, dtype=cp.uint8)
            
            # Flip image horizontally to correct direction mapping
            image = cp.fliplr(image)
            
            return image.tobytes()

    def warmup(self) -> None:
        if self._warmed:
            return

        dummy_command = holo_pb2.TweezerCommand()
        dummy_point = dummy_command.points.add()
        dummy_point.x = 0.0
        dummy_point.y = 0.0
        dummy_point.z = 0.0
        dummy_point.intensity = 1.0

        _ = self.generate(dummy_command)
        self._warmed = True

    def _initialize_generator(self) -> None:
        self._generator.Initialize_HologramGenerator(
            self._config.width,
            self._config.height,
            depth=8,
            iterations=self._config.iterations,
            RGB=0,
        )
    def _configure_legacy_affine(self, affine: holo_pb2.AffineParameters) -> bool:
        # Map extended AffineParameters fields back into the legacy three-point calibration.
        # Fields are repurposed as follows:
        #   (SLM_X_i, SLM_Y_i) -> translate_x/y, rotate_y_deg/rotate_z_deg, scale_z/shear_xy
        #   (CAM_X_i, CAM_Y_i) -> translate_z/rotate_x_deg, scale_x/scale_y, shear_yz/shear_xz
        values = (
            affine.translate_x,
            affine.translate_y,
            affine.translate_z,
            affine.rotate_x_deg,
            affine.rotate_y_deg,
            affine.rotate_z_deg,
            affine.scale_x,
            affine.scale_y,
            affine.scale_z,
            affine.shear_xy,
            affine.shear_yz,
            affine.shear_xz,
        )

        if not any(math.isfinite(value) and abs(value) > 1e-9 for value in values):
            return False

        try:
            self._generator.CalculateAffinePolynomials(
                CAM_X_0=affine.translate_z,
                CAM_Y_0=affine.rotate_x_deg,
                SLM_X_0=affine.translate_x,
                SLM_Y_0=affine.translate_y,
                CAM_X_1=affine.scale_x,
                CAM_Y_1=affine.scale_y,
                SLM_X_1=affine.rotate_y_deg,
                SLM_Y_1=affine.rotate_z_deg,
                CAM_X_2=affine.shear_yz,
                CAM_Y_2=affine.shear_xz,
                SLM_X_2=affine.scale_z,
                SLM_Y_2=affine.shear_xy,
            )
        except (cp.linalg.LinAlgError, ValueError):
            METRICS_LOGGER.warning("affine calibration is singular; skipping legacy mapping")
            return False

        return True


class SlmStreamClient:
    def __init__(self, target: str, fire_and_forget: bool = True):
        self._target = target
        self._fire_and_forget = fire_and_forget
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[holo_pb2_grpc.DriverServiceStub] = None
        self._call: Optional[StreamStreamCall] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending: Dict[str, asyncio.Future] = {}
        self._acks_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Lock()

    async def connect(self) -> None:
        async with self._connected:
            if self._channel is not None:
                return
            self._channel = grpc.aio.insecure_channel(self._target)
            self._stub = holo_pb2_grpc.DriverServiceStub(self._channel)
            assert self._stub is not None
            self._call = self._stub.PushHolograms(self._frame_iterator())
            self._acks_task = asyncio.create_task(self._consume_acks())

    async def close(self) -> None:
        if self._channel is None:
            return
        await self._queue.put(None)
        if self._acks_task is not None:
            self._acks_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._acks_task
        await self._channel.close()
        self._channel = None

    async def send_frame(
        self, frame: holo_pb2.HologramFrame
    ) -> Optional[holo_pb2.UpdateConfirmation]:
        if self._channel is None:
            await self.connect()
        sent_at = _timestamp_from_datetime(_now_utc())
        frame.metrics.hologram_sent_at.CopyFrom(sent_at)

        if self._fire_and_forget:
            await self._queue.put(frame)
            return None

        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future = loop.create_future()
        await self._queue.put((frame, ack_future))
        return await ack_future

    async def _frame_iterator(self) -> AsyncIterator[holo_pb2.HologramFrame]:
        while True:
            item = await self._queue.get()
            if item is None:
                break

            if self._fire_and_forget:
                yield item
                continue

            frame, future = item
            self._pending[frame.command_id] = future
            yield frame

    async def _consume_acks(self) -> None:
        assert self._call is not None
        try:
            async for confirmation in self._call:
                if self._fire_and_forget:
                    METRICS_LOGGER.debug(
                        "command=%s stage=acknowledged slm_ms=%s",
                        confirmation.command_id,
                        confirmation.metrics.slm_update_ms
                        if confirmation.metrics.slm_update_ms
                        else "-",
                    )
                    continue

                future = self._pending.pop(confirmation.command_id, None)
                if future is not None and not future.done():
                    future.set_result(confirmation)
        except grpc.aio.AioRpcError as exc:
            if not self._fire_and_forget:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(exc)
                self._pending.clear()
            METRICS_LOGGER.error("SLM stream failed: %s", exc)
            raise
            raise


class HologramGeneratorService(holo_pb2_grpc.ControlServiceServicer):
    def __init__(self, config: GeneratorConfig, slm_client: SlmStreamClient, engine: GsEngine):
        self._config = config
        self._slm_client = slm_client
        self._engine = engine

    async def StreamCommands(
        self, request_iterator: AsyncIterator[holo_pb2.TweezerCommand], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[holo_pb2.CommandAcknowledge]:
        async for command in request_iterator:
            command_id = command.command_id or uuid.uuid4().hex
            accepted = holo_pb2.CommandAcknowledge(
                command_id=command_id,
                stage="ACCEPTED",
                detail="Command accepted",
            )
            yield accepted

            try:
                generation_start = _now_utc()
                loop = asyncio.get_running_loop()
                hologram = await loop.run_in_executor(None, self._engine.generate, command)
                generation_end = _now_utc()
                generation_ms = _timedelta_to_ms(generation_end - generation_start)

                METRICS_LOGGER.info(
                    "command=%s stage=generated generation_ms=%s",
                    command_id,
                    generation_ms,
                )

                metrics = holo_pb2.Metrics(generation_ms=generation_ms)
                metrics.hologram_generated_at.CopyFrom(_timestamp_from_datetime(generation_end))

                frame = holo_pb2.HologramFrame(
                    command_id=command_id,
                    hologram=hologram,
                    width=self._config.width,
                    height=self._config.height,
                )
                frame.affine.CopyFrom(command.affine)
                frame.metrics.CopyFrom(metrics)

                confirmation = await self._slm_client.send_frame(frame)

                # If fire-and-forget, we're done, acknowledge as "SENT"
                if confirmation is None:
                    sent_ack = holo_pb2.CommandAcknowledge(
                        command_id=command_id,
                        stage="SENT",
                        detail="Hologram sent to SLM driver",
                    )
                    sent_ack.metrics.CopyFrom(metrics)
                    yield sent_ack
                    continue

                # Otherwise, wait for completion and report full round-trip
                ack_received_at = _now_utc()
                completed = holo_pb2.CommandAcknowledge(
                    command_id=command_id,
                    stage="COMPLETED",
                    detail=confirmation.detail or confirmation.status or "SLM updated",
                )
                completed.metrics.CopyFrom(confirmation.metrics)

                round_trip_ms = _timedelta_to_ms(ack_received_at - generation_start)

                METRICS_LOGGER.info(
                    "command=%s stage=completed transfer_ms=%s slm_ms=%s round_trip_ms=%s",
                    command_id,
                    confirmation.metrics.driver_transfer_ms
                    if confirmation.metrics.driver_transfer_ms
                    else "-",
                    confirmation.metrics.slm_update_ms if confirmation.metrics.slm_update_ms else "-",
                    round_trip_ms,
                )
                yield completed
            except Exception as exc:  # broad except to keep stream alive
                METRICS_LOGGER.exception("command=%s stage=error", command_id)
                error_ack = holo_pb2.CommandAcknowledge(
                    command_id=command_id,
                    stage="ERROR",
                    detail=str(exc),
                )
                yield error_ack


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GS algorithm hologram generator")
    parser.add_argument(
        "--bind",
        default="192.168.6.1:50052",
        help="Address to expose for upstream clients (default: %(default)s)",
    )
    parser.add_argument(
        "--slm-target",
        default="192.168.6.2:50051",
        help="SLM hardware service address (default: %(default)s)",
    )
    parser.add_argument(
        "--fire-and-forget",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable fire-and-forget streaming to the SLM driver",
    )
    parser.add_argument("--width", type=int, default=1920, help="Output hologram width in pixels")
    parser.add_argument("--height", type=int, default=1152, help="Output hologram height in pixels")
    parser.add_argument("--iterations", type=int, default=50, help="Gerchberg–Saxton iterations")
    return parser.parse_args(list(argv) if argv is not None else sys.argv[1:])


async def serve(bind: str, slm_target: str, config: GeneratorConfig, fire_and_forget: bool) -> None:
    slm_client = SlmStreamClient(slm_target, fire_and_forget=fire_and_forget)
    await slm_client.connect()

    engine = GsEngine(config)
    loop = asyncio.get_running_loop()
    METRICS_LOGGER.info("warming up GS engine for %sx%s", config.width, config.height)
    await loop.run_in_executor(None, engine.warmup)

    server = grpc.aio.server()
    service = HologramGeneratorService(config, slm_client, engine)
    holo_pb2_grpc.add_ControlServiceServicer_to_server(service, server)
    server.add_insecure_port(bind)
    await server.start()

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        await stop_event.wait()
    finally:
        await server.stop(grace=None)
        await slm_client.close()


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    config = GeneratorConfig(width=args.width, height=args.height, iterations=args.iterations)
    try:
        asyncio.run(serve(args.bind, args.slm_target, config, args.fire_and_forget))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
