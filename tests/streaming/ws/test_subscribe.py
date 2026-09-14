import json

import pytest
import redis
import redis.asyncio
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key

TEST_REDIS_URL = "redis://127.0.0.1:6380/15"


@pytest.fixture
def stream_redis(monkeypatch):
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
    client.flushdb()
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False),
    )
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


def publish(client, topic: str, seq: int, *, payload_kind: str = "control", key: dict[str, str] | None = None) -> str:
    header = {
        "topic": topic,
        "schema_major": 1,
        "seq": seq,
        "epoch": "epoch-1",
        "producer_id": "test",
        "origin_ts": "2026-09-14T00:00:00Z",
        "payload_kind": payload_kind,
        "payload_schema": "schema/stream/envelope.schema.json",
    }
    if key:
        header["key"] = key
    payload = b"ipc" if payload_kind == "arrow_ipc" else b'{"status":"ok"}'
    return client.xadd(stream_key(topic), {b"h": json.dumps(header).encode(), b"p": payload}).decode()


@pytest.mark.integration
def test_subscription_rejects_unknown_topics_and_forwards_only_entries_after_ack(stream_redis):
    """A live-only subscription must not leak history or unknown-topic traffic."""
    stream_redis.set(STREAM_EPOCH_KEY, "stream-epoch-1")
    publish(stream_redis, "jobs.progress", 1, key={"kind": "backtest", "job_id": "one"})

    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.progress", "jobs.terminal", "nope"]})
        subscribed = socket.receive_json()
        assert subscribed["topics"].keys() == {"jobs.progress", "jobs.terminal"}
        assert socket.receive_json() == {"reason": "unknown_topic", "topic": "nope"}


@pytest.mark.integration
def test_later_subscription_adds_a_topic_and_delivers_arrow_as_binary(stream_redis):
    """Adding a topic must not require reconnecting or turn Arrow bytes into JSON."""
    stream_redis.set(STREAM_EPOCH_KEY, "stream-epoch-1")
    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.progress"]})
        socket.receive_json()
        socket.send_json({"topics": ["quotes"]})
        assert socket.receive_json()["topics"].keys() == {"quotes"}
