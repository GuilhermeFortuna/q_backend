import asyncio

import pytest

from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key, topic_epoch_key
from q_backend.streaming.ws.session import StreamSession
from tests.streaming.ws.conftest import FakeSocket, async_redis, envelope_fields, eventually, xadd

pytestmark = pytest.mark.integration


def run(scenario):
    async def wrapper():
        client = async_redis()
        await client.flushdb()
        await client.set(STREAM_EPOCH_KEY, "stream-epoch-1")
        try:
            await scenario(client)
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(wrapper())


def test_reader_places_post_subscription_entry_in_its_topic_queue():
    """A reader that advances its cursor without queueing would silently drop live events."""

    async def scenario(client):
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.progress"], {})
        await xadd(client, "jobs.progress", 1, key={"kind": "backtest", "job_id": "one"})
        task = asyncio.create_task(session._read_loop())
        try:
            await eventually(lambda: len(session.queues["jobs.progress"]) == 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run(scenario)


def test_first_entry_on_an_idle_topic_adopts_its_epoch_without_a_notice():
    """An idle durable stream has no known epoch; its first event must not force a resnapshot."""

    async def scenario(client):
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.terminal"], {})
        await session._offer_entry("jobs.terminal", b"1-1", envelope_fields("jobs.terminal", 1))

        assert [frame["type"] for frame in session._controls] == ["subscribed"]
        assert session.epochs["jobs.terminal"] == "epoch-1"
        assert len(session.queues["jobs.terminal"]) == 1

    run(scenario)


def test_ephemeral_acknowledgement_reports_the_topic_epoch_of_an_empty_stream():
    async def scenario(client):
        await client.set(topic_epoch_key("quotes"), "quotes-epoch-7")
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["quotes"], {})

        assert session._controls[0]["topics"]["quotes"]["epoch"] == "quotes-epoch-7"

    run(scenario)


def test_epoch_change_discards_old_epoch_entries_and_notifies_before_the_new_one():
    """An old-epoch entry delivered after the notice has a sequence the client can no longer place."""

    async def scenario(client):
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.terminal"], {})
        session._controls.clear()
        await session._offer_entry("jobs.terminal", b"1-1", envelope_fields("jobs.terminal", 1))
        await session._offer_entry("jobs.terminal", b"2-0", envelope_fields("jobs.terminal", 1, epoch="epoch-2"))

        assert list(session._controls) == [
            {"type": "epoch_changed", "topic": "jobs.terminal", "new_epoch": "epoch-2", "previous_epoch": "epoch-1"}
        ]
        queue = session.queues["jobs.terminal"]
        assert len(queue) == 1
        assert queue.pop().epoch == "epoch-2"

    run(scenario)


def test_relay_duplicates_are_forwarded_once():
    """The relay may re-append a batch after a crash; a client must still see each entry once."""

    async def scenario(client):
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.terminal"], {})
        for index, seq in enumerate([1, 2, 2, 1, 3]):
            await session._offer_entry("jobs.terminal", f"1-{index}".encode(), envelope_fields("jobs.terminal", seq))

        queue = session.queues["jobs.terminal"]
        assert [queue.pop().seq for _ in range(len(queue))] == [1, 2, 3]
        assert session.cursors["jobs.terminal"] == "1-4"

    run(scenario)


def test_resubscribing_without_a_cursor_keeps_the_queued_entries():
    """Replacing the queue on a repeated subscribe would silently drop durable entries."""

    async def scenario(client):
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.terminal"], {})
        await session._offer_entry("jobs.terminal", b"1-1", envelope_fields("jobs.terminal", 1))
        await session._subscribe(["jobs.terminal"], {})

        assert len(session.queues["jobs.terminal"]) == 1
        assert session._controls[-1]["topics"]["jobs.terminal"]["cursor"] == "1-1"

    run(scenario)


def test_entries_read_before_a_resubscribe_with_a_cursor_are_not_queued():
    """An in-flight read carries the old cursor; its entries would duplicate the resumed range."""

    async def scenario(client):
        ids = [await xadd(client, "jobs.terminal", seq) for seq in range(1, 4)]
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.terminal"], {"jobs.terminal": ids[0]})
        generation = dict(session.generations)
        await session._subscribe(["jobs.terminal"], {"jobs.terminal": ids[1]})

        stale = [(ids[1].encode(), envelope_fields("jobs.terminal", 2))]
        await session._offer_batch("jobs.terminal", stale, generation)

        assert len(session.queues["jobs.terminal"]) == 0
        assert session.cursors["jobs.terminal"] == ids[1]

    run(scenario)


