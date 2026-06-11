from datetime import datetime
from unittest.mock import MagicMock

import MetaTrader5 as mt5
import numpy as np
import pytest

from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.optimization.backtest_runner import BacktestRunConfig
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.runner import OptimizationRunner
from q_backend.optimization.tick_backtest_runner import (
    TickBacktestRunner,
    columnar_to_tick_arrays,
)


def _synthetic_columnar(n: int = 800, day_offset_msc: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(42)
    prices = 100.0 + np.cumsum(rng.normal(0.05, 0.2, n))
    spread = 0.02
    base = 1_700_000_000_000 + day_offset_msc
    return {
        "time_msc": base + np.arange(n, dtype=np.int64) * 1000,
        "bid": (prices - spread / 2).astype(np.float64),
        "ask": (prices + spread / 2).astype(np.float64),
        "last": prices.astype(np.float64),
        "volume": np.ones(n, dtype=np.float64),
    }


def _tick_backtest_config(**overrides) -> BacktestRunConfig:
    defaults = {
        "symbol": "TEST",
        "timeframe": "TICK",
        "start": datetime(2024, 1, 1),
        "end": datetime(2024, 2, 1),
        "initial_capital": 10_000.0,
        "point_value": 1.0,
        "strategy": "TickMaBreakout",
        "strategy_params": {
            "short_period": 5,
            "long_period": 20,
            "threshold": 0.0,
            "sl_points": 0.0,
            "tp_points": 0.0,
        },
        "position_sizing": FixedQuantityPositionSizing(quantity=1.0),
        "engine": "tick",
    }
    defaults.update(overrides)
    return BacktestRunConfig(**defaults)


def test_tick_backtest_runner_run_returns_extended_metrics():
    runner = TickBacktestRunner(columnar_to_tick_arrays(_synthetic_columnar()))
    result = runner.run(_tick_backtest_config())

    assert result.trial_user_attrs["total_trades"] == result.metrics["total_trades"]
    for key in (
        "total_trades",
        "total_pnl",
        "max_drawdown_pct",
        "sharpe_ratio",
        "return_drawdown_ratio",
    ):
        assert key in result.metrics


def test_from_market_data_loads_ticks_once_and_reuses_across_runs():
    service = MagicMock()
    service.get_ticks_columnar.return_value = _synthetic_columnar()

    runner = TickBacktestRunner.from_market_data(
        service,
        symbol="TEST",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 2, 1),
        flags=mt5.COPY_TICKS_ALL,
    )
    config = _tick_backtest_config()

    runner.run(config)
    runner.run(config)

    service.get_ticks_columnar.assert_called_once_with(
        "TEST",
        datetime(2024, 1, 1),
        datetime(2024, 2, 1),
        flags=mt5.COPY_TICKS_ALL,
    )


def test_from_market_data_raises_on_empty_ticks():
    service = MagicMock()
    service.get_ticks_columnar.return_value = {
        "time_msc": np.array([], dtype=np.int64),
        "bid": np.array([], dtype=np.float64),
        "ask": np.array([], dtype=np.float64),
        "last": np.array([], dtype=np.float64),
        "volume": np.array([], dtype=np.float64),
    }

    with pytest.raises(ValueError, match="No tick data found"):
        TickBacktestRunner.from_market_data(
            service,
            symbol="TEST",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 2, 1),
        )


def test_tick_optimization_end_to_end():
    service = MagicMock()
    service.get_ticks_columnar.return_value = _synthetic_columnar(n=1200)

    runner = TickBacktestRunner.from_market_data(
        service,
        symbol="TEST",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 3, 1),
    )

    config = OptimizationConfig.model_validate(
        {
            "study": {
                "name": "tick_opt_test",
                "n_trials": 3,
                "seed": 7,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "start": "2024-01-01T00:00:00",
                "end": "2024-03-01T00:00:00",
                "strategy": "TickMaBreakout",
                "engine": "tick",
                "tick_flags": "all",
                "initial_capital": 10_000,
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 3, "high": 6},
                    "long_period": {"type": "int", "low": 10, "high": 15},
                },
                "risk_params": {
                    "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                    "quantity": {"type": "float", "low": 1.0, "high": 1.0},
                },
            },
        }
    )

    result = OptimizationRunner(config, runner).run()

    assert service.get_ticks_columnar.call_count == 1
    assert result.best_trial is not None
    assert "strategy__short_period" in result.best_params
    assert result.best_trial.user_attrs.get("status") == "complete"
