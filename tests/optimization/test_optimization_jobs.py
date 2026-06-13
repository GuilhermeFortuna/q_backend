"""Optimization job-manager tests against the Dramatiq dispatch model.

The ``run_jobs_sync`` fixture (see ``tests/conftest.py``) runs the coordinator →
trial-worker → finalizer chain in-process, so ``start_job`` returns only once the
whole distributed study has completed. This exercises the real flow end to end:
multiple trial workers collaborating on one shared (temp-SQLite) Optuna study, with
the engine running on synthetic OHLCV.
"""

import re
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api import optimization_jobs
from q_backend.optimization.models import OptimizationConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_optimization_study
from q_backend.tasks.fanin import is_cancelled


@pytest.fixture
def db_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield scope
    Base.metadata.drop_all(engine)
    engine.dispose()


def _config(n_trials: int = 5) -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {
                "name": "job_test",
                "n_trials": n_trials,
                "seed": 42,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
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


def test_dispatch_runs_study_to_completion(run_jobs_sync, db_scope):
    with patch("q_backend.api.optimization_jobs.session_scope", db_scope):
        job = optimization_jobs.start_job(_config(n_trials=5))
        payload = optimization_jobs.get_status_payload(job.study_id)

    assert re.fullmatch(r"[0-9a-f]{32}", job.study_id)
    assert payload["status"] == "done"
    # Every trial reaches a finished state (completed or pruned).
    assert payload["completed_trials"] == 5

    # The study and its trials were persisted across the worker chain.
    with db_scope() as session:
        study = get_optimization_study(session, uuid.UUID(hex=job.study_id))
        assert study is not None
        assert study.status == "done"
        assert len(study.trials) == 5


def test_get_job_is_stateless():
    # Studies no longer live in the API process.
    assert optimization_jobs.get_job("does-not-exist") is None


def test_request_cancel_sets_flag(run_jobs_sync):
    optimization_jobs.request_cancel("study-abc")
    assert is_cancelled("study-abc")


def test_results_payload_none_before_completion():
    job = optimization_jobs.OptimizationJob(
        study_id="pending-study",
        config=_config(n_trials=1),
        n_trials=1,
    )
    assert optimization_jobs.results_payload(job) is None
