import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api import walkforward_jobs
from q_backend.api.main import (
    cancel_walkforward,
    delete_walkforward,
    get_walkforward_equity_artifact,
    get_walkforward_results,
    get_walkforward_status,
    list_walkforwards,
    start_walkforward,
)
from q_backend.api.walkforward_jobs import WalkForwardRequest
from q_backend.optimization.backtest_runner import BacktestRunConfig, DefaultBacktestRunner
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.runner import OptimizationRunner
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_walkforward_run
from q_backend.storage.lake.artifacts import lake_root
from q_backend.storage.settings import get_settings


@pytest.fixture
def api_db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def api_session_factory(api_db_engine):
    return sessionmaker(
        bind=api_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def api_session_scope(api_session_factory):
    @contextmanager
    def test_session_scope():
        session = api_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return test_session_scope


@pytest.fixture
def api_db_session(api_session_factory) -> Session:
    session = api_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clear_jobs():
    walkforward_jobs._jobs.clear()
    yield
    walkforward_jobs._jobs.clear()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _make_intraday_ohlcv_df(start: datetime, days: int) -> pd.DataFrame:
    rows = []
    price = 100.0
    for day in range(days):
        for hour in (0, 6, 12, 18):
            timestamp = start + timedelta(days=day, hours=hour)
            drift = 0.1 if day % 10 < 5 else -0.05
            price = max(50.0, price + drift)
            rows.append(
                {
                    "time": timestamp,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 1000,
                }
            )
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def _sliced_data_provider(full_df: pd.DataFrame):
    def data_provider(config: BacktestRunConfig) -> pd.DataFrame:
        return full_df.loc[config.start : config.end]

    return data_provider


def _request(*, n_trials: int = 2) -> WalkForwardRequest:
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 30)
    return WalkForwardRequest.model_validate(
        {
            "optimization": {
                "study": {
                    "name": "WF WIN$ MA",
                    "n_trials": n_trials,
                    "seed": 42,
                    "storage": {"type": "memory"},
                },
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "timeframe": "D1",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "initial_capital": 10_000.0,
                    "point_value": 1.0,
                    "strategy": "MACrossover",
                },
                "search_space": {
                    "strategy_params": {
                        "short_period": {"type": "int", "low": 2, "high": 4},
                        "long_period": {"type": "int", "low": 6, "high": 8},
                    },
                    "risk_params": {
                        "type": {
                            "type": "categorical",
                            "choices": ["fixed_quantity"],
                        },
                        "quantity": {"type": "float", "low": 1.0, "high": 1.0},
                    },
                },
            },
            "walkforward": {
                "train_days": 30,
                "test_days": 15,
                "mode": "rolling",
                "min_windows": 2,
            },
        }
    )


def _backtest_runner() -> DefaultBacktestRunner:
    start = datetime(2024, 1, 1)
    full_df = _make_intraday_ohlcv_df(start, 120)
    return DefaultBacktestRunner(data_provider=_sliced_data_provider(full_df))


