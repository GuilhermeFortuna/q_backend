import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.outbox_models import OutboxTopicState
from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.outbox import record_event
from q_backend.streaming.redis_binary import get_binary_redis
from q_backend.streaming.relay import OutboxRelay, RelayConfig


@pytest.fixture
def clean_test_topics():
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    client = get_binary_redis()
    topics = ["jobs.terminal", "deployments"]

    with session_factory() as session:
        for t in topics:
            session.execute(text(f"DELETE FROM stream_outbox WHERE topic = '{t}'"))
            session.execute(
                text(f"UPDATE stream_outbox_topic_state SET last_seq = 0, last_relayed_seq = 0 WHERE topic = '{t}'")
            )
        session.commit()

    for t in topics:
        client.delete(stream_key(t))

    yield session_factory, client

    with session_factory() as session:
        for t in topics:
            session.execute(text(f"DELETE FROM stream_outbox WHERE topic = '{t}'"))
            session.execute(
                text(f"UPDATE stream_outbox_topic_state SET last_seq = 0, last_relayed_seq = 0 WHERE topic = '{t}'")
            )
        session.commit()

    for t in topics:
        client.delete(stream_key(t))


@pytest.mark.integration
def test_relay_run_once_orders_and_advances_progress(clean_test_topics):
    session_factory, client = clean_test_topics

    # 1. Record 3 events on jobs.terminal and 2 on deployments
    with session_factory() as session:
        for i in range(1, 4):
            record_event(
                session,
                "jobs.terminal",
                {
                    "job_id": f"job-term-{i}",
                    "kind": "backtest",
                    "status": "completed",
                    "finished_at": f"2026-09-12T00:0{i}:00Z",
                },
                payload_schema="schema/stream/payloads/job-terminal.schema.json",
                producer_id="worker-test",
            )
        for j in range(1, 3):
            record_event(
                session,
                "deployments",
                {
                    "deployment_id": f"dep-{j}",
                    "status": "active",
                },
                payload_schema="schema/stream/envelope.schema.json",
                producer_id="deployer-test",
            )
        session.commit()

    # 2. Run relay once
    relay = OutboxRelay(session_factory, client, RelayConfig(batch_size=500))
    appended = relay.run_once()
    assert appended == 5

    # 3. Check Redis streams
    entries_term = client.xrange(stream_key("jobs.terminal"))
    assert len(entries_term) == 3
    for idx, (_, fields) in enumerate(entries_term, start=1):
        env, _ = decode_entry(fields)
        assert env.seq == idx
        assert env.topic == "jobs.terminal"
        assert env.payload["job_id"] == f"job-term-{idx}"

    entries_dep = client.xrange(stream_key("deployments"))
    assert len(entries_dep) == 2
    for idx, (_, fields) in enumerate(entries_dep, start=1):
        env, _ = decode_entry(fields)
        assert env.seq == idx
        assert env.topic == "deployments"
        assert env.payload["deployment_id"] == f"dep-{idx}"

    # 4. Check Postgres last_relayed_seq
    with session_factory() as session:
        state_term = session.execute(
            select(OutboxTopicState).where(OutboxTopicState.topic == "jobs.terminal")
        ).scalar_one()
        assert state_term.last_relayed_seq == 3

        state_dep = session.execute(
            select(OutboxTopicState).where(OutboxTopicState.topic == "deployments")
        ).scalar_one()
        assert state_dep.last_relayed_seq == 2

    # 5. A second run_once appends nothing
    second_appended = relay.run_once()
    assert second_appended == 0
    assert len(client.xrange(stream_key("jobs.terminal"))) == 3
    assert len(client.xrange(stream_key("deployments"))) == 2
