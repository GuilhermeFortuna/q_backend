"""Tests for rolling-window bound calculation."""

from __future__ import annotations

import pytest

from q_backend.execution.warmup import (
    WindowBoundUndeterminedError,
    compute_window_bound_bars,
)


def test_window_bound_from_strategy_and_exit_params():
    compiled = {
        "strategy": "MACrossover",
        "strategy_params": {"short_period": 10, "long_period": 200},
        "exit_params": {"atr_period": 21, "stop_loss_atr": 2.0},
        "symbol": "WIN$",
        "timeframe": "H1",
    }
    # longest lookback is long_period=200 -> 200*3+5 = 605
    assert compute_window_bound_bars(compiled) == 605


def test_window_bound_rejects_tick_engine():
    with pytest.raises(WindowBoundUndeterminedError):
        compute_window_bound_bars(
            {
                "strategy": "TickScalper",
                "engine": "tick",
                "symbol": "WIN$",
                "timeframe": "TICK",
            }
        )


def test_window_bound_rejects_missing_lookbacks():
    with pytest.raises(WindowBoundUndeterminedError):
        compute_window_bound_bars(
            {
                "strategy": "MACrossover",
                "strategy_params": {"threshold": 0.5},
                "symbol": "WIN$",
                "timeframe": "H1",
            }
        )
