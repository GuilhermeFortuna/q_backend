"""Worker heartbeat, stopped marker, and readiness callback ordering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.edge_client import EdgeUnavailable
from q_backend.execution.worker import ExecutionWorker, RecoveryFailedClosed
from q_backend.storage.db.execution_repositories import get_worker_heartbeat
from q_backend.storage.settings import Settings

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _worker(db_engine, clock, *, edge_health=None, failed_closed=False, **kwargs) -> ExecutionWorker:
    recovery = MagicMock()
    recovery.recover.return_value = SimpleNamespace(
        failed_closed=failed_closed, message="quote unavailable", leases_released=0, unknown_orders_marked=0
    )
    return ExecutionWorker(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False),
        coordinator=MagicMock(),
        quote_source=MagicMock(),
        broker=MagicMock(),
        ledger=MagicMock(),
        service=MagicMock(),
        recovery=recovery,
        settings=Settings(
            execution_worker_id="w-test",
            execution_edge_health_interval_s=5.0,
        ),
        clock=clock.now,
        poll_interval_seconds=0.0,
        edge_health=edge_health,
        version="9.9",
        **kwargs,
    )


def _row(db_engine):
    with sessionmaker(bind=db_engine)() as session:
        row = get_worker_heartbeat(session, "w-test")
        session.expunge_all()
        return row


def test_poll_records_heartbeat_with_edge_status(db_engine):
    clock = FixedClock(T0)
    edge = MagicMock(return_value=SimpleNamespace(mt5_connected=True, terminal_build=4500))
    worker = _worker(db_engine, clock, edge_health=edge)
    worker._started_at = T0
    worker.poll_once()
    row = _row(db_engine)
    assert row.version == "9.9"
    assert row.edge_reachable is True and row.edge_mt5_connected is True and row.edge_terminal_build == 4500
    assert row.stopped_at is None


def test_edge_check_is_rate_limited_and_detects_outage(db_engine):
    clock = FixedClock(T0)
    edge = MagicMock(return_value=SimpleNamespace(mt5_connected=True, terminal_build=1))
    worker = _worker(db_engine, clock, edge_health=edge)
    worker.poll_once()
    clock.advance(1)
    worker.poll_once()
    assert edge.call_count == 1

    edge.side_effect = EdgeUnavailable("down")
    clock.advance(5)
    worker.poll_once()
    assert edge.call_count == 2
    row = _row(db_engine)
    assert row.edge_reachable is False and row.edge_terminal_build is None


def test_run_calls_ready_once_after_first_poll_then_on_poll_and_records_stop(db_engine):
    clock = FixedClock(T0)
    events: list[str] = []
    worker = _worker(db_engine, clock, on_ready=lambda: events.append("ready"))
    worker.on_poll = lambda: (events.append("poll"), len(events) > 4 and worker.request_shutdown())
    worker.recovery.recover.side_effect = lambda *a, **k: (
        events.append("recovered"),
        SimpleNamespace(failed_closed=False, message="", leases_released=0, unknown_orders_marked=0),
    )[1]
    worker.run()
    assert events[:3] == ["recovered", "ready", "poll"]
    assert events.count("ready") == 1
    assert _row(db_engine).stopped_at is not None


def test_failed_closed_recovery_raises_without_ready(db_engine):
    events: list[str] = []
    worker = _worker(db_engine, FixedClock(T0), failed_closed=True, on_ready=lambda: events.append("ready"))
    with pytest.raises(RecoveryFailedClosed):
        worker.run()
    assert events == []
