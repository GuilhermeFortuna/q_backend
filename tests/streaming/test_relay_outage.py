import logging
import threading
import time
import pytest
import redis
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.outbox_models import OutboxTopicState
from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.outbox import record_event
from q_backend.streaming.redis_binary import get_binary_redis
from q_backend.streaming.relay import OutboxRelay, RelayConfig


class _LogCaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.integration
def test_relay_redis_outage_backoff_and_recovery():
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    topic = "jobs.terminal"

    with session_factory() as session:
        session.execute(text(f"DELETE FROM stream_outbox WHERE topic = '{topic}'"))
        session.execute(
            text(f"UPDATE stream_outbox_topic_state SET last_seq = 0, last_relayed_seq = 0 WHERE topic = '{topic}'")
        )
        record_event(
            session,
            topic,
            {
                "job_id": "job-outage-1",
                "kind": "backtest",
                "status": "completed",
                "finished_at": "2026-09-12T00:01:00Z",
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id="worker-outage",
        )
        session.commit()

    # Closed port for Redis
    bad_client = redis.Redis(
        host="127.0.0.1",
        port=6399,
        decode_responses=False,
        retry=None,
        socket_connect_timeout=0.5,
    )
    relay = OutboxRelay(session_factory, bad_client, RelayConfig(poll_interval_s=0.1, max_backoff_s=2.0))

    stop_event = threading.Event()
    thread = threading.Thread(target=relay.run_forever, args=(stop_event,))

    handler = _LogCaptureHandler()
    relay_logger = logging.getLogger("q_backend.streaming.relay")
    relay_logger.disabled = False
    relay_logger.addHandler(handler)

    try:
        thread.start()
        time.sleep(3.0)
        stop_event.set()
        thread.join(timeout=5.0)

        assert not thread.is_alive()

        # Records no progress
        with session_factory() as session:
            state = session.execute(select(OutboxTopicState).where(OutboxTopicState.topic == topic)).scalar_one()
            assert state.last_relayed_seq == 0

        # Logs backoff
        assert any("Backing off" in msg for msg in handler.messages)
    finally:
        relay_logger.removeHandler(handler)

    # Pointed back, appends everything committed meanwhile
    good_client = get_binary_redis()
    good_client.delete(stream_key(topic))
    relay.client = good_client
    appended = relay.run_once()
    assert appended == 1

    with session_factory() as session:
        state = session.execute(select(OutboxTopicState).where(OutboxTopicState.topic == topic)).scalar_one()
        assert state.last_relayed_seq == 1

    entries = good_client.xrange(stream_key(topic))
    assert len(entries) == 1
    assert decode_entry(entries[0][1])[0].seq == 1


@pytest.mark.integration
def test_relay_postgres_outage_backoff_and_recovery():
    bad_engine = create_engine(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5439/q_fake_db",
        pool_pre_ping=False,
    )
    bad_session_factory = sessionmaker(bind=bad_engine, expire_on_commit=False)
    client = get_binary_redis()

    relay = OutboxRelay(bad_session_factory, client, RelayConfig(poll_interval_s=0.1, max_backoff_s=2.0))

    stop_event = threading.Event()
    thread = threading.Thread(target=relay.run_forever, args=(stop_event,))

    handler = _LogCaptureHandler()
    relay_logger = logging.getLogger("q_backend.streaming.relay")
    relay_logger.disabled = False
    relay_logger.addHandler(handler)

    try:
        thread.start()
        time.sleep(2.0)
        stop_event.set()
        thread.join(timeout=5.0)

        assert not thread.is_alive()

        # Does not raise and logs backoff
        assert any("Backing off" in msg for msg in handler.messages)
    finally:
        relay_logger.removeHandler(handler)
        bad_engine.dispose()