def _wait_for(run_id: str, statuses: set[str], timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = walkforward_jobs.get_job(run_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"walk-forward run {run_id} did not reach {statuses} in time")


def _start_persisted_job(api_session_scope, *, n_trials: int = 2):
    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        job = walkforward_jobs.start_job(
            _request(n_trials=n_trials),
            backtest_runner=_backtest_runner(),
        )
        done = _wait_for(job.run_id, {"completed"})
    return job, done


def test_walkforward_end_to_end_persists_db_and_lake(
    api_db_session, api_session_scope, lake_root_path
):
    job, done = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    assert done.status == "completed"
    assert done.result is not None
    assert len(done.result.windows) >= 2

    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        results = get_walkforward_results(job.run_id)

    assert results["run_id"] == job.run_id
    assert len(results["windows"]) >= 2
    assert results["oos_metrics"]
    assert results["efficiency"] is not None
    assert len(results["equity_curve"]) >= 1

    run_uuid = uuid.UUID(hex=job.run_id)
    run = get_walkforward_run(api_db_session, run_uuid)
    assert run is not None
    assert run.name == "WF WIN$ MA"
    assert run.status == "completed"
    assert len(run.windows) >= 2
    assert run.lake_paths is not None
    assert (lake_root_path / run.lake_paths["oos_equity"]).is_file()
    assert (lake_root_path / run.lake_paths["oos_trades"]).is_file()
    assert (lake_root_path / run.lake_paths["windows"]).is_file()

    equity_payload = get_walkforward_equity_artifact(job.run_id)
    assert equity_payload["run_id"] == job.run_id
    assert len(equity_payload["points"]) >= 1


def test_walkforward_graceful_degradation_when_persistence_unavailable():
    with patch(
        "q_backend.api.walkforward_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = walkforward_jobs.start_job(
            _request(n_trials=2),
            backtest_runner=_backtest_runner(),
        )

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    done = _wait_for(job.run_id, {"completed"})
    assert done.db_run_id is None
    assert walkforward_jobs.results_payload(done) is not None


def test_walkforward_status_rebuild_after_restart(api_session_scope):
    job, _done = _start_persisted_job(api_session_scope, n_trials=2)
    walkforward_jobs._jobs.clear()

    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        status = get_walkforward_status(job.run_id)

    assert status["run_id"] == job.run_id
    assert status["status"] == "completed"
    assert status["optimization_config"] is not None
    assert status["walkforward_config"] is not None
    assert status["backtest_config"]["symbol"] == "WIN$"


def test_walkforward_results_rebuild_after_restart(api_session_scope):
    job, done = _start_persisted_job(api_session_scope, n_trials=2)
    in_memory = walkforward_jobs.results_payload(done)
    walkforward_jobs._jobs.clear()

    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        rebuilt = get_walkforward_results(job.run_id)

    assert rebuilt["run_id"] == job.run_id
    assert len(rebuilt["windows"]) == len(in_memory["windows"])
    assert rebuilt["efficiency"] == in_memory["efficiency"]


def test_walkforward_cancel_between_windows(api_session_scope):
    original_run = OptimizationRunner.run

    def slow_run(self, callbacks=None):
        time.sleep(0.25)
        return original_run(self, callbacks=callbacks)

    with (
        patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope),
        patch.object(OptimizationRunner, "run", slow_run),
    ):
        job = walkforward_jobs.start_job(
            _request(n_trials=2),
            backtest_runner=_backtest_runner(),
        )
        walkforward_jobs.request_cancel(job.run_id)
        finished = _wait_for(job.run_id, {"cancelled"})

    assert finished.status == "cancelled"
    assert finished.result is not None
    assert len(finished.result.windows) < finished.total_windows


def test_list_and_delete_walkforward(api_db_session, api_session_scope, lake_root_path):
    job, _done = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    list_payload = list_walkforwards(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert list_payload["items"][0].run_id == job.run_id

    delete_walkforward(job.run_id, session=api_db_session)

    list_payload = list_walkforwards(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0
    assert walkforward_jobs.get_job(job.run_id) is None
    assert not (lake_root() / "walkforward" / job.run_id).exists()


def test_start_walkforward_returns_422_for_short_range():
    request = WalkForwardRequest.model_validate(
        {
            "optimization": {
                "study": {"name": "bad", "n_trials": 1, "storage": {"type": "memory"}},
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-02-01T00:00:00",
                    "strategy": "MACrossover",
                },
                "search_space": {},
            },
            "walkforward": {
                "train_days": 30,
                "test_days": 30,
                "min_windows": 2,
            },
        }
    )

    with pytest.raises(HTTPException) as exc:
        start_walkforward(request)
    assert exc.value.status_code == 422


def test_start_walkforward_returns_422_for_tick_engine():
    request = WalkForwardRequest.model_validate(
        {
            "optimization": {
                "study": {"name": "tick", "n_trials": 1, "storage": {"type": "memory"}},
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-06-01T00:00:00",
                    "engine": "tick",
                },
                "search_space": {},
            },
            "walkforward": {"train_days": 10, "test_days": 5},
        }
    )

    with pytest.raises(HTTPException) as exc:
        start_walkforward(request)
    assert exc.value.status_code == 422
