import asyncio
import json

import pytest
import redis.asyncio

from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key
from q_backend.streaming.ws.session import StreamSession

TEST_REDIS_URL = "redis://127.0.0.1:6380/15"


class _Socket:
    async def send_text(self, _value):
        return None

    async def send_bytes(self, _value):
        return None


async def _publish_progress(client, seq: int, topic: str = "jobs.progress") -> str:
    header = {
        "topic": topic,
        "schema_major": 1,
        "seq": seq,
        "epoch": "epoch-1",
        "producer_id": "test",
        "origin_ts": "2026-09-14T00:00:00Z",
        "payload_kind": "control",
        "payload_schema": "schema/stream/envelope.schema.json",
    }
    if topic == "jobs.progress":
        header["key"] = {"kind": "backtest", "job_id": "one"}
    return _id(await client.xadd(stream_key(topic), {b"h": json.dumps(header).encode(), b"p": b"{}"}))


def _fields(topic: str, seq: int, *, epoch: str = "epoch-1", key: dict[str, str] | None = None) -> dict[bytes, bytes]:
    header = {
        "topic": topic,
        "schema_major": 1,
        "seq": seq,
        "epoch": epoch,
        "producer_id": "test",
        "origin_ts": "2026-09-14T00:00:00Z",
        "payload_kind": "control",
        "payload_schema": "schema/stream/envelope.schema.json",
    }
    if key:
        header["key"] = key
    return {b"h": json.dumps(header).encode(), b"p": b"{}"}


def _id(value: bytes) -> str:
    return value.decode()


@pytest.mark.integration
def test_reader_places_post_subscription_entry_in_its_topic_queue():
    """A reader that advances its cursor without queueing would silently drop live events."""

    async def exercise():
        client = redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
        await client.flushdb()
        await client.set(STREAM_EPOCH_KEY, "stream-epoch-1")
        session = StreamSession(_Socket(), client)
        await session._subscribe(["jobs.progress"], {})
        await _publish_progress(client, 1)
        task = asyncio.create_task(session._read_loop())
        try:
            for _ in range(100):
                if len(session.queues["jobs.progress"]):
                    break
                await asyncio.sleep(0.01)
            assert len(session.queues["jobs.progress"]) == 1
        finally:
            session._closed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.flushdb()
            await client.aclose()

    asyncio.run(exercise())


def test_durable_overflow_suspends_the_topic_at_the_first_unqueued_sequence(monkeypatch):
    """Continuing a durable topic after overflow would silently omit its gap."""

    async def exercise():
        client = redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
        session = StreamSession(_Socket(), client)
        monkeypatch.setattr("q_backend.streaming.ws.session.QUEUE_CAPACITY", {"jobs.terminal": 2})
        await session._subscribe(["jobs.terminal"], {})
        for seq in (1, 2, 3):
            await session._offer_entry("jobs.terminal", f"1-{seq}".encode(), _fields("jobs.terminal", seq))
        assert await session._controls.get() == {
            "topics": {"jobs.terminal": {"cursor": "0-0", "epoch": "", "last_seq": 0}}
        }
        assert (await session._controls.get())["topic"] == "jobs.terminal"
        assert await session._controls.get() == {"topic": "jobs.terminal", "from_seq": 3}
        assert "jobs.terminal" in session.suspended
        assert len(session.queues["jobs.terminal"]) == 0
        await client.aclose()

    asyncio.run(exercise())


def test_epoch_change_notice_is_queued_before_the_new_epoch_entry():
    """Delivering an entry first makes its sequence uninterpretable to the client."""

    async def exercise():
        client = redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
        session = StreamSession(_Socket(), client)
        await session._subscribe(["jobs.terminal"], {})
        await session._controls.get()
        session.epochs["jobs.terminal"] = "epoch-1"
        await session._offer_entry("jobs.terminal", b"2-0", _fields("jobs.terminal", 1, epoch="epoch-2"))
        assert await session._controls.get() == {
            "topic": "jobs.terminal",
            "new_epoch": "epoch-2",
            "previous_epoch": "epoch-1",
        }
        assert session.queues["jobs.terminal"].pop().seq == 1
        await client.aclose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_resume_cursor_replays_only_entries_after_it_and_expired_cursor_is_not_forwarded():
    """Ignoring trim boundaries silently presents a gap as a valid resume."""

    async def exercise():
        client = redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
        await client.flushdb()
        await client.set(STREAM_EPOCH_KEY, "stream-epoch-1")
        topic = "jobs.terminal"
        ids = [await _publish_progress(client, seq, topic) for seq in range(1, 11)]

        session = StreamSession(_Socket(), client)
        await session._subscribe([topic], {topic: ids[4]})
        task = asyncio.create_task(session._read_loop())
        try:
            for _ in range(100):
                if len(session.queues[topic]) == 5:
                    break
                await asyncio.sleep(0.01)
            assert [session.queues[topic].pop().seq for _ in range(5)] == [6, 7, 8, 9, 10]
        finally:
            session._closed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        await client.xtrim(stream_key(topic), maxlen=3, approximate=False)
        expired = StreamSession(_Socket(), client)
        await expired._subscribe([topic], {topic: ids[0]})
        assert await expired._controls.get() == {"topic": topic, "cursor": ids[0]}
        assert topic not in expired.queues
        await client.flushdb()
        await client.aclose()

    asyncio.run(exercise())
