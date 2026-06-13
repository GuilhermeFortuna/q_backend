from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from concurrent.futures import Future

from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.objectives import resolve_objective
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.runner import OptimizationRunner, _run_backtest_worker
from q_backend.optimization.walkforward import resolve_worker_count as wf_resolve_worker_count


@pytest.fixture
def inline_process_pool(monkeypatch):
    """Run pool tasks in-process so worker patches apply during tests."""

    class ImmediatePool:
        def __init__(self, max_workers=None, initializer=None, initargs=()):
            if initializer is not None:
                initializer(*initargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args, **kwargs):
            future: Future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

    monkeypatch.setattr(
        "q_backend.optimization.runner.ProcessPoolExecutor",
        ImmediatePool,
    )


@pytest.fixture
def naive_ohlcv_df():
    start = datetime(2024, 1, 1)
    rows = []
    price = 100.0
    for day in range(120):
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


def test_resolve_worker_count_from_parallel_module():
    import os

    assert resolve_worker_count(None, 8) == min(8, os.cpu_count() or 1)
    assert resolve_worker_count(2, 12) == 2
    assert resolve_worker_count(16, 3) == 3
    assert resolve_worker_count(None, 0) == 1


def test_resolve_worker_count_reexported_from_walkforward():
    assert wf_resolve_worker_count is resolve_worker_count


def _parallel_optimization_config(*, n_trials: int = 12) -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {
                "name": "parallel_compare",
                "n_trials": n_trials,
                "seed": 42,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "timeframe": "D1",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
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
        }
    )


def test_parallel_and_sequential_find_comparable_best_objective(naive_ohlcv_df):
    config = _parallel_optimization_config(n_trials=12)
    backtest_runner = DefaultBacktestRunner.from_frame_sliced(naive_ohlcv_df)

    seq_result = OptimizationRunner(config, backtest_runner).run()
    par_result = OptimizationRunner(
        config,
        backtest_runner,
        ohlcv=naive_ohlcv_df,
        max_workers=2,
    ).run()

    assert len(seq_result.study.trials) == 12
    assert len(par_result.study.trials) == 12
    assert seq_result.best_trial is not None
    assert par_result.best_trial is not None

    seq_best = float(
        resolve_objective(
            seq_result.best_trial.user_attrs["metrics"],
            config.objective.mode,
        )
    )
    par_best = float(
        resolve_objective(
            par_result.best_trial.user_attrs["metrics"],
            config.objective.mode,
        )
    )
    assert par_best >= seq_best - 1e-6

    best = par_result.best_trial
    assert "strategy_params" in best.user_attrs
    assert "risk_params" in best.user_attrs
    assert "metrics" in best.user_attrs
    assert best.user_attrs["status"] == "complete"


def test_parallel_without_frame_uses_sequential_path(naive_ohlcv_df):
    config = _parallel_optimization_config(n_trials=3)
    backtest_runner = DefaultBacktestRunner.from_frame_sliced(naive_ohlcv_df)

    with patch.object(OptimizationRunner, "_run_parallel") as parallel_mock:
        OptimizationRunner(config, backtest_runner).run()
        parallel_mock.assert_not_called()


def test_parallel_continue_on_trial_error_records_failures(
    naive_ohlcv_df, inline_process_pool
):
    config = _parallel_optimization_config(n_trials=4)
    config.study.continue_on_trial_error = True
    backtest_runner = DefaultBacktestRunner.from_frame_sliced(naive_ohlcv_df)

    call_count = 0

    def flaky_worker(cfg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"status": "error", "error": "worker blew up"}
        return _run_backtest_worker(cfg)

    with patch(
        "q_backend.optimization.runner._run_backtest_worker",
        side_effect=flaky_worker,
    ):
        result = OptimizationRunner(
            config,
            backtest_runner,
            ohlcv=naive_ohlcv_df,
            max_workers=2,
        ).run()

    assert len(result.failures) == 1
    assert result.failures[0]["error"] == "worker blew up"
    assert len(result.study.trials) == 4


def test_parallel_unexpected_error_reraises_without_continue(
    naive_ohlcv_df, inline_process_pool
):
    config = _parallel_optimization_config(n_trials=2)
    config.study.continue_on_trial_error = False
    backtest_runner = DefaultBacktestRunner.from_frame_sliced(naive_ohlcv_df)

    with patch(
        "q_backend.optimization.runner._run_backtest_worker",
        return_value={"status": "error", "error": "worker blew up"},
    ):
        with pytest.raises(RuntimeError, match="worker blew up"):
            OptimizationRunner(
                config,
                backtest_runner,
                ohlcv=naive_ohlcv_df,
                max_workers=2,
            ).run()


def test_parallel_should_stop_after_first_batch(naive_ohlcv_df):
    config = _parallel_optimization_config(n_trials=12)
    backtest_runner = DefaultBacktestRunner.from_frame_sliced(naive_ohlcv_df)
    completed_trials: list[int] = []

    def track_progress(_study, trial):
        completed_trials.append(trial.number)

    def should_stop() -> bool:
        return len(completed_trials) >= 2

    result = OptimizationRunner(
        config,
        backtest_runner,
        ohlcv=naive_ohlcv_df,
        max_workers=2,
    ).run(callbacks=[track_progress], should_stop=should_stop)

    assert len(result.study.trials) == 2


def test_parallel_multi_objective_returns_pareto_front(
    naive_ohlcv_df, multi_objective_config
):
    multi_objective_config.study.n_trials = 6
    backtest_runner = DefaultBacktestRunner.from_frame_sliced(naive_ohlcv_df)

    result = OptimizationRunner(
        multi_objective_config,
        backtest_runner,
        ohlcv=naive_ohlcv_df,
        max_workers=2,
    ).run()

    assert len(result.study.trials) == 6
    assert result.pareto_trials
