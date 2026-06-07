from datetime import datetime, timezone

import pandas as pd
import pytest

from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.optimization.metrics import (
    DRAWDOWN_FLOOR,
    build_equity_curve,
    compute_extended_metrics,
)


def _make_trade(pnl: float, exit_day: int) -> Trade:
    entry = datetime(2024, 1, 1, tzinfo=timezone.utc)
    exit_time = datetime(2024, 1, exit_day, tzinfo=timezone.utc)
    return Trade(
        id=f"t{exit_day}",
        order_id=f"o{exit_day}",
        symbol="TEST",
        action=OrderAction.BUY,
        quantity=1.0,
        entry_price=100.0,
        entry_time=entry,
        exit_price=100.0 + pnl,
        exit_time=exit_time,
        status=TradeStatus.CLOSED,
        pnl=pnl,
    )


def test_return_drawdown_ratio_uses_floor():
    metrics = compute_extended_metrics(
        base_metrics={"total_pnl": 1000.0, "max_drawdown_pct": 0.0},
        closed_trades=[_make_trade(500, 2), _make_trade(500, 4)],
        initial_capital=10_000.0,
        equity_curve=None,
        backtest_days=30.0,
    )
    expected = 0.1 / DRAWDOWN_FLOOR
    assert metrics["return_drawdown_ratio"] == pytest.approx(expected)


def test_sharpe_from_equity_curve():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)
    trades = [_make_trade(100, 5), _make_trade(50, 10), _make_trade(-20, 15)]
    equity = build_equity_curve(trades, 10_000.0, start, end)
    metrics = compute_extended_metrics(
        base_metrics={"total_pnl": 130.0, "max_drawdown_pct": 0.01},
        closed_trades=trades,
        initial_capital=10_000.0,
        equity_curve=equity,
        backtest_days=30.0,
    )
    assert "sharpe_ratio" in metrics
    assert "trade_sharpe_ratio" in metrics


def test_trade_sharpe_fallback_when_no_equity():
    trades = [_make_trade(100, 2), _make_trade(-50, 4)]
    metrics = compute_extended_metrics(
        base_metrics={"total_pnl": 50.0, "max_drawdown_pct": 0.05},
        closed_trades=trades,
        initial_capital=10_000.0,
        equity_curve=None,
        backtest_days=60.0,
    )
    assert metrics["trade_sharpe_ratio"] != 0.0
