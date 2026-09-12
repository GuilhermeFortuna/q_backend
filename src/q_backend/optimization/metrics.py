import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from q_backend.backtesting.models import Trade

DRAWDOWN_FLOOR = 1e-9


def _normalize_timestamp(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def build_equity_curve(
    closed_trades: list[Trade],
    initial_capital: float,
    start: datetime,
    end: datetime,
) -> pd.Series:
    equity = initial_capital
    points: list[tuple[datetime, float]] = [(_normalize_timestamp(start), equity)]

    sorted_trades = sorted(
        closed_trades,
        key=lambda trade: trade.exit_time or trade.entry_time,
    )
    for trade in sorted_trades:
        if trade.exit_time is None:
            continue
        equity += trade.pnl or 0.0
        points.append((_normalize_timestamp(trade.exit_time), equity))

    points.append((_normalize_timestamp(end), equity))

    series = pd.Series(
        [value for _, value in points],
        index=pd.DatetimeIndex([timestamp for timestamp, _ in points]),
    )
    return series[~series.index.duplicated(keep="last")].sort_index()


def _sharpe_from_daily_returns(daily_returns: pd.Series) -> float | None:
    if len(daily_returns) < 2:
        return None
    std = daily_returns.std()
    if std == 0 or math.isnan(std):
        return None
    return float(daily_returns.mean() / std * math.sqrt(252))


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
        daily_equity = equity_curve.resample("D").last().ffill()
        daily_returns = daily_equity.pct_change().dropna()
        sharpe_ratio = _sharpe_from_daily_returns(daily_returns)

    if sharpe_ratio is None:
        sharpe_ratio = trade_sharpe_ratio

    return {
        **base_metrics,
        "total_return_pct": total_return_pct,
        "sharpe_ratio": sharpe_ratio,
        "trade_sharpe_ratio": trade_sharpe_ratio,
        "return_drawdown_ratio": return_drawdown_ratio,
    }
