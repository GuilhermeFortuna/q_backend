from dataclasses import dataclass, field
from typing import Any

import optuna
import pytest

from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
    DefaultBacktestRunner,
)
from q_backend.optimization.exceptions import ExpectedTrialFailure
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.runner import OptimizationRunner


@dataclass
class StubBacktestRunner:
    call_count: int = 0
    fail_on_call: bool = False
    calls: list[BacktestRunConfig] = field(default_factory=list)

    def run(self, config: BacktestRunConfig) -> BacktestRunResult:
        self.call_count += 1
        self.calls.append(config)
        if self.fail_on_call:
            raise RuntimeError("unexpected backtest failure")

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


def test_runner_single_objective_finds_best_params(single_objective_config):
    runner_stub = StubBacktestRunner()
    opt_runner = OptimizationRunner(single_objective_config, runner_stub)
    result = opt_runner.run()

    assert runner_stub.call_count > 0
    assert result.best_trial is not None
    assert "strategy__short_period" in result.best_params
    best_short = result.best_params["strategy__short_period"]
    assert best_short == max(call.strategy_params["short_period"] for call in runner_stub.calls)


def test_invalid_combos_pruned_before_backtest():
    config = OptimizationConfig.model_validate(
        {
            "study": {
                "name": "prune_test",
                "n_trials": 5,
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
                    "short_period": {"type": "int", "low": 20, "high": 30},
                    "long_period": {"type": "int", "low": 5, "high": 15},
                }
            },
        }
    )
    runner_stub = StubBacktestRunner()
    result = OptimizationRunner(config, runner_stub).run()
    assert runner_stub.call_count == 0
    pruned = [trial for trial in result.study.trials if trial.state == optuna.trial.TrialState.PRUNED]
    assert len(pruned) == config.study.n_trials


def test_continue_on_trial_error_swallows_unexpected():
    config = OptimizationConfig.model_validate(
        {
            "study": {
                "name": "error_test",
                "n_trials": 2,
                "seed": 42,
                "continue_on_trial_error": True,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 4},
                    "long_period": {"type": "int", "low": 5, "high": 8},
                }
            },
        }
    )
    runner_stub = StubBacktestRunner(fail_on_call=True)
    result = OptimizationRunner(config, runner_stub).run()
    assert len(result.failures) == 2


def test_unexpected_exception_reraises_without_continue_flag(single_objective_config):
    single_objective_config.study.continue_on_trial_error = False
    runner_stub = StubBacktestRunner(fail_on_call=True)
    with pytest.raises(RuntimeError, match="unexpected backtest failure"):
        OptimizationRunner(single_objective_config, runner_stub).run()


def test_multi_objective_returns_pareto_front(multi_objective_config):
    runner_stub = StubBacktestRunner()
    result = OptimizationRunner(multi_objective_config, runner_stub).run()
    assert result.pareto_trials is not None
    assert len(result.study.trials) == multi_objective_config.study.n_trials


def test_default_backtest_runner_with_injected_data(single_objective_config, sample_ohlcv_df):
    def data_provider(_config):
        return sample_ohlcv_df

    backtest_runner = DefaultBacktestRunner(data_provider=data_provider)
    single_objective_config.study.n_trials = 2
    result = OptimizationRunner(single_objective_config, backtest_runner).run()
    assert len(result.study.trials) == 2


def test_expected_trial_failure_prunes():
    @dataclass
    class FailingRunner:
        def run(self, config: BacktestRunConfig) -> BacktestRunResult:
            raise ExpectedTrialFailure("no signals")

    config = OptimizationConfig.model_validate(
        {
            "study": {
                "name": "expected_fail",
                "n_trials": 1,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 3},
                    "long_period": {"type": "int", "low": 5, "high": 6},
                }
            },
        }
    )
    result = OptimizationRunner(config, FailingRunner()).run()
    assert result.study.trials[0].state == optuna.trial.TrialState.PRUNED
