"""BacktestResult wrapper for research backtest results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.models import OrderAction, TradeStatus
from q_backend.backtesting.registry import TradeRegistry
from q_backend.market_data.timezone import BRASILIA_TZ

TRADE_COLUMNS = [
    "trade_id",
    "symbol",
    "side",
    "status",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "pnl",
    "quantity",
    "commission",
    "point_value",
    "exit_reason",
]


@dataclass(frozen=True)
class BacktestResult:
    """Synchronous research backtest results."""

    metrics: dict[str, Any]
    trades: pd.DataFrame
    equity: pd.DataFrame
    data: pd.DataFrame


def empty_trades_frame() -> pd.DataFrame:
    dtypes = {
        "trade_id": "object",
        "symbol": "object",
        "side": "object",
        "status": "object",
        "entry_time": "datetime64[ns, America/Sao_Paulo]",
        "entry_price": "float64",
        "exit_time": "datetime64[ns, America/Sao_Paulo]",
        "exit_price": "float64",
        "pnl": "float64",
        "quantity": "float64",
        "commission": "float64",
        "point_value": "float64",
        "exit_reason": "object",
    }
    df = pd.DataFrame({col: pd.Series(dtype=dt) for col, dt in dtypes.items()})
    return df[TRADE_COLUMNS]


def trades_to_frame(registry: TradeRegistry) -> pd.DataFrame:
    """Build the stable trades DataFrame sorted by entry_time."""
    all_trades = registry.get_all_trades()
    if not all_trades:
        return empty_trades_frame()

    # Sort by entry_time
    sorted_trades = sorted(all_trades, key=lambda t: t.entry_time)

    rows = []
    for t in sorted_trades:
        side = "long" if t.action == OrderAction.BUY else "short"
        status = "closed" if t.status == TradeStatus.CLOSED else "open"

        # Ensure America/Sao_Paulo timestamps
        entry_ts = pd.Timestamp(t.entry_time)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize(BRASILIA_TZ)
        else:
            entry_ts = entry_ts.tz_convert(BRASILIA_TZ)

        if t.exit_time is not None:
            exit_ts = pd.Timestamp(t.exit_time)
            if exit_ts.tzinfo is None:
                exit_ts = exit_ts.tz_localize(BRASILIA_TZ)
            else:
                exit_ts = exit_ts.tz_convert(BRASILIA_TZ)
        else:
            exit_ts = pd.NaT

        rows.append(
            {
                "trade_id": t.id,
                "symbol": t.symbol,
                "side": side,
                "status": status,
                "entry_time": entry_ts,
                "entry_price": float(t.entry_price),
                "exit_time": exit_ts,
                "exit_price": float(t.exit_price) if t.exit_price is not None else np.nan,
                "pnl": float(t.pnl) if t.pnl is not None else np.nan,
                "quantity": float(t.quantity),
                "commission": float(t.commission),
                "point_value": float(getattr(t, "point_value", 1.0)),
                "exit_reason": t.exit_reason,
            }
        )

    df = pd.DataFrame(rows)
    return df[TRADE_COLUMNS]


def build_equity_curve(
    registry: TradeRegistry,
    data_index: pd.DatetimeIndex,
    initial_capital: float,
) -> pd.DataFrame:
    """Build realized equity curve: initial capital + cumulative closed-trade net PnL at exit timestamps."""
    if len(data_index) == 0:
        return pd.DataFrame(
            {"realized_equity": pd.Series(dtype="float64")},
            index=pd.DatetimeIndex([], name="time", tz=BRASILIA_TZ),
        )

    # Base equity series on data_index
    equity_series = pd.Series(0.0, index=data_index, dtype="float64")

    # Add closed trade PnLs at their exit_time
    closed_trades = registry.get_closed_trades()
    for t in closed_trades:
        if t.exit_time is None or t.pnl is None:
            continue
        exit_ts = pd.Timestamp(t.exit_time)
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize(BRASILIA_TZ)
        else:
            exit_ts = exit_ts.tz_convert(BRASILIA_TZ)

        if exit_ts in equity_series.index:
            equity_series.loc[exit_ts] += float(t.pnl)

    realized_equity = float(initial_capital) + equity_series.cumsum()
    out = pd.DataFrame({"realized_equity": realized_equity}, index=data_index)
    out.index.name = "time"
    return out
