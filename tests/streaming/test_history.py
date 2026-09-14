import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.base import utc_now
from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.outbox_models import OutboxEvent
from q_backend.streaming.outbox import prune_relayed, record_event, rotate_epoch
from q_backend.streaming.snapshot import EpochMismatch, HistoryExpired, read_history


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


def _seed_terminal_events(session, count: int = 10) -> str:
    session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
    session.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 0 WHERE topic = 'jobs.terminal'"))
    session.commit()

    epoch = session.execute(
        text("SELECT epoch FROM stream_outbox_topic_state WHERE topic = 'jobs.terminal'")
    ).scalar_one()
    for seq in range(1, count + 1):
        record_event(
            session,
            "jobs.terminal",
            {
                "job_id": f"job-{seq}",
                "kind": "backtest",
                "status": "completed",
                "finished_at": utc_now().isoformat(),
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id=f"producer-{seq}",
            routing_key={"kind": "backtest", "job_id": f"job-{seq}"},
        )
    session.commit()
    return epoch


@pytest.mark.integration
def test_read_history_pages_and_signals_more():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        epoch = _seed_terminal_events(session, count=10)

    with session_factory() as session:
        page = read_history(session, "jobs.terminal", epoch, from_seq=5, limit=3)
        assert [entry.seq for entry in page.entries] == [5, 6, 7]
        assert page.next_seq == 8

        last_page = read_history(session, "jobs.terminal", epoch, from_seq=9, limit=3)
        assert [entry.seq for entry in last_page.entries] == [9, 10]
        assert last_page.next_seq is None


@pytest.mark.integration
def test_read_history_expired_after_prune():
    from datetime import timedelta

    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        epoch = _seed_terminal_events(session, count=10)
        session.execute(
            text("UPDATE stream_outbox_topic_state " "SET last_relayed_seq = 6 WHERE topic = 'jobs.terminal'")
        )
        old_time = utc_now() - timedelta(days=32)
        session.execute(
            text("UPDATE stream_outbox SET recorded_at = :old_time WHERE topic = 'jobs.terminal'"),
            {"old_time": old_time},
        )
        session.commit()

    with session_factory() as session:
        prune_relayed(session, older_than=timedelta(days=30))
        session.commit()

    with session_factory() as session:
        with pytest.raises(HistoryExpired) as excinfo:
            read_history(session, "jobs.terminal", epoch, from_seq=3, limit=3)
        exc = excinfo.value
        assert exc.oldest_available_seq == 7
        assert exc.requested_from_seq == 3


@pytest.mark.integration
def test_read_history_epoch_mismatch():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        epoch = _seed_terminal_events(session, count=3)

    with session_factory() as session:
        old_epoch, new_epoch = rotate_epoch(session, "jobs.terminal", reason="test rotation")
        session.commit()
        assert old_epoch == epoch

    with session_factory() as session:
        with pytest.raises(EpochMismatch) as excinfo:
            read_history(session, "jobs.terminal", epoch, from_seq=1, limit=3)
        exc = excinfo.value
        assert exc.requested_epoch == epoch
        assert exc.current_epoch == new_epoch
