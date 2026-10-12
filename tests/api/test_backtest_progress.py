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

    assert [payload["progress"] for payload in published] == [None, 0.2, 0.4]
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

    assert published[-1]["progress"] == 0.5
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

    assert [payload.get("phase") for payload in published_progress] == [
        "queued",
        "loading_data",
        "preparing",
        "simulating",
        "saving",
        None,
    ]
    assert published_progress[-1] == {"run_id": run_id, "status": "completed", "error": None}
    simulating = published_progress[3]
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
