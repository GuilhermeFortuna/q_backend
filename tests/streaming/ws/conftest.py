import asyncio
import json
import time
from typing import Any

import redis.asyncio

from q_backend.streaming.keys import stream_key

TEST_REDIS_URL = "redis://127.0.0.1:6380/15"


def async_redis() -> redis.asyncio.Redis:
    return redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False)


def envelope_fields(
    topic: str,
    seq: int,
    *,
    epoch: str = "epoch-1",
    key: dict[str, str] | None = None,
    payload: bytes = b'{"status":"ok"}',
    payload_kind: str = "control",
) -> dict[bytes, bytes]:
    header: dict[str, Any] = {
        "topic": topic,
        "schema_major": 1,
        "seq": seq,
        "epoch": epoch,
        "producer_id": "test",
        "origin_ts": "2026-09-14T00:00:00Z",
        "payload_kind": payload_kind,
        "payload_schema": "schema/stream/envelope.schema.json",
    }
    if key:
        header["key"] = key
    return {b"h": json.dumps(header).encode(), b"p": payload}


async def xadd(client, topic: str, seq: int, **kwargs: Any) -> str:
    return (await client.xadd(stream_key(topic), envelope_fields(topic, seq, **kwargs))).decode()


class FakeSocket:
    """A WebSocket whose sends can be stalled, standing in for a client that stops reading."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.frames: list[Any] = []
        self.arrivals: list[float] = []
        self.close_code: int | None = None
        self._open = asyncio.Event()
        self._open.set()

    def stall(self) -> None:
        self._open.clear()

    def resume(self) -> None:
        self._open.set()

    def subscribe(self, topics: list[str], cursors: dict[str, str] | None = None) -> None:
        frame: dict[str, Any] = {"topics": topics}
        if cursors is not None:
            frame["cursors"] = cursors
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_text(self, value: str) -> None:
        await self._open.wait()
        self.frames.append(json.loads(value))
        self.arrivals.append(time.monotonic())

    async def send_bytes(self, value: bytes) -> None:
        await self._open.wait()
        self.frames.append(value)
        self.arrivals.append(time.monotonic())

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    def controls(self, frame_type: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if isinstance(frame, dict) and frame.get("type") == frame_type]

    def entries(self, topic: str) -> list[dict[str, Any]]:
        return [
            frame
            for frame in self.frames
            if isinstance(frame, dict) and "type" not in frame and frame.get("topic") == topic
        ]


async def eventually(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)
