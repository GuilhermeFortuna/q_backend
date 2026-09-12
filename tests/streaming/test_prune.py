from datetime import timedelta
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.base import utc_now
from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.outbox_models import OutboxEvent, OutboxTopicState
from q_backend.streaming.outbox import oldest_retained_seq, prune_relayed


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.mark.integration
def test_prune_relayed_retention():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    old_time = utc_now() - timedelta(days=32)

    with session_factory() as session:
        # Reset jobs.terminal state
        session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
        session.execute(
            text(
                "UPDATE stream_outbox_topic_state "
                "SET last_seq = 10, last_relayed_seq = 6 "
                "WHERE topic = 'jobs.terminal'"
            )
        )
        epoch = session.execute(
            text("SELECT epoch FROM stream_outbox_topic_state WHERE topic = 'jobs.terminal'")
        ).scalar_one()

        # Seed events 1-10 all older than 31 days
        for seq in range(1, 11):
            session.add(
                OutboxEvent(
                    topic="jobs.terminal",
                    epoch=epoch,
                    seq=seq,
                    producer_id="test-producer",
                    origin_ts=old_time,
                    routing_key=None,
                    payload_kind="control",
                    payload_schema="schema/stream/payloads/job-terminal.schema.json",
                    payload={
                        "job_id": f"job-{seq}",
                        "kind": "backtest",
                        "status": "completed",
                        "finished_at": old_time.isoformat(),
                    },
                    recorded_at=old_time,
                )
            )
        session.commit()

    # First prune
    with session_factory() as session:
        retained = prune_relayed(session, older_than=timedelta(days=30))
        session.commit()
        assert retained.get("jobs.terminal") == 7
        assert oldest_retained_seq(session, "jobs.terminal") == 7

        remaining_seqs = (
            session.execute(text("SELECT seq FROM stream_outbox WHERE topic = 'jobs.terminal' ORDER BY seq"))
            .scalars()
            .all()
        )
        assert remaining_seqs == [7, 8, 9, 10]

    # Second prune deletes nothing
    with session_factory() as session:
        count_before = session.execute(
            text("SELECT count(*) FROM stream_outbox WHERE topic = 'jobs.terminal'")
        ).scalar_one()
        assert count_before == 4

        retained_second = prune_relayed(session, older_than=timedelta(days=30))
        session.commit()

        count_after = session.execute(
            text("SELECT count(*) FROM stream_outbox WHERE topic = 'jobs.terminal'")
        ).scalar_one()
        assert count_after == count_before
        assert retained_second.get("jobs.terminal") == 7
        assert oldest_retained_seq(session, "jobs.terminal") == 7
