import time
from dataclasses import dataclass, field

from q_backend.api import optimization_jobs
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
)
from q_backend.optimization.models import OptimizationConfig


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
    import pytest

    with pytest.raises(ValueError, match="market_data_service is required"):
        optimization_jobs.start_job(_config(n_trials=1))
