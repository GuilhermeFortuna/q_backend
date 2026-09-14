import json

import fakeredis
import fakeredis.aioredis
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.streaming.keys import stream_key


def _publish(server, topic: str, seq: int, *, key: dict[str, str] | None = None) -> str:
    if topic == "jobs.progress" and key is None:
        key = {"kind": "backtest", "job_id": "job-1"}
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
    if key:
        header["key"] = key
    client = fakeredis.FakeRedis(server=server, decode_responses=False)
    return client.xadd(stream_key(topic), {b"h": json.dumps(header).encode(), b"p": b'{"status":"ok"}'}).decode()


def test_subscription_acknowledges_declared_topics_rejects_unknown_and_forwards_new_entries(monkeypatch):
    """A route that accepts unknown topics or leaks pre-ack history breaks live-only startup."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=False),
    )
    _publish(server, "jobs.progress", 1)

    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.progress", "jobs.terminal", "nope"]})

        assert socket.receive_json()["topics"].keys() == {"jobs.progress", "jobs.terminal"}
        assert socket.receive_json() == {"reason": "unknown_topic", "topic": "nope"}


def test_later_subscription_adds_a_topic_without_leaking_to_unsubscribed_clients(monkeypatch):
    """Forwarding every stream after connect would violate topic isolation."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=False),
    )

    with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as socket:
        socket.send_json({"topics": ["jobs.progress"]})
        socket.receive_json()
        socket.send_json({"topics": ["quotes"]})
        assert socket.receive_json()["topics"].keys() == {"quotes"}
