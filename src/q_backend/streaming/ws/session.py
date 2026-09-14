"""One bounded, asynchronous Redis-stream session per WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

import redis.asyncio
from redis.exceptions import RedisError, ResponseError
from starlette.websockets import WebSocket

from q_backend.streaming.codec import routing_key_string
from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key, topic_epoch_key
from q_backend.streaming.ws.frames import FrameError, binary_frame, control_frame, parse_client_frame, text_frame
from q_backend.streaming.ws.queue import OfferResult, QueuedEntry, TopicQueue
from q_contracts.topics import TOPICS

QUEUE_CAPACITY: Mapping[str, int] = {
    "quotes": 64,
    "bars.forming": 256,
    "bars.completed": 256,
}
DEFAULT_QUEUE_CAPACITY = 1024
# Control frames are few per topic (lagging notices are deduplicated), so this is
# only reached by a client that keeps sending requests while never reading.
CONTROL_CAPACITY = 256
XREAD_BLOCK_MS = 1000
XREAD_COUNT = 256
FINAL_SEND_TIMEOUT_S = 1.0
CLOSE_POLICY_VIOLATION = 1008
CLOSE_INTERNAL_ERROR = 1011
# An epoch the session has not learned yet: an idle durable stream, or an
# ephemeral topic whose epoch was lost with Redis. The first entry's epoch is
# adopted without a notice, because the client was never told a different one.
UNKNOWN_EPOCH = ""
_UNSET = object()
logger = logging.getLogger(__name__)


def _capacity(topic: str) -> int:
    return QUEUE_CAPACITY.get(topic, DEFAULT_QUEUE_CAPACITY)


def _id(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _field(mapping: Mapping[Any, Any], name: str) -> Any:
    return mapping.get(name.encode(), mapping.get(name))


def _stream_id_before(left: str, right: str) -> bool:
    """Compare Redis stream IDs numerically, not lexicographically."""
    try:
        return tuple(map(int, left.split("-", 1))) < tuple(map(int, right.split("-", 1)))
    except ValueError:
        return False


class StreamSession:
    def __init__(self, websocket: WebSocket, redis: redis.asyncio.Redis, clock: Any = None) -> None:
        self.websocket = websocket
        self.redis = redis
        self.clock = clock
        self.queues: dict[str, TopicQueue] = {}
        self.cursors: dict[str, str] = {}
        self.epochs: dict[str, str] = {}
        self.last_seq: dict[str, int | None] = {}
        self.generations: dict[str, int] = {}
        self.suspended: set[str] = set()
        self._stream_epoch: str | None | object = _UNSET
        self._controls: deque[dict[str, Any]] = deque()
        self._pending_lag: set[str] = set()
        self._wake_writer = asyncio.Event()
        self._wake_reader = asyncio.Event()
        self._done = asyncio.Event()
        self._close_code: int | None = None
        self._final_frame: dict[str, Any] | None = None

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._receive_loop()),
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._write_loop()),
            asyncio.create_task(self._done.wait()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks[:3]:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    logger.error("stream session task failed", exc_info=task.exception())
                    self._shutdown(CLOSE_INTERNAL_ERROR)
        finally:
            # Shielded so an outer cancellation (the server tearing the connection
            # down) still lets the tasks finish and the Redis pool close, and so the
            # cancellation that propagates is the caller's own rather than a copy.
            await asyncio.shield(self._close(tasks))

    async def _close(self, tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)
        if self._close_code is not None:
            # The writer is cancelled first so this is the only sender. A stalled
            # client may never accept the frame, so the send is bounded.
            try:
                await asyncio.wait_for(self._send_final(), FINAL_SEND_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - the connection is ending either way
                logger.debug("stream session final frame not delivered", exc_info=True)
        await self.redis.aclose()

    def _shutdown(self, code: int | None, final_frame: dict[str, Any] | None = None) -> None:
        if self._done.is_set():
            return
        self._close_code = code
        self._final_frame = final_frame
        self._done.set()

    async def _send_final(self) -> None:
        if self._final_frame is not None:
            await self.websocket.send_text(text_frame(self._final_frame))
        await self.websocket.close(code=self._close_code)

    def _control(self, frame: dict[str, Any]) -> None:
        if len(self._controls) >= CONTROL_CAPACITY:
            self._shutdown(CLOSE_POLICY_VIOLATION)
            return
        self._controls.append(frame)
        self._wake_writer.set()

    def _lagging(self, topic: str, from_seq: int) -> None:
        # One unsent notice per topic. A later overflow while it is still pending
        # starts at a higher sequence, so the pending notice already covers it.
        if topic in self._pending_lag:
            return
        self._pending_lag.add(topic)
        self._control(control_frame("lagging", topic=topic, from_seq=from_seq))

    def _stream_unavailable(self) -> None:
        self._shutdown(CLOSE_INTERNAL_ERROR, control_frame("rejected", reason="stream_unavailable"))

    async def _receive_loop(self) -> None:
        while not self._done.is_set():
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                self._shutdown(None)
                return
            try:
                if message.get("text") is None:
                    raise FrameError("client frames must be text")
                frame = parse_client_frame(message["text"])
            except FrameError as exc:
                self._control(control_frame("rejected", reason="invalid_frame", detail=str(exc)))
                continue
            try:
                await self._subscribe(frame.topics, frame.cursors or {})
            except RedisError:
                self._stream_unavailable()
                return

    async def _subscribe(self, topics: list[str], cursors: Mapping[str, str]) -> None:
        if self._stream_epoch is _UNSET:
            self._stream_epoch = await self._read_stream_epoch()
        accepted: dict[str, dict[str, Any]] = {}
        rejected: list[str] = []
        for topic in dict.fromkeys(topics):
            if topic not in TOPICS:
                rejected.append(topic)
                continue
            requested = cursors.get(topic)
            if requested is None and topic in self.queues and topic not in self.suspended:
                # Already forwarding: acknowledge the current position rather than
                # replacing the queue, which would drop entries waiting in it.
                accepted[topic] = {
                    "cursor": self.cursors[topic],
                    "epoch": self.epochs[topic],
                    "last_seq": self.last_seq[topic] or 0,
                }
                continue
            start, epoch, last_seq, expired = await self._subscription_state(topic, requested)
            if expired:
                self._control(control_frame("cursor_expired", topic=topic, cursor=requested))
                continue
            self.queues[topic] = TopicQueue(topic, TOPICS[topic], _capacity(topic))
            self.cursors[topic] = start
            self.epochs[topic] = epoch
            # With no cursor, forwarding starts after the entry carrying last_seq, so
            # a relay re-append of that entry is already known to the client.
            self.last_seq[topic] = last_seq if requested is None and last_seq else None
            self.generations[topic] = self.generations.get(topic, 0) + 1
            self.suspended.discard(topic)
            accepted[topic] = {"cursor": start, "epoch": epoch, "last_seq": last_seq}
        if accepted:
            self._control(control_frame("subscribed", topics=accepted))
            self._wake_reader.set()
        for topic in rejected:
            self._control(control_frame("rejected", reason="unknown_topic", topic=topic))

    async def _topic_epoch(self, topic: str) -> str:
        """The epoch Redis holds for an ephemeral topic; durable epochs live only in entries."""
        if TOPICS[topic].topic_class != "ephemeral":
            return UNKNOWN_EPOCH
        raw = await self.redis.get(topic_epoch_key(topic))
        return _id(raw) if raw is not None else UNKNOWN_EPOCH

    async def _read_stream_epoch(self) -> str | None:
        raw = await self.redis.get(STREAM_EPOCH_KEY)
        return _id(raw) if raw is not None else None

    async def _subscription_state(self, topic: str, requested: str | None) -> tuple[str, str, int, bool]:
        try:
            info = await self.redis.xinfo_stream(stream_key(topic))
        except ResponseError:
            # No stream yet. `$` is evaluated when XREAD begins, so it would skip an
            # entry appended between the acknowledgement and the reader's first call.
            return "0-0", await self._topic_epoch(topic), 0, requested is not None
        first_entry = _field(info, "first-entry")
        last_entry = _field(info, "last-entry")
        last_generated = _id(_field(info, "last-generated-id"))
        if last_entry:
            header = json.loads(_field(last_entry[1], "h"))
            epoch, last_seq = str(header.get("epoch", UNKNOWN_EPOCH)), int(header.get("seq", 0))
        else:
            epoch, last_seq = await self._topic_epoch(topic), 0
        if requested is None:
            return last_generated, epoch, last_seq, False
        # A cursor below the first retained entry means entries between them were trimmed.
        oldest = _id(first_entry[0]) if first_entry else last_generated
        expired = _stream_id_before(requested, oldest) if first_entry else _stream_id_before(requested, last_generated)
        return requested, epoch, last_seq, expired

    async def _read_loop(self) -> None:
        while not self._done.is_set():
            streams = {
                stream_key(topic): cursor for topic, cursor in self.cursors.items() if topic not in self.suspended
            }
            if not streams:
                self._wake_reader.clear()
                await self._wake_reader.wait()
                continue
            generations = dict(self.generations)
            try:
                result = await self.redis.xread(streams, count=XREAD_COUNT, block=XREAD_BLOCK_MS)
                await self._watch_epochs()
            except RedisError:
                self._stream_unavailable()
                return
            for key, entries in result or []:
                await self._offer_batch(_id(key).removeprefix("q:stream:"), entries, generations)

    async def _offer_batch(
        self, topic: str, entries: Iterable[tuple[bytes, Mapping[bytes, bytes]]], generations: Mapping[str, int]
    ) -> None:
        # A subscribe that reset this topic while the read was in flight owns the
        # cursor now; entries read from the old cursor would duplicate its range.
        if topic not in self.queues or generations.get(topic) != self.generations.get(topic):
            return
        for entry_id, fields in entries:
            await self._offer_entry(topic, entry_id, fields)

    async def _offer_entry(self, topic: str, entry_id: bytes, fields: Mapping[bytes, bytes]) -> None:
        if topic in self.suspended:
            return
        header = json.loads(fields[b"h"])
        epoch = str(header["epoch"])
        seq = int(header["seq"])
        queue = self.queues[topic]
        known = self.epochs.get(topic, UNKNOWN_EPOCH)
        if known == UNKNOWN_EPOCH:
            self.epochs[topic] = epoch
        elif epoch != known:
            # Entries of the old epoch still queued cannot be placed after the notice.
            queue.clear()
            self.last_seq[topic] = None
            self._control(control_frame("epoch_changed", topic=topic, new_epoch=epoch, previous_epoch=known))
            self.epochs[topic] = epoch
        self.cursors[topic] = _id(entry_id)
        last = self.last_seq.get(topic)
        if last is not None and seq <= last:
            return

        if header["payload_kind"] == "arrow_ipc":
            frame: str | bytes = binary_frame(header, fields[b"p"])
        else:
            envelope = dict(header)
            envelope["payload"] = json.loads(fields[b"p"])
            frame = text_frame(envelope)
        key = header.get("key")
        policy = TOPICS[topic]
        routing_key = routing_key_string(topic, key) if key and policy.coalesce_key else None
        entry = QueuedEntry(entry_id, seq, epoch, routing_key, frame)

        if queue.offer(entry) is OfferResult.OVERFLOW:
            if policy.on_overflow == "coalesce":
                # The key limit is reached. Existing keys keep their latest entry;
                # the new key's entry is dropped and named.
                self._lagging(topic, seq)
            else:
                from_seq = queue.oldest_seq()
                queue.clear()
                self._lagging(topic, from_seq if from_seq is not None else seq)
                if policy.topic_class == "durable":
                    # Durable topics are replayable: the client re-snapshots, so
                    # forwarding stops until it subscribes again.
                    self.suspended.add(topic)
                    return
                # Non-durable topics have no snapshot; continue from the newest entry.
                queue.offer(entry)
        self.last_seq[topic] = seq
        self._wake_writer.set()

    async def _watch_epochs(self) -> None:
        stream_epoch = await self._read_stream_epoch()
        previous = self._stream_epoch
        self._stream_epoch = stream_epoch
        # A known epoch that disappeared or was replaced means Redis lost its data.
        # A missing epoch that appears is the relay re-establishing one after that
        # loss, which was already announced (or preceded every subscription).
        if previous is _UNSET or previous is None or stream_epoch == previous:
            return
        for topic in list(self.queues):
            new_epoch = await self._topic_epoch(topic)
            self.queues[topic].clear()
            self.last_seq[topic] = None
            self._control(
                control_frame(
                    "epoch_changed",
                    topic=topic,
                    new_epoch=new_epoch,
                    previous_epoch=self.epochs.get(topic) or None,
                )
            )
            self.epochs[topic] = new_epoch

    async def _write_loop(self) -> None:
        while not self._done.is_set():
            if self._controls:
                control = self._controls.popleft()
                if control["type"] == "lagging":
                    self._pending_lag.discard(control["topic"])
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
                self._wake_writer.clear()
                if self._controls or any(len(queue) for queue in self.queues.values()):
                    continue
                await self._wake_writer.wait()
