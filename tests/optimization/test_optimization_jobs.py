import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import numpy as np
import fakeredis
import pytest

from q_backend.api import optimization_jobs
from q_backend.storage.redis.progress import get_job_progress
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
)
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.tick_backtest_runner import TickBacktestRunner


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


@pytest.fixture(autouse=True)
def clear_jobs():
    optimization_jobs._jobs.clear()
    yield
    optimization_jobs._jobs.clear()


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


def _wait_for(study_id: str, statuses: set[str], timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = optimization_jobs.get_job(study_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {study_id} did not reach {statuses} in time")


def test_start_job_completes_and_reports_progress():
    stub = StubBacktestRunner()
    job = optimization_jobs.start_job(_config(n_trials=5), backtest_runner=stub)

    done = _wait_for(job.study_id, {"done"})

    assert done.completed_trials == 5
    assert stub.call_count == 5
    assert done.best_params  # best params populated for single-objective study

    payload = optimization_jobs.results_payload(done)
    assert payload is not None
    assert len(payload["trials"]) == 5
    assert payload["is_multi_objective"] is False


def test_unknown_study_returns_none():
    assert optimization_jobs.get_job("does-not-exist") is None
    assert optimization_jobs.request_cancel("does-not-exist") is None


def test_results_payload_none_before_completion():
    config = _config(n_trials=1)
    job = optimization_jobs.OptimizationJob(
        study_id="pending-job",
        config=config,
        n_trials=1,
    )
    assert optimization_jobs.results_payload(job) is None
    assert job.status == "pending"


def test_start_job_requires_market_data_service_without_runner():
    with pytest.raises(ValueError, match="market_data_service is required"):
        optimization_jobs.start_job(_config(n_trials=1))


def test_start_job_constructs_tick_runner_for_tick_engine(monkeypatch):
    captured: dict[str, object] = {}

    def _submit(fn, job, runner):
        captured["runner"] = runner

    monkeypatch.setattr(optimization_jobs._executor, "submit", _submit)

    service = MagicMock()
    service.get_ticks_columnar.return_value = {
        "time_msc": np.array([1_700_000_000_000], dtype=np.int64),
        "bid": np.array([10.0]),
        "ask": np.array([10.02]),
        "last": np.array([10.01]),
        "volume": np.array([1.0]),
    }

    config = OptimizationConfig.model_validate(
        {
            "study": {
                "name": "tick_job_test",
                "n_trials": 1,
                "seed": 1,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "start": "2024-01-01T00:00:00",
                "end": "2024-02-01T00:00:00",
                "strategy": "TickMaBreakout",
                "engine": "tick",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 3},
                    "long_period": {"type": "int", "low": 4, "high": 5},
                }
            },
        }
    )

    optimization_jobs.start_job(config, market_data_service=service)

    assert isinstance(captured["runner"], TickBacktestRunner)
    service.get_ticks_columnar.assert_called_once()


def test_redis_progress_keyed_by_study_id(monkeypatch):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(optimization_jobs, "get_redis", lambda: redis_client)

    stub = StubBacktestRunner()
    job = optimization_jobs.start_job(_config(n_trials=3), backtest_runner=stub)
    _wait_for(job.study_id, {"done"})

    deadline = time.time() + 2.0
    cached = None
    while time.time() < deadline:
        cached = get_job_progress(redis_client, job.study_id)
        if cached is not None and cached["status"] == "done":
            break
        time.sleep(0.02)

    assert cached is not None
    assert cached["study_id"] == job.study_id
    assert cached["status"] == "done"
    assert cached["completed_trials"] == 3
    assert "trials" not in cached


def test_get_status_payload_backward_compatible_shape(monkeypatch):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(optimization_jobs, "get_redis", lambda: redis_client)

    stub = StubBacktestRunner()
    job = optimization_jobs.start_job(_config(n_trials=2), backtest_runner=stub)
    _wait_for(job.study_id, {"done"})

    payload = optimization_jobs.get_status_payload(job.study_id)
    assert optimization_jobs.STATUS_PAYLOAD_KEYS.issubset(payload.keys())


def test_redis_failure_falls_back_to_memory(monkeypatch):
    def raise_redis(*_args, **_kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(optimization_jobs, "set_job_progress", raise_redis)
    monkeypatch.setattr(optimization_jobs, "get_job_progress", raise_redis)

    stub = StubBacktestRunner()
    job = optimization_jobs.start_job(_config(n_trials=3), backtest_runner=stub)
    done = _wait_for(job.study_id, {"done"})

    payload = optimization_jobs.get_status_payload(job.study_id)
    assert payload["study_id"] == done.study_id
    assert payload["status"] == "done"
    assert payload["completed_trials"] == 3

    results = optimization_jobs.results_payload(done)
    assert results is not None
    assert len(results["trials"]) == 3
