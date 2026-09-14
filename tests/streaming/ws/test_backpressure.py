"""Criteria 5-7: stalled clients stay bounded and do not delay a fast client.

A client that stops reading is modelled by a socket whose sends block, which is what
a full TCP window looks like to the session's writer.
"""

import asyncio
import json
import statistics
import struct

import pytest

from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key
from q_backend.streaming.ws import session as session_module
from q_backend.streaming.ws.queue import TopicQueue
from q_backend.streaming.ws.session import StreamSession
from tests.streaming.ws.conftest import FakeSocket, async_redis, envelope_fields, eventually

pytestmark = pytest.mark.integration

ENTRIES = 2000
FAST_CLIENT_P95_GAP_S = 0.05


@pytest.fixture
def max_queue_lengths(monkeypatch):
    lengths: dict[str, int] = {}
    offer = TopicQueue.offer

    def probe(queue, entry):
        result = offer(queue, entry)
        lengths[queue.topic] = max(lengths.get(queue.topic, 0), len(queue))
        return result

    monkeypatch.setattr(TopicQueue, "offer", probe)
    return lengths


async def publish(client, topic: str, count: int, *, key_for=lambda seq: None, payload_kind: str = "control") -> str:
    last_id = ""
    for start in range(1, count + 1, 100):
        pipe = client.pipeline(transaction=False)
        for seq in range(start, min(start + 100, count + 1)):
            payload = b"ipc-bytes" if payload_kind == "arrow_ipc" else b'{"status":"ok"}'
            fields = envelope_fields(topic, seq, key=key_for(seq), payload=payload, payload_kind=payload_kind)
            pipe.xadd(stream_key(topic), fields)
        last_id = (await pipe.execute())[-1].decode()
        await asyncio.sleep(0)
    return last_id


def binary_headers(socket: FakeSocket) -> list[dict]:
    headers = []
    for frame in socket.frames:
        if isinstance(frame, bytes):
            length = struct.unpack("<I", frame[:4])[0]
            headers.append(json.loads(frame[4 : 4 + length]))
    return headers


async def quiet(socket: FakeSocket, settle_s: float = 0.3) -> None:
    count = -1
    while count != len(socket.frames):
        count = len(socket.frames)
        await asyncio.sleep(settle_s)


async def start(client, topics: list[str], *, stalled: bool = False) -> tuple[FakeSocket, StreamSession, asyncio.Task]:
    socket = FakeSocket()
    session = StreamSession(socket, client)
    task = asyncio.create_task(session.run())
    socket.subscribe(topics)
    await eventually(lambda: socket.controls("subscribed"))
    if stalled:
        socket.stall()
    return socket, session, task


async def stop(socket: FakeSocket, task: asyncio.Task) -> None:
    socket.resume()
    socket.disconnect()
    await asyncio.wait_for(task, 5)


def run(scenario):
    async def wrapper():
        admin = async_redis()
        await admin.flushdb()
        await admin.set(STREAM_EPOCH_KEY, "stream-epoch-1")
        try:
            await scenario(admin)
        finally:
            await admin.flushdb()
            await admin.aclose()

    asyncio.run(wrapper())


def test_stalled_quotes_client_resumes_with_the_newest_entry_per_symbol(max_queue_lengths):
    """Criterion 5."""

    async def scenario(admin):
        socket, session, task = await start(async_redis(), ["quotes"], stalled=True)
        try:
            last_id = await publish(
                admin, "quotes", ENTRIES, key_for=lambda seq: {"symbol": "AB"[seq % 2]}, payload_kind="arrow_ipc"
            )
            await eventually(lambda: session.cursors["quotes"] == last_id)
            socket.resume()
            await quiet(socket)

            latest = {header["key"]["symbol"]: header["seq"] for header in binary_headers(socket)}
            assert latest == {"A": ENTRIES, "B": ENTRIES - 1}
            # At most the frame in flight when the client stalled, then one per symbol.
            assert len(binary_headers(socket)) <= 3
            assert max_queue_lengths["quotes"] <= 2
            assert not socket.controls("lagging")
        finally:
            await stop(socket, task)

    run(scenario)


def test_stalled_durable_client_is_told_where_its_view_ends_and_receives_nothing_after(max_queue_lengths):
    """Criterion 6."""

    async def scenario(admin):
        socket, session, task = await start(async_redis(), ["jobs.terminal"], stalled=True)
        try:
            await publish(admin, "jobs.terminal", ENTRIES)
            await eventually(lambda: "jobs.terminal" in session.suspended)
            socket.resume()
            await quiet(socket)

            delivered = [frame["seq"] for frame in socket.entries("jobs.terminal")]
            (lagging,) = socket.controls("lagging")
            assert delivered == list(range(1, len(delivered) + 1))
            assert lagging == {"type": "lagging", "topic": "jobs.terminal", "from_seq": len(delivered) + 1}
            assert max_queue_lengths["jobs.terminal"] <= session_module.DEFAULT_QUEUE_CAPACITY

            before = len(socket.frames)
            await publish(admin, "jobs.terminal", 10)
            await asyncio.sleep(2)
            assert len(socket.frames) == before
        finally:
            await stop(socket, task)

    run(scenario)


def test_fast_client_receives_everything_promptly_beside_stalled_clients():
    """Criterion 7."""

    async def scenario(admin):
        stalled_quotes = await start(async_redis(), ["quotes"], stalled=True)
        stalled_durable = await start(async_redis(), ["jobs.terminal"], stalled=True)
        fast_socket, _, fast_task = await start(async_redis(), ["jobs.terminal"])
        try:
            await asyncio.gather(
                publish(admin, "jobs.terminal", ENTRIES),
                publish(admin, "quotes", ENTRIES, key_for=lambda seq: {"symbol": "AB"[seq % 2]}),
            )
            await eventually(lambda: len(fast_socket.entries("jobs.terminal")) == ENTRIES, timeout=10)

            assert [frame["seq"] for frame in fast_socket.entries("jobs.terminal")] == list(range(1, ENTRIES + 1))
            arrivals = [
                at
                for frame, at in zip(fast_socket.frames, fast_socket.arrivals)
                if isinstance(frame, dict) and "seq" in frame
            ]
            gaps = [later - earlier for earlier, later in zip(arrivals, arrivals[1:])]
            p95_gap = statistics.quantiles(gaps, n=100, method="inclusive")[94]
            print(f"fast client p95 inter-arrival gap: {p95_gap * 1000:.3f} ms")
            assert p95_gap < FAST_CLIENT_P95_GAP_S
        finally:
            for socket, _, task in (stalled_quotes, stalled_durable, (fast_socket, None, fast_task)):
                await stop(socket, task)

    run(scenario)
