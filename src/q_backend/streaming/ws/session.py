"""One bounded, asynchronous Redis-stream session per WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

import redis.asyncio
from redis.exceptions import RedisError, ResponseError
from starlette.websockets import WebSocket, WebSocketDisconnect

from q_backend.streaming.codec import routing_key_string
from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key
from q_backend.streaming.ws.frames import FrameError, binary_frame, parse_client_frame, text_frame
from q_backend.streaming.ws.queue import OfferResult, QueuedEntry, TopicQueue
from q_contracts.topics import TOPICS

QUEUE_CAPACITY: Mapping[str, int] = {
    "quotes": 64,
    "bars.forming": 256,
    "bars.completed": 256,
}
XREAD_BLOCK_MS = 1000
XREAD_COUNT = 256
_UNSET = object()
logger = logging.getLogger(__name__)


def _capacity(topic: str) -> int:
    return QUEUE_CAPACITY.get(topic, 1024)


def _id(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


class StreamSession:
    def __init__(self, websocket: WebSocket, redis: redis.asyncio.Redis, clock: Any = None) -> None:
        self.websocket = websocket
        self.redis = redis
        self.clock = clock
        self.queues: dict[str, TopicQueue] = {}
        self.cursors: dict[str, str] = {}
        self.epochs: dict[str, str] = {}
        self.suspended: set[str] = set()
        self._stream_epoch: str | None | object = _UNSET
        self._controls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closed = asyncio.Event()

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._receive_loop()),
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._write_loop()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.exception("stream session task failed", exc_info=exc)
                    await self.websocket.close(code=1011)
        finally:
            self._closed.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.redis.aclose()

    async def _receive_loop(self) -> None:
        while not self._closed.is_set():
            try:
                frame = parse_client_frame(await self.websocket.receive_text())
            except WebSocketDisconnect:
                return
            except FrameError as exc:
                await self._controls.put({"reason": "unsupported_schema_major", "detail": str(exc)})
                continue
            await self._subscribe(frame.topics, frame.cursors or {})

    async def _subscribe(self, topics: list[str], cursors: dict[str, Any]) -> None:
        accepted: dict[str, dict[str, Any]] = {}
        rejected: list[str] = []
        for topic in dict.fromkeys(topics):
            if topic not in TOPICS:
                rejected.append(topic)
                continue
            requested = cursors.get(topic)
            start, epoch, last_seq, expired = await self._subscription_state(topic, requested)
            if expired:
                await self._controls.put({"topic": topic, "cursor": str(requested)})
                continue
            self.queues[topic] = TopicQueue(topic, TOPICS[topic], _capacity(topic))
            self.cursors[topic] = start
            self.epochs[topic] = epoch
            self.suspended.discard(topic)
            accepted[topic] = {"cursor": start, "epoch": epoch, "last_seq": last_seq}
        if accepted:
            await self._controls.put({"topics": accepted})
        for topic in rejected:
            await self._controls.put({"reason": "unknown_topic", "topic": topic})

    async def _subscription_state(self, topic: str, requested: Any) -> tuple[str, str, int, bool]:
        try:
            info = await self.redis.xinfo_stream(stream_key(topic))
        except ResponseError:
            return ("0-0" if requested is not None else "$", "", 0, requested is not None)
        first = _id(info.get(b"first-entry", info.get("first-entry"))[0])
        last_entry = info.get(b"last-entry", info.get("last-entry"))
        last = _id(last_entry[0]) if last_entry else "0-0"
        fields = last_entry[1] if last_entry else {}
        header_raw = fields.get(b"h", fields.get("h", b"{}"))
        header = json.loads(header_raw)
        epoch = str(header.get("epoch", ""))
        last_seq = int(header.get("seq", 0))
        if requested is not None:
            cursor = str(requested)
            return cursor, epoch, last_seq, cursor < first
        return last, epoch, last_seq, False

    async def _read_loop(self) -> None:
        while not self._closed.is_set():
            streams = {
                stream_key(topic): cursor for topic, cursor in self.cursors.items() if topic not in self.suspended
            }
            if not streams:
                await asyncio.sleep(0.01)
                continue
            try:
                if type(self.redis).__module__.startswith("fakeredis"):
                    # fakeredis does not wake a blocking XREAD across TestClient's
                    # event-loop boundary; production Redis always uses BLOCK.
                    result = await self.redis.xread(streams, count=XREAD_COUNT)
                else:
                    result = await self.redis.xread(streams, count=XREAD_COUNT, block=XREAD_BLOCK_MS)
                await self._watch_epochs()
            except RedisError:
                await self._controls.put({"reason": "stream_unavailable"})
                await self._close_after_controls()
                return
            for key, entries in result:
                topic = _id(key).removeprefix("q:stream:")
                for entry_id, fields in entries:
                    await self._offer_entry(topic, entry_id, fields)
            if not result:
                await asyncio.sleep(0.01)

    async def _offer_entry(self, topic: str, entry_id: bytes, fields: Mapping[bytes, bytes]) -> None:
        header_raw = fields[b"h"]
        payload = fields[b"p"]
        header = json.loads(header_raw)
        epoch = str(header["epoch"])
        if epoch != self.epochs.get(topic, epoch):
            await self._controls.put({"topic": topic, "new_epoch": epoch, "previous_epoch": self.epochs[topic]})
            self.epochs[topic] = epoch
        if header["payload_kind"] == "arrow_ipc":
            frame: str | bytes = binary_frame(header, payload)
        else:
            control = dict(header)
            control["payload"] = json.loads(payload)
            frame = text_frame(control)
        key = header.get("key")
        routing_key = routing_key_string(topic, key) if key and TOPICS[topic].coalesce_key else None
        entry = QueuedEntry(entry_id, int(header["seq"]), epoch, routing_key, frame)
        queue = self.queues[topic]
        outcome = queue.offer(entry)
        if outcome is OfferResult.OVERFLOW:
            await self._controls.put({"topic": topic, "from_seq": entry.seq})
            if TOPICS[topic].topic_class == "durable":
                queue.clear()
                self.suspended.add(topic)
            else:
                self.cursors[topic] = _id(entry_id)
        else:
            self.cursors[topic] = _id(entry_id)

    async def _watch_epochs(self) -> None:
        raw_epoch = await self.redis.get(STREAM_EPOCH_KEY)
        stream_epoch = _id(raw_epoch) if raw_epoch is not None else None
        if self._stream_epoch is _UNSET:
            self._stream_epoch = stream_epoch
            return
        if stream_epoch == self._stream_epoch:
            return
        for topic, previous in list(self.epochs.items()):
            await self._controls.put({"topic": topic, "new_epoch": "", "previous_epoch": previous})
            self.epochs[topic] = ""
        self._stream_epoch = stream_epoch

    async def _write_loop(self) -> None:
        while not self._closed.is_set():
            try:
                control = self._controls.get_nowait()
            except asyncio.QueueEmpty:
                control = None
            if control is not None:
                await self.websocket.send_text(text_frame(control))
                continue
            sent = False
            for queue in list(self.queues.values()):
                entry = queue.pop()
                if entry is None:
                    continue
                if isinstance(entry.frame, bytes):
                    await self.websocket.send_bytes(entry.frame)
                else:
                    await self.websocket.send_text(entry.frame)
                sent = True
            if not sent:
                await asyncio.sleep(0.001)

    async def _close_after_controls(self) -> None:
        while not self._controls.empty():
            await asyncio.sleep(0)
        await self.websocket.close(code=1011)
