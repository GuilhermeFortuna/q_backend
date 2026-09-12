import concurrent.futures
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.streaming.outbox import record_event


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.mark.integration
def test_rollback_does_not_consume_seq():
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with session_factory() as session:
        session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
        session.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 0 WHERE topic = 'jobs.terminal'"))
        session.commit()

    # Session 1 records then rolls back
    with session_factory() as s1:
        ev1 = record_event(
            s1,
            "jobs.terminal",
            {
                "job_id": "job-rolled-back",
                "kind": "backtest",
                "status": "failed",
                "finished_at": "2026-09-12T00:00:00Z",
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id="test-producer",
        )
        assert ev1.seq == 1
        s1.rollback()

    # Session 2 records and commits
    with session_factory() as s2:
        ev2 = record_event(
            s2,
            "jobs.terminal",
            {
                "job_id": "job-committed",
                "kind": "backtest",
                "status": "completed",
                "finished_at": "2026-09-12T00:01:00Z",
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id="test-producer",
        )
        assert ev2.seq == 1
        s2.commit()

    with session_factory() as s3:
        rows = s3.execute(text("SELECT seq, payload FROM stream_outbox WHERE topic = 'jobs.terminal'")).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1


@pytest.mark.integration
def test_concurrent_recording_gapless():
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with session_factory() as session:
        session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
        session.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 0 WHERE topic = 'jobs.terminal'"))
        session.commit()

    def record_worker(thread_idx: int) -> int:
        with session_factory() as session:
            ev = record_event(
                session,
                "jobs.terminal",
                {
                    "job_id": f"job-{thread_idx}",
                    "kind": "backtest",
                    "status": "completed",
                    "finished_at": "2026-09-12T00:00:00Z",
                },
                payload_schema="schema/stream/payloads/job-terminal.schema.json",
                producer_id=f"worker-{thread_idx}",
            )
            seq = ev.seq
            session.commit()
            return seq

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(record_worker, i) for i in range(1, 101)]
        results = [f.result() for f in futures]

    assert len(results) == 100
    assert set(results) == set(range(1, 101))

    with session_factory() as session:
        rows = (
            session.execute(text("SELECT seq FROM stream_outbox WHERE topic = 'jobs.terminal' ORDER BY seq"))
            .scalars()
            .all()
        )
        assert len(rows) == 100
        assert set(rows) == set(range(1, 101))
