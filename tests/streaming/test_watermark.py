import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.streaming.outbox import read_watermark, record_event


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.mark.integration
def test_read_watermark_repeatable_read_isolation():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
        session.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 0 WHERE topic = 'jobs.terminal'"))
        session.commit()

    # Session A starts in REPEATABLE READ
    conn_a = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    session_a = Session(bind=conn_a)
    try:
        wm_a1 = read_watermark(session_a, ["jobs.terminal"])
        epoch, seq_a1 = wm_a1["jobs.terminal"]
        assert seq_a1 == 0
        assert bool(epoch)

        # Session B records and commits seq 1
        with session_factory() as session_b:
            ev = record_event(
                session_b,
                "jobs.terminal",
                {
                    "job_id": "job-wm-1",
                    "kind": "backtest",
                    "status": "completed",
                    "finished_at": "2026-09-12T00:00:00Z",
                },
                payload_schema="schema/stream/payloads/job-terminal.schema.json",
                producer_id="producer-b",
            )
            assert ev.seq == 1
            session_b.commit()

        # Session A reads again and still sees earlier watermark (0)
        wm_a2 = read_watermark(session_a, ["jobs.terminal"])
        _, seq_a2 = wm_a2["jobs.terminal"]
        assert seq_a2 == 0

        # A new session sees seq 1
        with session_factory() as session_c:
            wm_c = read_watermark(session_c, ["jobs.terminal"])
            _, seq_c = wm_c["jobs.terminal"]
            assert seq_c == 1
    finally:
        session_a.close()
        conn_a.close()
