import json
import struct

import pyarrow as pa
import pytest
import redis
import redis.asyncio
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key
from q_backend.streaming.publisher import EphemeralPublisher
from tests.streaming.ws.conftest import TEST_REDIS_URL, envelope_fields

pytestmark = pytest.mark.integration


@pytest.fixture
def stream_redis(monkeypatch):
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
    client.flushdb()
    client.set(STREAM_EPOCH_KEY, "stream-epoch-1")
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False),
    )
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


def publish(client, topic: str, seq: int, **kwargs) -> str:
    return client.xadd(stream_key(topic), envelope_fields(topic, seq, **kwargs)).decode()


def test_subscription_rejects_unknown_topics_and_forwards_only_entries_after_ack(stream_redis):
    """Criteria 1 and 2: history is not leaked, unknown topics are named, and order is kept."""
    publish(stream_redis, "jobs.progress", 1, key={"kind": "backtest", "job_id": "one"})
    publish(stream_redis, "jobs.terminal", 1)

    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.progress", "jobs.terminal", "nope"]})
        subscribed = socket.receive_json()
        assert subscribed["type"] == "subscribed"
        assert subscribed["topics"].keys() == {"jobs.progress", "jobs.terminal"}
        assert socket.receive_json() == {"type": "rejected", "reason": "unknown_topic", "topic": "nope"}

        for seq in (2, 3, 4):
            publish(stream_redis, "jobs.terminal", seq)
        publish(stream_redis, "jobs.progress", 2, key={"kind": "backtest", "job_id": "one"})

        frames = [socket.receive_json() for _ in range(4)]
        assert [frame["seq"] for frame in frames if frame["topic"] == "jobs.terminal"] == [2, 3, 4]
        assert [frame["seq"] for frame in frames if frame["topic"] == "jobs.progress"] == [2]
        assert all("type" not in frame for frame in frames)


def test_resume_cursor_over_the_socket_yields_exactly_the_later_entries(stream_redis):
    """Criterion 3."""
    ids = [publish(stream_redis, "jobs.terminal", seq) for seq in range(1, 11)]

    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.terminal"], "cursors": {"jobs.terminal": ids[4]}})
        assert socket.receive_json()["topics"]["jobs.terminal"]["cursor"] == ids[4]
        assert [socket.receive_json()["seq"] for _ in range(5)] == [6, 7, 8, 9, 10]

        stream_redis.xtrim(stream_key("jobs.terminal"), maxlen=3, approximate=False)
        socket.send_json({"topics": ["jobs.terminal"], "cursors": {"jobs.terminal": ids[0]}})
        assert socket.receive_json() == {"type": "cursor_expired", "topic": "jobs.terminal", "cursor": ids[0]}


def test_quotes_arrive_as_binary_frames_that_decode_to_the_published_envelope(stream_redis):
    """Criterion 4, through the real ephemeral publisher and a real Arrow IPC payload."""
    table = pa.table({"time_msc": [1, 2, 3], "bid": [10.0, 10.5, 11.0]})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    payload = sink.getvalue().to_pybytes()
    publisher = EphemeralPublisher(stream_redis, "quotes", producer_id="test")

    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["quotes"]})
        socket.receive_json()
        publisher.publish(
            routing_key={"symbol": "WINZ25"},
            payload_kind="arrow_ipc",
            payload_schema="schema/api/arrow/ticks.schema.json",
            payload=payload,
        )
        frame = socket.receive_bytes()

    header_len = struct.unpack("<I", frame[:4])[0]
    header = json.loads(frame[4 : 4 + header_len])
    ((_, entries),) = stream_redis.xread({stream_key("quotes"): "0-0"})
    stored_header = json.loads(entries[0][1][b"h"])
    assert header == stored_header
    assert header["key"] == {"symbol": "WINZ25"}
    assert frame[4 + header_len :] == payload
    assert pa.ipc.open_stream(frame[4 + header_len :]).read_all().equals(table)


def test_malformed_cursor_is_rejected_as_an_invalid_frame_and_the_socket_stays_open(stream_redis):
    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.terminal"], "cursors": {"jobs.terminal": "garbage"}})
        rejected = socket.receive_json()
        assert rejected["type"] == "rejected"
        assert rejected["reason"] == "invalid_frame"

        socket.send_json({"topics": ["quotes"]})
        assert socket.receive_json()["topics"].keys() == {"quotes"}
