import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from q_backend.storage.db.engine import get_engine


@pytest.mark.integration
def test_migration_up_and_down():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")

    alembic_cfg = Config("alembic.ini")

    # Ensure we are at 20260702_0016
    command.downgrade(alembic_cfg, "20260702_0016")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "stream_outbox" not in tables
    assert "stream_outbox_topic_state" not in tables

    # Upgrade to head
    command.upgrade(alembic_cfg, "head")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "stream_outbox" in tables
    assert "stream_outbox_topic_state" in tables

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT topic, epoch, last_seq, last_relayed_seq FROM stream_outbox_topic_state")
        ).fetchall()
        assert len(rows) == 7
        for row in rows:
            assert row[2] == 0
            assert row[3] == 0
            assert bool(row[1])

    # Downgrade back to 20260702_0016
    command.downgrade(alembic_cfg, "20260702_0016")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "stream_outbox" not in tables
    assert "stream_outbox_topic_state" not in tables

    # Return to head
    command.upgrade(alembic_cfg, "head")
