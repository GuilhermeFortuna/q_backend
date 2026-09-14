"""Measure WebSocket stream latency and API-process CPU for Q-014's live soak."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import struct
import time
from datetime import datetime

import websockets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, required=True)
    parser.add_argument("--topics", required=True, help="Comma-separated topic names")
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--url", default="ws://127.0.0.1:8000/api/v1/stream")
    parser.add_argument("--pid", type=int, default=os.getpid(), help="API PID to sample")
    return parser.parse_args()


def _cpu_ticks(pid: int) -> int:
    fields = open(f"/proc/{pid}/stat", encoding="utf-8").read().split()
    return int(fields[13]) + int(fields[14])


def _header(frame: str | bytes) -> dict[str, object] | None:
    if isinstance(frame, str):
        data = json.loads(frame)
        return data if "origin_ts" in data else None
    if len(frame) < 4:
        return None
    length = struct.unpack("<I", frame[:4])[0]
    return json.loads(frame[4 : 4 + length])


def _origin_seconds(header: dict[str, object]) -> float | None:
    value = header.get("origin_ts")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def consume(url: str, topics: list[str], stop_at: float, latencies_ms: list[float]) -> None:
    async with websockets.connect(url, compression=None) as socket:
        await socket.send(json.dumps({"topics": topics}))
        while time.monotonic() < stop_at:
            try:
                frame = await asyncio.wait_for(socket.recv(), timeout=min(1.0, stop_at - time.monotonic()))
            except TimeoutError:
                continue
            header = _header(frame)
            if header is None:
                continue
            origin = _origin_seconds(header)
            if origin is not None:
                latencies_ms.append((time.time() - origin) * 1000)


async def run(args: argparse.Namespace) -> None:
    topics = [topic for topic in args.topics.split(",") if topic]
    if args.clients < 1 or not topics or args.minutes <= 0:
        raise SystemExit("--clients must be positive, --topics non-empty, and --minutes positive")
    start_ticks = _cpu_ticks(args.pid)
    start = time.monotonic()
    stop_at = start + args.minutes * 60
    samples: list[list[float]] = [[] for _ in range(args.clients)]
    await asyncio.gather(*(consume(args.url, topics, stop_at, sample) for sample in samples))
    elapsed = time.monotonic() - start
    cpu_pct = ((_cpu_ticks(args.pid) - start_ticks) / os.sysconf("SC_CLK_TCK")) / elapsed * 100
    latencies = [value for sample in samples for value in sample]
    if latencies:
        quantiles = statistics.quantiles(latencies, n=100, method="inclusive")
        print(f"append-to-send latency: p50={statistics.median(latencies):.2f} ms p95={quantiles[94]:.2f} ms")
    else:
        print("append-to-send latency: no frames with origin_ts received")
    print(f"mean API CPU: {cpu_pct:.2f}%")


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
