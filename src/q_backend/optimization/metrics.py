import math
from typing import Any

import pandas as pd

from q_backend.backtesting.analytics import (
    build_equity_curve,
    daily_equity_returns,
    daily_sharpe_ratio,
)
from q_backend.backtesting.models import Trade

__all__ = ["DRAWDOWN_FLOOR", "build_equity_curve", "compute_extended_metrics"]

DRAWDOWN_FLOOR = 1e-9


def _trade_sharpe_ratio(
    closed_trades: list[Trade],
    initial_capital: float,
    backtest_days: float,
) -> float:
    if len(closed_trades) < 2 or backtest_days <= 0:
        return 0.0

    returns = [(trade.pnl or 0.0) / initial_capital for trade in closed_trades]
    std = pd.Series(returns).std()
    if std == 0 or math.isnan(std):
        return 0.0
    mean = sum(returns) / len(returns)
    annualization = math.sqrt(252 / backtest_days)
    return float(mean / std * annualization)


def compute_extended_metrics(
    base_metrics: dict[str, Any],
    closed_trades: list[Trade],
    initial_capital: float,
    equity_curve: pd.Series | None,
    backtest_days: float,
) -> dict[str, Any]:
    total_pnl = float(base_metrics.get("total_pnl", 0.0))
    max_drawdown_pct = float(base_metrics.get("max_drawdown_pct", 0.0))

    total_return_pct = total_pnl / initial_capital if initial_capital > 0 else 0.0
    return_drawdown_ratio = total_return_pct / max(max_drawdown_pct, DRAWDOWN_FLOOR)

    sharpe_ratio: float | None = None
    trade_sharpe_ratio = _trade_sharpe_ratio(closed_trades, initial_capital, backtest_days)

    if equity_curve is not None and len(equity_curve) >= 2:
        sharpe_ratio = daily_sharpe_ratio(daily_equity_returns(equity_curve))

    if sharpe_ratio is None:
        sharpe_ratio = trade_sharpe_ratio

    return {
        **base_metrics,
        "total_return_pct": total_return_pct,
        "sharpe_ratio": sharpe_ratio,
        "trade_sharpe_ratio": trade_sharpe_ratio,
        "return_drawdown_ratio": return_drawdown_ratio,
    }
