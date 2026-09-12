"""Held-out lock-box date splitting and champion evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunner,
)
from q_backend.optimization.models import BacktestConfig
from q_backend.optimization.metrics import build_equity_curve
from q_backend.optimization.search_space import build_position_sizing_config
from q_backend.optimization.strategy_search import LockboxConfig


def compute_lockbox_bounds(
    start: datetime,
    end: datetime,
    lockbox: LockboxConfig,
) -> tuple[datetime, datetime | None, datetime | None]:
    """Return ``(wf_end, lockbox_start, lockbox_end)`` for the configured range.

    When lock-box is disabled, ``wf_end`` equals ``end`` and lock-box bounds are ``None``.
    Walk-forward windows must use ``[start, wf_end)`` so no test bar overlaps the lock-box.
    """
    if not lockbox.enabled:
        return end, None, None

    if end <= start:
        raise ValueError("start must be before end")

    if lockbox.lockbox_days is not None:
        lockbox_start = end - timedelta(days=lockbox.lockbox_days)
    else:
        pct = lockbox.lockbox_pct if lockbox.lockbox_pct is not None else 0.15
        total_seconds = (end - start).total_seconds()
        lockbox_start = start + timedelta(seconds=total_seconds * (1.0 - pct))

    if lockbox_start <= start:
        raise ValueError("lockbox segment is too large for the date range")
    if lockbox_start >= end:
        raise ValueError("lockbox segment is empty")

    return lockbox_start, lockbox_start, end


def backtest_config_for_walkforward(
    backtest: BacktestConfig,
    lockbox: LockboxConfig,
) -> BacktestConfig:
    """Truncate ``backtest.end`` to exclude the lock-box tail when enabled."""
    if not lockbox.enabled:
        return backtest
    wf_end, _, _ = compute_lockbox_bounds(backtest.start, backtest.end, lockbox)
    return backtest.model_copy(update={"end": wf_end})


def evaluate_lockbox(
    *,
    backtest: BacktestConfig,
    lockbox: LockboxConfig,
    best_params: dict[str, Any],
    strategy: str,
    backtest_runner: BacktestRunner,
) -> tuple[dict[str, Any] | None, bool, pd.Series | None]:
    """Run one champion backtest over the lock-box slice; return metrics and gate pass."""
    if not lockbox.enabled:
        return None, True, None

    _, lockbox_start, lockbox_end = compute_lockbox_bounds(backtest.start, backtest.end, lockbox)
    if lockbox_start is None or lockbox_end is None:
        return None, True, None

    strategy_params = best_params.get("strategy_params", {})
    risk_params = best_params.get("risk_params", {})
    position_sizing = build_position_sizing_config(risk_params)

    run_config = BacktestRunConfig(
        symbol=backtest.symbol,
        timeframe=backtest.timeframe,
        start=lockbox_start,
        end=lockbox_end,
        initial_capital=backtest.initial_capital,
        point_value=backtest.point_value,
        strategy=strategy,
        strategy_params=strategy_params,
        position_sizing=position_sizing,
        costs=backtest.costs,
        parallel_mode=backtest.parallel_mode,
        day_trade=backtest.day_trade,
        day_trade_start_time=backtest.day_trade_start_time,
        day_trade_end_time=backtest.day_trade_end_time,
        day_trade_close_time=backtest.day_trade_close_time,
        engine=backtest.engine,
        display_timeframe=backtest.display_timeframe,
        tick_flags=backtest.tick_flags,
    )

    result = backtest_runner.run(run_config)
    metrics = result.metrics or {}
    passed = _lockbox_passed(metrics, lockbox)
    equity_curve = None
    if result.trades is not None:
        equity_curve = build_equity_curve(
            result.trades,
            backtest.initial_capital,
            lockbox_start,
            lockbox_end,
        )
    return metrics, passed, equity_curve


def _lockbox_passed(metrics: dict[str, Any], lockbox: LockboxConfig) -> bool:
    trades = int(metrics.get("total_trades", 0))
    if trades < lockbox.min_trades:
        return False
    if lockbox.max_drawdown_pct is not None:
        dd = float(metrics.get("max_drawdown_pct", 0.0))
        if dd > lockbox.max_drawdown_pct:
            return False
    return True
