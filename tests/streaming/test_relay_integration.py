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


@pytest.mark.integration
def test_relay_crash_safety_duplicate_without_gap(clean_test_topics):
    session_factory, client = clean_test_topics

    # Record 3 events on jobs.terminal
    with session_factory() as session:
        for i in range(1, 4):
            record_event(
                session,
                "jobs.terminal",
                {
                    "job_id": f"crash-job-{i}",
                    "kind": "backtest",
                    "status": "completed",
                    "finished_at": f"2026-09-12T00:0{i}:00Z",
                },
                payload_schema="schema/stream/payloads/job-terminal.schema.json",
                producer_id="worker-crash-test",
            )
        session.commit()

    # Step 1: Relay first event with batch_size=1
    relay_1 = OutboxRelay(session_factory, client, RelayConfig(batch_size=1))
    appended_1 = relay_1.run_once()
    assert appended_1 == 1

    with session_factory() as session:
        state = session.execute(select(OutboxTopicState).where(OutboxTopicState.topic == "jobs.terminal")).scalar_one()
        assert state.last_relayed_seq == 1

    # Step 2: Patch _record_progress to simulate crash right after pipeline execute
    def crash_record_progress(session, topic, epoch, last_seq):
        raise RuntimeError("Simulated crash after Redis append!")

    relay_1._record_progress = crash_record_progress

    with pytest.raises(RuntimeError, match="Simulated crash after Redis append"):
        relay_1.run_once()

    # Verify event 2 was appended to Redis, but last_relayed_seq in Postgres is still 1
    entries_mid = client.xrange(stream_key("jobs.terminal"))
    assert len(entries_mid) == 2
    assert decode_entry(entries_mid[0][1])[0].seq == 1
    assert decode_entry(entries_mid[1][1])[0].seq == 2

    with session_factory() as session:
        state = session.execute(select(OutboxTopicState).where(OutboxTopicState.topic == "jobs.terminal")).scalar_one()
        assert state.last_relayed_seq == 1

    # Step 3: Restart with a fresh relay
    fresh_relay = OutboxRelay(session_factory, client, RelayConfig(batch_size=500))
    appended_restart = fresh_relay.run_once()
    assert appended_restart == 2

    # Step 4: Verify the stream holds 1, 2, 2, 3 (one duplicate, no gaps)
    entries_final = client.xrange(stream_key("jobs.terminal"))
    final_seqs = [decode_entry(fields)[0].seq for _, fields in entries_final]
    assert final_seqs == [1, 2, 2, 3]

    with session_factory() as session:
        state = session.execute(select(OutboxTopicState).where(OutboxTopicState.topic == "jobs.terminal")).scalar_one()
        assert state.last_relayed_seq == 3
