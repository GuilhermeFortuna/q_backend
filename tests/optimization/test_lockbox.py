"""Tests for lock-box splitting and champion evaluation (WO40)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from q_backend.optimization.backtest_runner import BacktestRunResult
from q_backend.optimization.lockbox import (
    backtest_config_for_walkforward,
    compute_lockbox_bounds,
    evaluate_lockbox,
)
from q_backend.optimization.models import BacktestConfig
from q_backend.optimization.strategy_search import LockboxConfig
from q_backend.optimization.walkforward import WalkForwardConfig, split_windows


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)


def test_lockbox_bounds_reserves_tail():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 12, 31)
    lockbox = LockboxConfig(enabled=True, lockbox_pct=0.15)
    wf_end, lockbox_start, lockbox_end = compute_lockbox_bounds(start, end, lockbox)
    assert wf_end == lockbox_start
    assert lockbox_end == end
    assert lockbox_start > start


def test_walkforward_windows_do_not_overlap_lockbox():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 12, 31)
    lockbox = LockboxConfig(enabled=True, lockbox_pct=0.15)
    wf_end, lockbox_start, _ = compute_lockbox_bounds(start, end, lockbox)
    wf = WalkForwardConfig(train_days=60, test_days=30, mode="rolling", min_windows=2)
    windows = split_windows(start, wf_end, wf)
    for window in windows:
        assert window.test_end <= lockbox_start


def test_backtest_config_for_walkforward_truncates_end():
    backtest = BacktestConfig(
        symbol="TEST",
        timeframe="D1",
        start=_dt(2024, 1, 1),
        end=_dt(2024, 12, 31),
        initial_capital=10_000.0,
        point_value=1.0,
        strategy="MACrossover",
    )
    truncated = backtest_config_for_walkforward(backtest, LockboxConfig(enabled=True, lockbox_pct=0.15))
    assert truncated.end < backtest.end


def test_lockbox_passed_reflects_gates():
    backtest = BacktestConfig(
        symbol="TEST",
        timeframe="D1",
        start=_dt(2024, 1, 1),
        end=_dt(2024, 12, 31),
        initial_capital=10_000.0,
        point_value=1.0,
        strategy="MACrossover",
    )
    lockbox = LockboxConfig(enabled=True, lockbox_pct=0.15, min_trades=5)

    runner = MagicMock()
    runner.run.return_value = BacktestRunResult(
        metrics={"total_trades": 2, "max_drawdown_pct": 0.05},
        trades=[],
    )

    metrics, passed, _equity = evaluate_lockbox(
        backtest=backtest,
        lockbox=lockbox,
        best_params={"strategy_params": {}, "risk_params": {}},
        strategy="MACrossover",
        backtest_runner=runner,
    )
    assert metrics is not None
    assert passed is False
    assert runner.run.call_count == 1


def test_lockbox_disabled_is_noop():
    backtest = BacktestConfig(
        symbol="TEST",
        timeframe="D1",
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        initial_capital=10_000.0,
        point_value=1.0,
        strategy="MACrossover",
    )
    runner = MagicMock()
    metrics, passed, equity = evaluate_lockbox(
        backtest=backtest,
        lockbox=LockboxConfig(enabled=False),
        best_params={"strategy_params": {}, "risk_params": {}},
        strategy="MACrossover",
        backtest_runner=runner,
    )
    assert metrics is None
    assert passed is True
    assert equity is None
    runner.run.assert_not_called()


def test_lockbox_xor_pct_and_days():
    with pytest.raises(ValueError, match="mutually exclusive"):
        LockboxConfig(enabled=True, lockbox_pct=0.15, lockbox_days=30)
