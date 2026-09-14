"""Measure WebSocket stream latency and API-process CPU for Q-014's live soak.

Latency is append-to-receive: the append time is the millisecond part of the Redis
stream id, read by this script from the same streams the clients subscribe to, and
matched to each received frame by (topic, epoch, seq). The header's origin_ts is the
producer's clock, which would include the producer's own delay. On loopback,
receive time stands in for send time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import struct
import time

import redis.asyncio
import websockets

from q_backend.storage.settings import get_settings
from q_backend.streaming.keys import stream_key

PENDING_TTL_S = 60.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clients", type=int, required=True)
    parser.add_argument("--topics", required=True, help="Comma-separated topic names")
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--pid", type=int, required=True, help="PID of the API process whose CPU is sampled")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/api/v1/stream")
    parser.add_argument("--redis-url", default=None, help="Defaults to the backend settings' redis_url")
    return parser.parse_args()


def _cpu_ticks(pid: int) -> int:
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
        # The command name may contain spaces; fields after its closing parenthesis are fixed.
        fields = stat.read().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime, stime


def _header(frame: str | bytes) -> dict[str, object] | None:
    if isinstance(frame, str):
        data = json.loads(frame)
        return None if "type" in data else data
    length = struct.unpack("<I", frame[:4])[0]
    return json.loads(frame[4 : 4 + length])


class LatencyMatcher:
    """Pairs stream appends with frame receipts, whichever is observed first."""

    def __init__(self) -> None:
        self.appended: dict[tuple, tuple[float, float]] = {}
        self.received: dict[tuple, list[float]] = {}
        self.latencies_ms: list[float] = []

    def append(self, key: tuple, append_s: float) -> None:
        for receive_s in self.received.pop(key, []):
            self.latencies_ms.append((receive_s - append_s) * 1000)
        self.appended[key] = (append_s, time.monotonic())

    def receive(self, key: tuple, receive_s: float) -> None:
        if key in self.appended:
            self.latencies_ms.append((receive_s - self.appended[key][0]) * 1000)
        else:
            self.received.setdefault(key, []).append(receive_s)

    def prune(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL_S
        self.appended = {key: value for key, value in self.appended.items() if value[1] >= cutoff}


async def watch_appends(client: redis.asyncio.Redis, topics: list[str], stop_at: float, matcher: LatencyMatcher):
    cursors = {stream_key(topic): "$" for topic in topics}
    while time.monotonic() < stop_at:
        for key, entries in await client.xread(cursors, block=500) or []:
            for entry_id, fields in entries:
                header = json.loads(fields[b"h"])
                append_ms = int(entry_id.split(b"-", 1)[0])
                matcher.append((header["topic"], header["epoch"], header["seq"]), append_ms / 1000)
                cursors[key.decode()] = entry_id.decode()
        matcher.prune()


async def consume(url: str, topics: list[str], stop_at: float, matcher: LatencyMatcher) -> None:
    async with websockets.connect(url, compression=None) as socket:
        await socket.send(json.dumps({"topics": topics}))
        while time.monotonic() < stop_at:
            try:
                frame = await asyncio.wait_for(socket.recv(), timeout=max(0.01, min(1.0, stop_at - time.monotonic())))
            except TimeoutError:
                continue
            received_s = time.time()
            header = _header(frame)
            if header is not None:
                matcher.receive((header["topic"], header["epoch"], header["seq"]), received_s)


async def run(args: argparse.Namespace) -> None:
    topics = [topic for topic in args.topics.split(",") if topic]
    if args.clients < 1 or not topics or args.minutes <= 0:
        raise SystemExit("--clients must be positive, --topics non-empty, and --minutes positive")
    client = redis.asyncio.Redis.from_url(args.redis_url or get_settings().redis_url, decode_responses=False)
    matcher = LatencyMatcher()
    start_ticks = _cpu_ticks(args.pid)
    start = time.monotonic()
    stop_at = start + args.minutes * 60
    try:
        await asyncio.gather(
            watch_appends(client, topics, stop_at, matcher),
            *(consume(args.url, topics, stop_at, matcher) for _ in range(args.clients)),
        )
    finally:
        await client.aclose()
    elapsed = time.monotonic() - start
    cpu_pct = ((_cpu_ticks(args.pid) - start_ticks) / os.sysconf("SC_CLK_TCK")) / elapsed * 100
    latencies = matcher.latencies_ms
    if len(latencies) >= 2:
        p95 = statistics.quantiles(latencies, n=100, method="inclusive")[94]
        print(
            f"append-to-receive latency over {len(latencies)} frames: p50={statistics.median(latencies):.2f} ms p95={p95:.2f} ms"
        )
    else:
        print("append-to-receive latency: too few matched frames")
    print(f"mean API CPU (pid {args.pid}): {cpu_pct:.2f}% of one core")


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
