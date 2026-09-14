from __future__ import annotations

from pathlib import Path
import threading
import time
from unittest.mock import MagicMock

import fakeredis
import pytest
import redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.base import Base
from q_backend.streaming.relay import OutboxRelay, RelayConfig


@pytest.fixture
def relay_db(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/relay.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_relay_on_first_success_empty_outbox(relay_db):
    session_factory = sessionmaker(bind=relay_db, expire_on_commit=False)
    fake_client = fakeredis.FakeRedis(decode_responses=False)
    config = RelayConfig(poll_interval_s=0.01, max_backoff_s=0.05)
    relay = OutboxRelay(session_factory, fake_client, config)

    stop = threading.Event()
    callback = MagicMock()
    passes = 0
    orig_run_once = relay.run_once

    def tracked_run_once() -> int:
        nonlocal passes
        res = orig_run_once()
        passes += 1
        if passes >= 3:
            stop.set()
        return res

    relay.run_once = tracked_run_once  # type: ignore[assignment]
    relay.run_forever(stop, on_first_success=callback)

    assert passes >= 3
    assert callback.call_count == 1


def test_relay_on_first_success_delayed_by_redis_outage(relay_db):
    session_factory = sessionmaker(bind=relay_db, expire_on_commit=False)
    real_fake = fakeredis.FakeRedis(decode_responses=False)
    config = RelayConfig(poll_interval_s=0.05, max_backoff_s=0.2)

    start = time.monotonic()
    outage_until = start + 2.0

    class FailingRedisProxy:
        def __getattr__(self, name: str):
            if time.monotonic() < outage_until:
                raise redis.ConnectionError("Simulated Redis outage")
            return getattr(real_fake, name)

    proxy_client = FailingRedisProxy()
    relay = OutboxRelay(session_factory, proxy_client, config)  # type: ignore[arg-type]

    stop = threading.Event()
    callback_called = threading.Event()
    callback_time: list[float] = []

    def on_success():
        callback_time.append(time.monotonic())
        callback_called.set()
        stop.set()

    t = threading.Thread(target=relay.run_forever, args=(stop, on_success))
    t.start()

    # After 1.0s, outage is still active, callback must not have been called
    time.sleep(1.0)
    assert not callback_called.is_set()

    # Wait for completion after outage recovers
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert callback_called.is_set()
    assert len(callback_time) == 1
    assert callback_time[0] >= outage_until
