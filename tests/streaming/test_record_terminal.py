import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from q_backend.storage.db.outbox_models import JobTerminalMarker, OutboxEvent


def test_record_job_terminal_success(seeded_session: Session):
    from q_backend.streaming.jobs import record_job_terminal

    # First call with raw "error" status
    recorded = record_job_terminal(
        seeded_session,
        kind="backtest",
        job_id="bt-123",
        raw_status="error",
        error="Strategy crashed",
    )
    assert recorded is True

    # Marker exists
    marker = seeded_session.scalar(
        select(JobTerminalMarker).where(
            JobTerminalMarker.kind == "backtest",
            JobTerminalMarker.job_id == "bt-123",
        )
    )
    assert marker is not None
    assert marker.status == "failed"
    assert marker.outbox_seq > 0

    # Exactly one event recorded on jobs.terminal
    events = seeded_session.scalars(select(OutboxEvent).where(OutboxEvent.topic == "jobs.terminal")).all()
    assert len(events) == 1
    event = events[0]
    assert event.seq == marker.outbox_seq
    assert event.payload["kind"] == "backtest"
    assert event.payload["job_id"] == "bt-123"
    assert event.payload["status"] == "failed"
    assert event.payload["error"] == "Strategy crashed"
    assert "finished_at" in event.payload


def test_record_job_terminal_duplicate_returns_false(seeded_session: Session):
    from q_backend.streaming.jobs import record_job_terminal

    first = record_job_terminal(
        seeded_session,
        kind="optimization",
        job_id="opt-dup",
        raw_status="completed",
    )
    assert first is True

    # Second call for the same (kind, job_id)
    second = record_job_terminal(
        seeded_session,
        kind="optimization",
        job_id="opt-dup",
        raw_status="completed",
    )
    assert second is False

    # Event count is still 1
    events = seeded_session.scalars(select(OutboxEvent).where(OutboxEvent.topic == "jobs.terminal")).all()
    assert len(events) == 1


def test_record_job_terminal_non_terminal_raises_value_error(seeded_session: Session):
    from q_backend.streaming.jobs import record_job_terminal

    with pytest.raises(ValueError, match="not a terminal status"):
        record_job_terminal(
            seeded_session,
            kind="walkforward",
            job_id="wf-running",
            raw_status="running",
        )


def test_clear_job_terminal_marker(seeded_session: Session):
    from q_backend.streaming.jobs import clear_job_terminal_marker, record_job_terminal

    record_job_terminal(
        seeded_session,
        kind="backtest",
        job_id="bt-clear-test",
        raw_status="completed",
    )
    marker = seeded_session.scalar(
        select(JobTerminalMarker).where(
            JobTerminalMarker.kind == "backtest",
            JobTerminalMarker.job_id == "bt-clear-test",
        )
    )
    assert marker is not None

    clear_job_terminal_marker(seeded_session, "backtest", "bt-clear-test")
    marker_after = seeded_session.scalar(
        select(JobTerminalMarker).where(
            JobTerminalMarker.kind == "backtest",
            JobTerminalMarker.job_id == "bt-clear-test",
        )
    )
    assert marker_after is None


def test_terminal_flag_commit_lifecycle(seeded_session: Session):
    import fakeredis
    from q_backend.streaming.jobs import (
        clear_job_terminal_marker,
        is_job_terminal_flagged,
        record_job_terminal,
        set_publisher_client,
    )

    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    set_publisher_client(fake_redis)
    try:
        record_job_terminal(
            seeded_session,
            kind="backtest",
            job_id="bt-flag-lifecycle",
            raw_status="completed",
        )
        # Not committed yet: Redis flag should not be set
        assert not is_job_terminal_flagged(fake_redis, "backtest", "bt-flag-lifecycle")

        # Commit transaction
        seeded_session.commit()
        assert is_job_terminal_flagged(fake_redis, "backtest", "bt-flag-lifecycle")

        # Clear marker
        clear_job_terminal_marker(seeded_session, "backtest", "bt-flag-lifecycle")
        # Still flagged before commit
        assert is_job_terminal_flagged(fake_redis, "backtest", "bt-flag-lifecycle")

        # Commit transaction
        seeded_session.commit()
        assert not is_job_terminal_flagged(fake_redis, "backtest", "bt-flag-lifecycle")
    finally:
        set_publisher_client(None)


def test_record_job_terminal_does_not_invent_topic_state(db_session: Session):
    """The migration seeds the epoch; inventing a fixed one would repeat across a reset."""
    from q_backend.storage.db.outbox_models import OutboxTopicState
    from q_backend.streaming.jobs import record_job_terminal

    db_session.delete(db_session.get(OutboxTopicState, "jobs.terminal"))
    db_session.commit()

    with pytest.raises(RuntimeError, match="no initialized state"):
        record_job_terminal(db_session, kind="backtest", job_id="bt-unseeded", raw_status="completed")

    assert db_session.get(OutboxTopicState, "jobs.terminal") is None
