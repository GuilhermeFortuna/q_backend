import dataclasses
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import STREAM_EPOCH_KEY, stream_key
from q_backend.streaming.outbox import record_event
from q_backend.streaming.redis_binary import get_binary_redis
from q_backend.streaming.relay import OutboxRelay, RelayConfig
from q_contracts.topics import TOPICS


@pytest.mark.integration
def test_relay_republishes_retained_on_redis_flush():
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    client = get_binary_redis()
    topic = "jobs.terminal"

    # Reset
    with session_factory() as session:
        session.execute(text(f"DELETE FROM stream_outbox WHERE topic = '{topic}'"))
        session.execute(
            text(f"UPDATE stream_outbox_topic_state SET last_seq = 0, last_relayed_seq = 0 WHERE topic = '{topic}'")
        )
        for i in range(1, 11):
            record_event(
                session,
                topic,
                {
                    "job_id": f"job-rec-{i}",
                    "kind": "backtest",
                    "status": "completed",
                    "finished_at": f"2026-09-12T00:{i:02d}:00Z",
                },
                payload_schema="schema/stream/payloads/job-terminal.schema.json",
                producer_id="worker-rec",
            )
        session.commit()

    client.delete(stream_key(topic))

    relay = OutboxRelay(session_factory, client, RelayConfig(batch_size=500))
    appended = relay.run_once()
    assert appended == 10

    # Old stream epoch
    old_epoch = client.get(STREAM_EPOCH_KEY)
    assert old_epoch is not None

    # Flush Redis
    client.flushall()

    # Run once after flush
    relay.run_once()

    # Stream epoch has a new value
    new_epoch = client.get(STREAM_EPOCH_KEY)
    assert new_epoch is not None
    assert new_epoch != old_epoch

    # Stream holds the retained window (all 10 here)
    entries = client.xrange(stream_key(topic))
    assert len(entries) == 10
    seqs = [decode_entry(fields)[0].seq for _, fields in entries]
    assert seqs == list(range(1, 11))


@pytest.mark.integration
def test_relay_republishes_bounded_by_retention_entries(monkeypatch):
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    client = get_binary_redis()
    topic = "jobs.terminal"

    # Monkeypatch retention_entries to 4
    custom_policy = dataclasses.replace(TOPICS[topic], retention_entries=4)
    monkeypatch.setitem(TOPICS, topic, custom_policy)

    with session_factory() as session:
        session.execute(text(f"DELETE FROM stream_outbox WHERE topic = '{topic}'"))
        session.execute(
            text(f"UPDATE stream_outbox_topic_state SET last_seq = 0, last_relayed_seq = 0 WHERE topic = '{topic}'")
        )
        for i in range(1, 11):
            record_event(
                session,
                topic,
                {
                    "job_id": f"job-bound-{i}",
                    "kind": "backtest",
                    "status": "completed",
                    "finished_at": f"2026-09-12T00:{i:02d}:00Z",
                },
                payload_schema="schema/stream/payloads/job-terminal.schema.json",
                producer_id="worker-bound",
            )
        session.commit()

    client.delete(stream_key(topic))

    relay = OutboxRelay(session_factory, client, RelayConfig(batch_size=500))
    relay.run_once()

    old_epoch = client.get(STREAM_EPOCH_KEY)
    assert old_epoch is not None

    # Flush Redis
    client.flushall()

    # Run once after flush
    relay.run_once()

    new_epoch = client.get(STREAM_EPOCH_KEY)
    assert new_epoch is not None
    assert new_epoch != old_epoch

    # With retention_entries monkeypatched to 4, it holds 7-10
    entries = client.xrange(stream_key(topic))
    assert len(entries) == 4
    seqs = [decode_entry(fields)[0].seq for _, fields in entries]
    assert seqs == [7, 8, 9, 10]