def test_resume_cursor_replays_only_entries_after_it_and_expired_cursor_is_not_forwarded():
    """Ignoring trim boundaries silently presents a gap as a valid resume."""

    async def scenario(client):
        topic = "jobs.terminal"
        ids = [await xadd(client, topic, seq) for seq in range(1, 11)]

        session = StreamSession(FakeSocket(), client)
        await session._subscribe([topic], {topic: ids[4]})
        task = asyncio.create_task(session._read_loop())
        try:
            await eventually(lambda: len(session.queues[topic]) == 5)
            assert [session.queues[topic].pop().seq for _ in range(5)] == [6, 7, 8, 9, 10]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        await client.xtrim(stream_key(topic), maxlen=3, approximate=False)
        expired = StreamSession(FakeSocket(), client)
        await expired._subscribe([topic], {topic: ids[0]})
        assert list(expired._controls) == [{"type": "cursor_expired", "topic": topic, "cursor": ids[0]}]
        assert topic not in expired.queues

    run(scenario)


def test_durable_overflow_names_the_oldest_discarded_sequence_and_suspends(monkeypatch):
    """Clearing queued entries but naming a later sequence makes the client believe it holds them."""

    async def scenario(client):
        monkeypatch.setattr("q_backend.streaming.ws.session.QUEUE_CAPACITY", {"jobs.terminal": 2})
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["jobs.terminal"], {})
        session._controls.clear()
        for seq in (1, 2, 3, 4):
            await session._offer_entry("jobs.terminal", f"1-{seq}".encode(), envelope_fields("jobs.terminal", seq))

        assert list(session._controls) == [{"type": "lagging", "topic": "jobs.terminal", "from_seq": 1}]
        assert "jobs.terminal" in session.suspended
        assert len(session.queues["jobs.terminal"]) == 0

    run(scenario)


def test_non_durable_lag_overflow_continues_from_the_newest_entries_with_one_notice(monkeypatch):
    """One notice per overflow per entry grows server memory without bound for a stalled client."""

    async def scenario(client):
        monkeypatch.setattr("q_backend.streaming.ws.session.QUEUE_CAPACITY", {"bars.completed": 4})
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["bars.completed"], {})
        session._controls.clear()
        for seq in range(1, 5001):
            await session._offer_entry(
                "bars.completed",
                f"1-{seq}".encode(),
                envelope_fields("bars.completed", seq, key={"symbol": "A", "timeframe": "M1"}),
            )

        assert list(session._controls) == [{"type": "lagging", "topic": "bars.completed", "from_seq": 1}]
        queue = session.queues["bars.completed"]
        assert len(queue) <= 4
        assert [queue.pop().seq for _ in range(len(queue))][-1] == 5000

    run(scenario)


def test_coalescing_key_overflow_keeps_existing_keys_and_notifies_once(monkeypatch):
    async def scenario(client):
        monkeypatch.setattr("q_backend.streaming.ws.session.QUEUE_CAPACITY", {"quotes": 2})
        session = StreamSession(FakeSocket(), client)
        await session._subscribe(["quotes"], {})
        session._controls.clear()
        for seq, symbol in enumerate(["A", "B", "C", "D", "A"], start=1):
            await session._offer_entry(
                "quotes", f"1-{seq}".encode(), envelope_fields("quotes", seq, key={"symbol": symbol})
            )

        assert list(session._controls) == [{"type": "lagging", "topic": "quotes", "from_seq": 3}]
        queue = session.queues["quotes"]
        assert [queue.pop().seq, queue.pop().seq] == [5, 2]

    run(scenario)


def test_flushed_redis_notifies_every_subscribed_topic_once_before_new_entries():
    async def scenario(client):
        socket = FakeSocket()
        session = StreamSession(socket, client)
        task = asyncio.create_task(session.run())
        try:
            socket.subscribe(["jobs.progress", "jobs.terminal"])
            await eventually(lambda: socket.controls("subscribed"))
            await client.flushdb()
            await client.set(STREAM_EPOCH_KEY, "stream-epoch-2")
            await client.set(topic_epoch_key("jobs.progress"), "progress-epoch-2")
            await xadd(client, "jobs.progress", 1, epoch="progress-epoch-2", key={"kind": "backtest", "job_id": "a"})
            await xadd(client, "jobs.terminal", 1, epoch="epoch-1")
            await eventually(lambda: socket.entries("jobs.progress") and socket.entries("jobs.terminal"))

            notices = socket.controls("epoch_changed")
            assert sorted(notice["topic"] for notice in notices) == ["jobs.progress", "jobs.terminal"]
            first_entry = min(socket.frames.index(socket.entries(t)[0]) for t in ("jobs.progress", "jobs.terminal"))
            assert all(socket.frames.index(notice) < first_entry for notice in notices)
            progress = next(notice for notice in notices if notice["topic"] == "jobs.progress")
            assert progress["new_epoch"] == "progress-epoch-2"
        finally:
            socket.disconnect()
            await asyncio.wait_for(task, 5)

    run(scenario)


def test_repeated_client_frames_from_a_stalled_client_close_the_connection():
    """Acknowledgements for a client that never reads would otherwise accumulate without bound."""

    async def scenario(client):
        socket = FakeSocket()
        socket.stall()
        session = StreamSession(socket, client)
        task = asyncio.create_task(session.run())
        for _ in range(1000):
            socket.subscribe(["jobs.terminal"])
        socket.resume()
        await asyncio.wait_for(task, 5)
        assert socket.close_code == 1008

    run(scenario)
