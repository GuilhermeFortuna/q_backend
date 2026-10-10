"""BacktestResult wrapper for research backtest results."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.candle_kernel import ChunkRun
from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.backtesting.registry import TradeRegistry
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.charting import ChartIndicator

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
    "exit_tick_time",
    "stop_loss",
    "take_profit",
]

REJECTED_COLUMNS = ["time", "side", "fill_price", "stop_loss", "take_profit"]


@dataclass(frozen=True)
class BacktestResult:
    """
    Immutable container holding the output of a research backtest run.

    Attributes
    ----------
    metrics : dict of str to Any
        Summary performance statistics (e.g., ``total_pnl``, ``win_rate``,
        ``profit_factor``, ``max_drawdown_pct``).
    trades : pandas.DataFrame
        Detailed trade execution log containing fill prices, timestamps, PnL,
        commissions, and exit reasons.
    equity : pandas.DataFrame
        Time series tracking realized account equity across closed trades.
    data : pandas.DataFrame
        Historical price frame augmented with computed user and exit indicators.
    indicators : tuple of ChartIndicator
        Indicator series configurations to render on the desktop Trade Chart.
    config : Mapping of str to Any
        Read-only dictionary of the arguments used for this backtest.
    rejected_entries : pandas.DataFrame
        DataFrame listing entries rejected due to invalid protective price levels.
    """

    metrics: dict[str, Any]
    trades: pd.DataFrame
    equity: pd.DataFrame
    data: pd.DataFrame
    indicators: tuple[ChartIndicator, ...] = ()
    config: Mapping[str, Any] = MappingProxyType({})
    _closed_trades: tuple[Trade, ...] = ()
    _strategy: Any = None
    rejected_entries: pd.DataFrame = field(default_factory=lambda: empty_rejected_frame())

    def publish(
        self,
        *,
        name: str | None = None,
        timeframe: str | None = None,
        api_url: str | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Publish the finished backtest to the Q Research desktop application.

        Sends the backtest run via HTTP to the Research API backend for visual
        inspection in the Trade Chart, Performance analytics, and Trade List views.

        Parameters
        ----------
        name : str, optional
            Display name for the strategy run. Defaults to the strategy class name.
        timeframe : str, optional
            Canonical bar timeframe (e.g. ``'M5'``). Defaults to the frame timeframe.
        api_url : str, optional
            Override for the Research API base URL (default: ``http://127.0.0.1:8001``).

        Returns
        -------
        str
            The created run UUID.
        """
        from q_backend.research.publishing import publish_backtest_result

        return publish_backtest_result(
            self,
            name=name,
            timeframe=timeframe,
            api_url=api_url,
            **kwargs,
        )


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
        "exit_tick_time": "datetime64[ns, America/Sao_Paulo]",
        "stop_loss": "float64",
        "take_profit": "float64",
    }
    df = pd.DataFrame({col: pd.Series(dtype=dt) for col, dt in dtypes.items()})
    return df[TRADE_COLUMNS]


def empty_rejected_frame() -> pd.DataFrame:
    dtypes = {
        "time": "datetime64[ns, America/Sao_Paulo]",
        "side": "object",
        "fill_price": "float64",
        "stop_loss": "float64",
        "take_profit": "float64",
    }
    df = pd.DataFrame({col: pd.Series(dtype=dt) for col, dt in dtypes.items()})
    return df[REJECTED_COLUMNS]


def rejected_entries_frame(run: ChunkRun, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Entries refused because a level was already on the wrong side of the fill."""
    rejected = run.rejected
    if len(rejected["bar"]) == 0:
        return empty_rejected_frame()
    times = [index[int(bar)] for bar in rejected["bar"]]
    df = pd.DataFrame(
        {
            "time": pd.array(times, dtype="datetime64[ns, America/Sao_Paulo]"),
            "side": ["long" if side == 1 else "short" for side in rejected["side"]],
            "fill_price": rejected["fill_price"].astype(np.float64),
            "stop_loss": rejected["stop_price"].astype(np.float64),
            "take_profit": rejected["target_price"].astype(np.float64),
        }
    )
    return df[REJECTED_COLUMNS]


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
                "exit_tick_time": _brasilia_or_nat(t.exit_tick_time),
                "stop_loss": np.nan if t.stop_loss is None else float(t.stop_loss),
                "take_profit": np.nan if t.take_profit is None else float(t.take_profit),
            }
        )

    df = pd.DataFrame(rows)
    for column in ("entry_time", "exit_time", "exit_tick_time"):
        df[column] = pd.array([row[column] for row in rows], dtype="datetime64[ns, America/Sao_Paulo]")
    return df[TRADE_COLUMNS]


def _brasilia_or_nat(value: datetime | None) -> pd.Timestamp:
    if value is None:
        return pd.NaT
    ts = pd.Timestamp(value)
    return ts.tz_localize(BRASILIA_TZ) if ts.tzinfo is None else ts.tz_convert(BRASILIA_TZ)


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
