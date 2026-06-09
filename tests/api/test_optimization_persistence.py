import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api import optimization_jobs
from q_backend.api.main import (
    delete_optimization,
    get_optimization_results,
    get_optimization_status,
    list_optimizations,
)
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
)
from q_backend.optimization.models import OptimizationConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_optimization_study


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
    optimization_jobs._jobs.clear()
    yield
    optimization_jobs._jobs.clear()


@dataclass
class StubBacktestRunner:
    call_count: int = 0
    calls: list = field(default_factory=list)

    def run(self, config: BacktestRunConfig) -> BacktestRunResult:
        self.call_count += 1
        self.calls.append(config)
        short_period = int(config.strategy_params.get("short_period", 1))
        total_pnl = float(short_period * 100)
        max_dd = max(0.01, 0.1 / short_period)
        return BacktestRunResult(
            metrics={
                "total_trades": 3,
                "total_pnl": total_pnl,
                "max_drawdown_pct": max_dd,
                "total_return_pct": total_pnl / config.initial_capital,
                "sharpe_ratio": short_period / 10.0,
                "return_drawdown_ratio": (total_pnl / config.initial_capital) / max_dd,
            }
        )


def _config(n_trials: int = 3) -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {
                "name": "WIN$ MA sweep",
                "n_trials": n_trials,
                "seed": 42,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "WIN$",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
                "strategy": "MACrossover",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 8},
                    "long_period": {"type": "int", "low": 20, "high": 40},
                },
                "risk_params": {
                    "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                    "quantity": {"type": "float", "low": 1.0, "high": 3.0},
                },
            },
        }
    )


def _wait_for(study_id: str, statuses: set[str], timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = optimization_jobs.get_job(study_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {study_id} did not reach {statuses} in time")


def _start_persisted_job(api_session_scope, n_trials: int = 3):
    stub = StubBacktestRunner()
    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        job = optimization_jobs.start_job(_config(n_trials=n_trials), backtest_runner=stub)
        done = _wait_for(job.study_id, {"done"})
    return job, done, stub


def test_optimization_persists_study_and_trials(api_db_session, api_session_scope):
    job, done, stub = _start_persisted_job(api_session_scope, n_trials=3)
    api_db_session.expire_all()

    assert re.fullmatch(r"[0-9a-f]{32}", job.study_id)
    assert done.completed_trials == 3
    assert stub.call_count == 3

    study_uuid = uuid.UUID(hex=job.study_id)
    study = get_optimization_study(api_db_session, study_uuid)
    assert study is not None
    assert study.name == "WIN$ MA sweep"
    assert study.status == "done"
    assert len(study.trials) == 3


def test_optimization_results_rebuild_after_restart(api_db_session, api_session_scope):
    job, done, _stub = _start_persisted_job(api_session_scope, n_trials=3)

    in_memory = optimization_jobs.results_payload(done)
    optimization_jobs._jobs.clear()

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        rebuilt = get_optimization_results(job.study_id)

    assert rebuilt["study_id"] == job.study_id
    assert len(rebuilt["trials"]) == len(in_memory["trials"])
    assert rebuilt["best_params"] == in_memory["best_params"]
    assert rebuilt["objective_mode"] == in_memory["objective_mode"]


def test_list_optimizations_returns_persisted_studies(api_db_session, api_session_scope):
    job, _done, _stub = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    list_payload = list_optimizations(
        session=api_db_session, limit=50, offset=0
    )
    assert list_payload["total"] == 1
    assert len(list_payload["items"]) == 1
    item = list_payload["items"][0]
    assert item.study_id == job.study_id
    assert item.name == "WIN$ MA sweep"
    assert item.status == "done"
    assert item.n_trials == 2
    assert item.completed_trials == 2
    assert item.best_value is not None


def test_optimization_status_rebuild_after_restart(api_session_scope):
    job, done, _stub = _start_persisted_job(api_session_scope, n_trials=2)
    optimization_jobs._jobs.clear()

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        status = get_optimization_status(job.study_id)

    assert status["study_id"] == job.study_id
    assert status["status"] == "done"
    assert status["completed_trials"] == 2
    assert status["optimization_config"] is not None
    assert status["optimization_config"]["backtest"]["symbol"] == "WIN$"
    assert status["backtest_config"]["symbol"] == "WIN$"


def test_delete_optimization_removes_study_from_history(
    api_db_session, api_session_scope
):
    job, _done, _stub = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    delete_optimization(job.study_id, session=api_db_session)

    list_payload = list_optimizations(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0
    assert list_payload["items"] == []
    assert optimization_jobs.get_job(job.study_id) is None


def test_optimization_graceful_degradation_when_persistence_unavailable():
    stub = StubBacktestRunner()
    with patch(
        "q_backend.api.optimization_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = optimization_jobs.start_job(_config(n_trials=2), backtest_runner=stub)

    assert re.fullmatch(r"[0-9a-f]{32}", job.study_id)
    done = _wait_for(job.study_id, {"done"})
    assert done.db_study_id is None
    assert optimization_jobs.results_payload(done) is not None
