"""Cancelling a backtest run: the API request, the worker checkpoints and the final state."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backtest_test_helpers import mock_worker_market_service
from q_backend.api import backtest_jobs
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers import backtest as backtest_router
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.models import BacktestRun
from q_backend.storage.db.outbox_models import OutboxEvent
from q_backend.tasks import actors, fanin

REQUEST_BODY = {
    "symbol": "WIN$",
    "timeframe": "M5",
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-02-01T00:00:00Z",
    "initial_capital": 100000.0,
    "point_value": 0.2,
    "strategy": "MACrossover",
    "strategy_params": {"short_period": 5, "long_period": 10},
}


@pytest.fixture
def sample_ohlcv():
    base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    return [
        OHLCV(
            time=base + timedelta(days=i),
            open=99.0 + (i % 5),
            high=102.0 + (i % 5),
            low=98.0 + (i % 5),
            close=100.0 + (i % 5),
            tick_volume=1000,
        )
        for i in range(30)
    ]


@pytest.fixture
def worker_service(sample_ohlcv):
    service = mock_worker_market_service(ohlcv=sample_ohlcv)
    with patch("q_backend.tasks.worker_context.get_worker_market_data_service", return_value=service):
        yield service


@pytest.fixture
def queued(run_jobs_sync, monkeypatch, worker_service):
    """Start runs without executing them; the test decides when the worker picks one up."""
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        actors.run_backtest, "send", lambda run_id, request_json: messages.append((run_id, request_json))
    )
    return messages


def _start() -> str:
    return backtest_jobs.start_job(BacktestJobRequest.model_validate(REQUEST_BODY))


def _row(run_jobs_sync, run_id: str) -> BacktestRun:
    with run_jobs_sync.harness_session_factory() as session:
        return session.get(BacktestRun, uuid.UUID(run_id))


def _terminal_events(run_jobs_sync, run_id: str) -> list[OutboxEvent]:
    with run_jobs_sync.harness_session_factory() as session:
        return list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.topic == "jobs.terminal",
                    OutboxEvent.producer_id == f"job-backtest-{run_id}",
                )
            )
        )


def test_cancelling_a_queued_run_marks_it_cancelled_and_the_worker_skips_it(run_jobs_sync, queued, worker_service):
    run_id = _start()

    status = backtest_jobs.request_cancel(run_id)

    assert status == {"run_id": run_id, "status": "cancelled", "error": backtest_jobs.CANCELLED_MESSAGE}
    assert _row(run_jobs_sync, run_id).finished_at is not None
    assert [e.payload["status"] for e in _terminal_events(run_jobs_sync, run_id)] == ["cancelled"]

    (message,) = queued
    backtest_jobs.run_backtest_job(*message)

    worker_service.get_ohlcv.assert_not_called()
    assert backtest_jobs.get_status_payload(run_id)["status"] == "cancelled"
    assert len(_terminal_events(run_jobs_sync, run_id)) == 1


def test_cancelling_during_the_simulation_stops_the_worker_before_saving(run_jobs_sync, queued, worker_service):
    run_id = _start()
    (message,) = queued
    original = worker_service.get_ohlcv.return_value

    def cancel_while_loading(*_args, **_kwargs):
        backtest_jobs.request_cancel(run_id)
        return original

    worker_service.get_ohlcv.side_effect = cancel_while_loading

    with patch.object(backtest_jobs, "write_backtest_result") as write_result:
        backtest_jobs.run_backtest_job(*message)

    write_result.assert_not_called()
    row = _row(run_jobs_sync, run_id)
    assert row.status == "cancelled"
    assert row.result_summary is None
    assert row.error_message == backtest_jobs.CANCELLED_MESSAGE
    assert len(_terminal_events(run_jobs_sync, run_id)) == 1


def test_a_cancel_that_lands_after_the_last_checkpoint_is_not_overwritten(run_jobs_sync, queued, worker_service):
    run_id = _start()
    backtest_jobs.request_cancel(run_id)

    backtest_jobs._finish_run(run_id, status="completed", result_summary={"total_pnl": 1.0})

    row = _row(run_jobs_sync, run_id)
    assert row.status == "cancelled"
    assert row.result_summary is None


def test_cancelling_a_finished_run_returns_its_final_status(run_jobs_sync, queued, worker_service):
    run_id = _start()
    backtest_jobs.run_backtest_job(*queued[0])
    assert backtest_jobs.get_status_payload(run_id)["status"] == "completed"

    status = backtest_jobs.request_cancel(run_id)

    assert status["status"] == "completed"
    assert _row(run_jobs_sync, run_id).status == "completed"
    assert not fanin.is_cancelled(run_id)


def test_an_unknown_or_malformed_run_id_is_not_found(run_jobs_sync):
    assert backtest_jobs.request_cancel(str(uuid.uuid4())) is None
    assert backtest_jobs.request_cancel("not-a-uuid") is None
    with pytest.raises(backtest_router.HTTPException) as caught:
        backtest_router.cancel_backtest(str(uuid.uuid4()))
    assert caught.value.status_code == 404


def test_rerunning_a_cancelled_config_is_not_cancelled_by_the_stale_flag(run_jobs_sync, queued, worker_service):
    first = _start()
    backtest_jobs.request_cancel(first)

    second = _start()
    assert second == first  # the run row is reused for an identical config
    backtest_jobs.run_backtest_job(*queued[-1])

    assert backtest_jobs.get_status_payload(second)["status"] == "completed"
    assert _row(run_jobs_sync, second).error_message is None
