import logging
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.cli.q_outbox import main as outbox_main
from q_backend.storage.db.engine import get_engine
from q_backend.streaming.outbox import record_event, rotate_epoch


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.mark.integration
def test_rotate_epoch_resets_seq_and_logs(caplog):
    caplog.set_level(logging.INFO)
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)

    with session_factory() as session:
        session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
        session.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 5 WHERE topic = 'jobs.terminal'"))
        session.commit()

    with session_factory() as session:
        old_epoch, new_epoch = rotate_epoch(session, "jobs.terminal", reason="operator resync")
        session.commit()

        assert old_epoch != new_epoch
        assert bool(new_epoch)
        # Verify log line contains both epochs and reason
        assert old_epoch in caplog.text
        assert new_epoch in caplog.text
        assert "operator resync" in caplog.text

    # Next recorded event gets seq 1 and the new epoch
    with session_factory() as session:
        ev = record_event(
            session,
            "jobs.terminal",
            {
                "job_id": "job-post-rotation",
                "kind": "backtest",
                "status": "completed",
                "finished_at": "2026-09-12T00:00:00Z",
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id="test-producer",
        )
        session.commit()
        assert ev.seq == 1
        assert ev.epoch == new_epoch


@pytest.mark.integration
def test_alembic_upgrade_preserves_rotated_epoch():
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with session_factory() as session:
        old_epoch, new_epoch = rotate_epoch(session, "jobs.terminal", reason="test migration idempotency")
        session.commit()

    # Re-run alembic upgrade head
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

    with session_factory() as session:
        current_epoch = session.execute(
            text("SELECT epoch FROM stream_outbox_topic_state WHERE topic = 'jobs.terminal'")
        ).scalar_one()
        assert current_epoch == new_epoch


@pytest.mark.integration
def test_cli_rotate_epoch():
    ret = outbox_main(["rotate-epoch", "jobs.terminal", "--reason", "cli manual test"])
    assert ret == 0

    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with session_factory() as session:
        last_seq = session.execute(
            text("SELECT last_seq FROM stream_outbox_topic_state WHERE topic = 'jobs.terminal'")
        ).scalar_one()
        assert last_seq == 0
