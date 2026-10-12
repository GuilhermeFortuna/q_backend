"""Progress reports of a running backtest: the reporter's rules and the job's phase sequence."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backtest_test_helpers import run_async_backtest
from q_backend.api import backtest_jobs
from q_backend.api.backtest_progress import REPLAY_REPORT_INTERVAL_SECONDS, BacktestProgressReporter
from q_backend.api.schemas.backtest import BacktestStatusResponse
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base

STARTED = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
CANDLE_TIME = pd.Timestamp("2024-01-02 10:00")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return STARTED + timedelta(seconds=self.now)


def make_reporter(clock: FakeClock | None = None) -> tuple[BacktestProgressReporter, list[dict], FakeClock]:
    clock = clock or FakeClock()
    published: list[dict] = []
    reporter = BacktestProgressReporter(
        "run-1",
        started_at=STARTED,
        publish=published.append,
        clock=clock,
        wall_clock=clock.wall,
    )
    return reporter, published, clock


def test_queued_phase_carries_the_start_time_and_null_progress():
    reporter, published, _ = make_reporter()

    reporter.phase("queued", "Waiting for a worker")

    assert published == [
        {
            "run_id": "run-1",
            "status": "running",
            "error": None,
            "phase": "queued",
            "progress": None,
            "message": "Waiting for a worker",
            "started_at": STARTED.isoformat(),
            "updated_at": STARTED.isoformat(),
            "simulated_until": None,
            "bars_total": None,
            "candles_replayed": 0,
            "candles_substituted": 0,
            "activity": None,
            "phase_history": [{"phase": "queued", "started_at": STARTED.isoformat(), "finished_at": None}],
        }
    ]


def test_replay_reports_are_throttled_to_one_per_interval():
    reporter, published, clock = make_reporter()
    reporter.phase("simulating", "Simulating 10 candles", bars_total=10)

    clock.now = 0.1
    reporter.replay(1, 10, CANDLE_TIME, replayed=2, substituted=0)  # within the interval: not published
    clock.now = REPLAY_REPORT_INTERVAL_SECONDS
    reporter.replay(2, 10, CANDLE_TIME, replayed=3, substituted=0)  # interval elapsed: published
    clock.now = 0.7
    reporter.replay(3, 10, CANDLE_TIME, replayed=4, substituted=0)  # within the interval: not published
    clock.now = 1.0
    reporter.replay(4, 10, CANDLE_TIME, replayed=5, substituted=1)  # published with the latest state

    assert [payload["progress"] for payload in published] == [None, 0.3, 0.5]
    assert published[-1]["candles_replayed"] == 5
    assert published[-1]["candles_substituted"] == 1


def test_a_phase_change_is_published_inside_the_throttle_window():
    reporter, published, clock = make_reporter()
    reporter.phase("simulating", "Simulating 10 candles", bars_total=10)
    clock.now = 0.1
    reporter.replay(1, 10, CANDLE_TIME, replayed=2, substituted=0)
    clock.now = 0.2

    reporter.phase("saving", "Saving results")

    assert [payload["phase"] for payload in published] == ["simulating", "saving"]
    assert published[-1]["progress"] is None
    assert published[-1]["message"] == "Saving results"


def test_progress_never_decreases_within_a_run():
    reporter, published, clock = make_reporter()
    reporter.phase("simulating", "Simulating 10 candles", bars_total=10)
    clock.now = 1.0
    reporter.replay(5, 10, CANDLE_TIME, replayed=6, substituted=0)
    clock.now = 2.0
    reporter.replay(3, 10, CANDLE_TIME, replayed=7, substituted=0)

    assert published[-1]["progress"] == 0.6
    assert published[-1]["candles_replayed"] == 7


def test_every_report_keeps_the_start_time_the_api_stored():
    reporter, published, clock = make_reporter()
    reporter.phase("queued", "Waiting for a worker")
    clock.now = 3.0
    reporter.phase("loading_data", "Loading H1 candles for TEST")

    assert {payload["started_at"] for payload in published} == {STARTED.isoformat()}
    assert published[-1]["updated_at"] == (STARTED + timedelta(seconds=3)).isoformat()


def test_a_silent_reporter_accepts_every_call_without_publishing():
    reporter = BacktestProgressReporter.silent()

    reporter.phase("simulating", "Simulating 10 candles", bars_total=10)
    reporter.replay(1, 10, CANDLE_TIME, replayed=2, substituted=0)


def test_read_boundaries_do_not_claim_pending_history_is_completed():
    reporter, published, clock = make_reporter()
    reporter.phase("simulating", "Simulating 10 candles", bars_total=10)
    clock.now = 1.0
    with reporter.read("reading_ticks", STARTED, STARTED + timedelta(hours=1)):
        assert published[-1]["activity"]["kind"] == "reading_ticks"
        assert published[-1]["activity"]["range_start"] == STARTED.isoformat()
        assert published[-1]["simulated_until"] is None
        assert published[-1]["candles_replayed"] == 0
        clock.now = 20.0
    assert published[-1]["activity"]["kind"] == "computing"
    reporter.replay(0, 10, CANDLE_TIME, replayed=1, substituted=0)
    clock.now = 21.0
    reporter.replay(0, 10, CANDLE_TIME, replayed=1, substituted=0)
    assert published[-1]["progress"] == 0.1


def test_read_failure_preserves_last_completed_replay():
    reporter, published, clock = make_reporter()
    reporter.phase("simulating", "Simulating 10 candles", bars_total=10)
    clock.now = 1.0
    reporter.replay(0, 10, CANDLE_TIME, replayed=1, substituted=0)
    with pytest.raises(ConnectionError):
        with reporter.read("reading_m1", STARTED, STARTED + timedelta(hours=1)):
            raise ConnectionError("provider unavailable")
    assert published[-1]["activity"]["kind"] == "reading_m1"
    assert published[-1]["candles_replayed"] == 1
    assert published[-1]["simulated_until"] == CANDLE_TIME.isoformat()


def test_phase_history_includes_queue_time_and_completed_phase_durations():
    reporter, published, clock = make_reporter()
    clock.now = 8.0
    reporter.phase("loading_data", "Loading candles")
    clock.now = 20.0
    reporter.phase("preparing", "Preparing strategy")
    assert published[-1]["phase_history"] == [
        {
            "phase": "queued",
            "started_at": STARTED.isoformat(),
            "finished_at": (STARTED + timedelta(seconds=8)).isoformat(),
        },
        {
            "phase": "loading_data",
            "started_at": (STARTED + timedelta(seconds=8)).isoformat(),
            "finished_at": (STARTED + timedelta(seconds=20)).isoformat(),
        },
        {"phase": "preparing", "started_at": (STARTED + timedelta(seconds=20)).isoformat(), "finished_at": None},
    ]


def test_progress_publication_failure_does_not_fail_a_read():
    def unavailable(payload):
        raise ConnectionError("redis unavailable")

    reporter = BacktestProgressReporter("run-1", started_at=STARTED, publish=unavailable)
    reporter.phase("loading_data", "Loading candles")
    with reporter.read("loading_candles", STARTED, STARTED + timedelta(days=1)):
        pass


def test_status_without_progress_fields_validates_with_them_null():
    status = BacktestStatusResponse.model_validate({"run_id": "run-1", "status": "completed", "error": None})

    assert status.phase is None
    assert status.progress is None
    assert status.started_at is None
    assert status.candles_replayed is None


def test_status_rejects_a_phase_outside_the_closed_set():
    with pytest.raises(ValidationError):
        BacktestStatusResponse.model_validate({"run_id": "run-1", "status": "running", "phase": "warming"})


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
def api_session_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    @contextmanager
    def test_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield test_session_scope
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def sample_ohlcv():
    base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(30):
        close = 100.0 + (i % 5)
        bars.append(
            OHLCV(
                time=base + timedelta(days=i),
                open=close - 1,
                high=close + 2,
                low=close - 2,
                close=close,
                tick_volume=1000,
            )
        )
    return bars


@pytest.fixture
def published_progress(monkeypatch):
    """Record every payload the job layer publishes, still writing through to the fake Redis."""
    published: list[dict] = []
    original = backtest_jobs._publish_progress

    def record(run_id, payload):
        published.append(dict(payload))
        original(run_id, payload)

    monkeypatch.setattr(backtest_jobs, "_publish_progress", record)
    return published


def test_candle_run_reports_its_phases_in_order_before_completion(
    run_jobs_sync, api_session_scope, sample_ohlcv, published_progress
):
    run_id, _ = run_async_backtest(REQUEST_BODY, api_session_scope=api_session_scope, sample_ohlcv=sample_ohlcv)

    phases = [payload.get("phase") for payload in published_progress]
    assert [phase for index, phase in enumerate(phases) if index == 0 or phase != phases[index - 1]] == [
        "queued",
        "loading_data",
        "preparing",
        "simulating",
        "saving",
        None,
    ]
    assert published_progress[-1] == {"run_id": run_id, "status": "completed", "error": None}
    simulating = next(payload for payload in published_progress if payload.get("phase") == "simulating")
    assert simulating["progress"] is None
    assert simulating["bars_total"] == len(sample_ohlcv)
    assert simulating["message"] == f"Simulating {len(sample_ohlcv)} candles"


def test_a_failing_progress_store_leaves_the_run_result_unchanged(
    run_jobs_sync, api_session_scope, sample_ohlcv, monkeypatch
):
    _, healthy = run_async_backtest(REQUEST_BODY, api_session_scope=api_session_scope, sample_ohlcv=sample_ohlcv)

    def unavailable():
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(backtest_jobs, "get_redis", unavailable)
    run_id, degraded = run_async_backtest(REQUEST_BODY, api_session_scope=api_session_scope, sample_ohlcv=sample_ohlcv)

    assert run_id is not None
    assert degraded["metrics"] == healthy["metrics"]
    assert degraded["trades"] == healthy["trades"]


def test_status_endpoint_preserves_activity_and_phase_history(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from q_backend.api.routers.backtest import router

    reporter, published, _ = make_reporter()
    reporter.phase("loading_data", "Loading candles")
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(backtest_jobs, "get_status_payload", lambda run_id: published[-1])
    with reporter.read("loading_candles", STARTED, STARTED + timedelta(days=1)):
        response = TestClient(app).get("/api/v1/backtest/run-1")
    assert response.status_code == 200
    assert response.json()["activity"]["kind"] == "loading_candles"
    assert [entry["phase"] for entry in response.json()["phase_history"]] == ["queued", "loading_data"]
