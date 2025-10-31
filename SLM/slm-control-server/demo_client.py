"""Demo client for streaming tweezer commands to the hologram generator."""

import argparse
import asyncio
import random
import sys
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional, Sequence

import grpc
from google.protobuf import timestamp_pb2

import hologram_pb2 as holo_pb2
import hologram_pb2_grpc as holo_pb2_grpc


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_from_datetime(value: datetime) -> timestamp_pb2.Timestamp:
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(value.astimezone(timezone.utc))
    return ts


def _random_point(width: int, height: int) -> holo_pb2.TweezerPoint:
    return holo_pb2.TweezerPoint(
        x=random.uniform(0.0, float(width - 1)),
        y=random.uniform(0.0, float(height - 1)),
        z=0.0,
        intensity=random.uniform(0.2, 1.0),
    )


def _identity_affine(width: int, height: int) -> holo_pb2.AffineParameters:
    affine = holo_pb2.AffineParameters()

    cam_x0, cam_y0 = 0.0, 0.0
    cam_x1, cam_y1 = 0.0, float(max(height - 1, 1))
    cam_x2, cam_y2 = float(max(width - 1, 1)), 0.0

    slm_x0, slm_y0 = cam_x0, cam_y0
    slm_x1, slm_y1 = cam_x1, cam_y1
    slm_x2, slm_y2 = cam_x2, cam_y2

    affine.translate_x = slm_x0
    affine.translate_y = slm_y0
    affine.translate_z = cam_x0
    affine.rotate_x_deg = cam_y0

    affine.rotate_y_deg = slm_x1
    affine.rotate_z_deg = slm_y1
    affine.scale_x = cam_x1
    affine.scale_y = cam_y1

    affine.scale_z = slm_x2
    affine.shear_xy = slm_y2
    affine.shear_yz = cam_x2
    affine.shear_xz = cam_y2

    return affine


def _format_metrics(metrics: holo_pb2.Metrics) -> str:
    parts = []
    if metrics.generation_ms:
        parts.append(f"gen={metrics.generation_ms}ms")
    if metrics.driver_transfer_ms:
        parts.append(f"transfer={metrics.driver_transfer_ms}ms")
    if metrics.slm_update_ms:
        parts.append(f"slm={metrics.slm_update_ms}ms")
    if metrics.hologram_generated_at.seconds or metrics.hologram_generated_at.nanos:
        generated = metrics.hologram_generated_at.ToDatetime().astimezone(timezone.utc)
        parts.append(f"generated={generated.isoformat()}")
    if metrics.slm_ack_at.seconds or metrics.slm_ack_at.nanos:
        acked = metrics.slm_ack_at.ToDatetime().astimezone(timezone.utc)
        parts.append(f"ack={acked.isoformat()}")
    return " ".join(parts) if parts else "no-metrics"


async def _command_stream(interval: float, points_per_command: int, width: int, height: int) -> AsyncIterator[holo_pb2.TweezerCommand]:
    while True:
        command_id = uuid.uuid4().hex
        command = holo_pb2.TweezerCommand(command_id=command_id)
        command.requested_at.CopyFrom(_timestamp_from_datetime(_now_utc()))
        command.affine.CopyFrom(_identity_affine(width, height))
        command.points.extend(_random_point(width, height) for _ in range(points_per_command))
        yield command
        await asyncio.sleep(interval)


async def run(target: str, interval: float, points_per_command: int, width: int, height: int) -> None:
    async with grpc.aio.insecure_channel(target) as channel:
        stub = holo_pb2_grpc.ControlServiceStub(channel)
        call = stub.StreamCommands(_command_stream(interval, points_per_command, width, height))
        async for acknowledgement in call:
            metrics_summary = _format_metrics(acknowledgement.metrics)
            print(
                f"{acknowledgement.stage:<9} {acknowledgement.command_id} "
                f"{acknowledgement.detail or '-'} | {metrics_summary}"
            )


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Demo client for the hologram generator service")
    parser.add_argument("--target", default="192.168.6.1:50052", help="Generator service address")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between commands")
    parser.add_argument("--points", type=int, default=5, help="Number of tweezer points per command")
    parser.add_argument("--width", type=int, default=1920, help="Calibration width in pixels")
    parser.add_argument("--height", type=int, default=1152, help="Calibration height in pixels")
    return parser.parse_args(list(argv) if argv is not None else sys.argv[1:])


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(run(args.target, args.interval, args.points, args.width, args.height))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
