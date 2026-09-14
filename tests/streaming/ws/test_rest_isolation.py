"""Criterion 10: serving a full-rate stream does not slow REST requests."""

import statistics
import threading
import time

import pytest
import redis
import redis.asyncio
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.streaming.keys import STREAM_EPOCH_KEY
from q_backend.streaming.publisher import EphemeralPublisher
from tests.streaming.ws.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.integration

REQUESTS = 100
QUOTES_PER_SECOND = 500
HEALTH_PATH = "/api/v1/system/health"
STREAM_WINDOW_S = 5.0


def health_p95(client: TestClient, min_duration_s: float = 0.0) -> float:
    durations = []
    deadline = time.monotonic() + min_duration_s
    while len(durations) < REQUESTS or time.monotonic() < deadline:
        started = time.perf_counter()
        assert client.get(HEALTH_PATH).status_code == 200
        durations.append(time.perf_counter() - started)
    return statistics.quantiles(durations, n=100, method="inclusive")[94]


def test_health_latency_is_unaffected_by_a_full_rate_quote_stream(monkeypatch):
    sync = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=False)
    sync.flushdb()
    sync.set(STREAM_EPOCH_KEY, "stream-epoch-1")
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=False),
    )
    publisher = EphemeralPublisher(sync, "quotes", producer_id="isolation-test")
    stop = threading.Event()
    received = 0

    def publish_quotes():
        seq = 0
        while not stop.is_set():
            seq += 1
            publisher.publish(
                routing_key={"symbol": f"S{seq % 8}"},
                payload_kind="arrow_ipc",
                payload_schema="schema/api/arrow/ticks.schema.json",
                payload=b"x" * 256,
            )
            time.sleep(1 / QUOTES_PER_SECOND)
        publisher.publish(
            routing_key={"symbol": "END"},
            payload_kind="arrow_ipc",
            payload_schema="schema/api/arrow/ticks.schema.json",
            payload=b"END",
        )

    try:
        with TestClient(app) as client:
            client.get(HEALTH_PATH)
            baseline = health_p95(client)

            with client.websocket_connect("/api/v1/stream") as socket:
                socket.send_json({"topics": ["quotes"]})
                socket.receive_json()

                def consume():
                    nonlocal received
                    while not socket.receive_bytes().endswith(b"END"):
                        received += 1

                consumer = threading.Thread(target=consume)
                producer = threading.Thread(target=publish_quotes)
                consumer.start()
                producer.start()
                try:
                    time.sleep(1)
                    streaming = health_p95(client, min_duration_s=STREAM_WINDOW_S)
                finally:
                    stop.set()
                    producer.join(10)
                    consumer.join(10)
    finally:
        sync.flushdb()
        sync.close()

    print(
        f"health p95 without stream {baseline * 1000:.2f} ms, with stream {streaming * 1000:.2f} ms; {received} quotes"
    )
    assert received >= QUOTES_PER_SECOND * STREAM_WINDOW_S / 4
    assert streaming <= max(2 * baseline, baseline + 0.02)
