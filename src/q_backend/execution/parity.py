"""Reference queued-signal extraction for parity tests."""

from __future__ import annotations

import datetime as dt
from datetime import datetime
from typing import Optional

import pandas as pd

from q_backend.backtesting.models import OrderAction, Signal, SignalAction as BacktestSignalAction, Trade
from q_backend.backtesting.candle_kernel import reference_decisions
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.execution.bars import bar_close_time
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.domain import StrategyIdentity
from q_backend.backtesting.signal_columns import signal_arrays
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.adapter import ResearchStrategyAdapter
from q_backend.research.positions import ResearchPosition
from q_backend.research.strategy import ResearchStrategy


def augment_with_exit_columns(
    strategy: TradingStrategy,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """Compute strategy indicators and any enabled exit-rule columns."""
    evaluator = StrategyEvaluator(
        deployment_id="parity-ref",
        identity=StrategyIdentity(
            strategy_name="parity",
            strategy_version=1,
            compiled_config={
                "strategy": "MACrossover",
                "strategy_params": {"short_period": 5, "long_period": 20},
            },
            config_hash="parity",
            symbol="TEST",
            timeframe="H1",
        ),
        strategy=strategy,
        window_bound=max(len(frame), 1),
    )
    evaluator.seed_window(frame)
    return evaluator._augmented_frame()


def reference_queued_signals_by_close(
    strategy: TradingStrategy,
    data: pd.DataFrame,
    *,
    timeframe: str,
    open_trade: Optional[Trade] = None,
) -> list[tuple[datetime, list[Signal], list[Signal]]]:
    """Engine section-D queued signals keyed by bar close time (no fills)."""
    augmented = augment_with_exit_columns(strategy, data)
    signals = signal_arrays(strategy, augmented)
    open_trades = [open_trade] if open_trade else []
    rows: list[tuple[datetime, list[Signal], list[Signal]]] = []
    for position, (exits, entries) in enumerate(reference_decisions(strategy, augmented, signals, open_trades)):
        open_time = signals.index[position]
        close_time = bar_close_time(open_time.to_pydatetime(), timeframe)
        rows.append((close_time, exits, entries))
    return rows


def signals_equal(
    left: list[Signal],
    right: list[Signal],
) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a.action != b.action or a.symbol != b.symbol:
            return False
        if a.action.value == "CLOSE":
            if getattr(a, "exit_reason", None) != getattr(b, "exit_reason", None):
                return False
    return True


def reference_research_decisions_by_close(
    strategy: ResearchStrategy,
    data: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    day_trade: bool = True,
    day_trade_start_time: str = "09:00",
    day_trade_end_time: str = "16:00",
    day_trade_close_time: str = "17:00",
    open_trade: Optional[Trade] = None,
) -> list[tuple[datetime, list[Signal], list[Signal]]]:
    """Research strategy queued signals keyed by bar close time (no fills)."""
    adapter = ResearchStrategyAdapter(strategy, symbol=symbol)
    augmented = adapter.compute_indicators(data.copy())
    adapter._decision_frame = augmented

    start_t = dt.time.fromisoformat(day_trade_start_time)
    end_t = dt.time.fromisoformat(day_trade_end_time)
    close_t = dt.time.fromisoformat(day_trade_close_time)

    rows: list[tuple[datetime, list[Signal], list[Signal]]] = []
    for position in range(len(augmented)):
        open_time = augmented.index[position]
        close_time = bar_close_time(open_time.to_pydatetime(), timeframe)
        bar_local = pd.Timestamp(close_time).tz_convert(BRASILIA_TZ)
        bar_time = bar_local.time()
        prefix = augmented.iloc[: position + 1]

        positions_view: tuple[ResearchPosition, ...] = ()
        if open_trade is not None:
            side = "long" if open_trade.action == OrderAction.BUY else "short"
            positions_view = (
                ResearchPosition(
                    symbol=symbol,
                    side=side,
                    entry_time=pd.Timestamp(open_trade.entry_time),
                    entry_price=float(open_trade.entry_price),
                    quantity=float(open_trade.quantity),
                ),
            )

        exits: list[Signal] = []
        entries: list[Signal] = []

        if day_trade and positions_view:
            entry_local = pd.Timestamp(positions_view[0].entry_time).tz_convert(BRASILIA_TZ)
            if bar_local.date() > entry_local.date() or bar_time >= close_t:
                exits.append(Signal(action=BacktestSignalAction.CLOSE, symbol=symbol, exit_reason="day_trade_close"))

        if not exits and positions_view:
            exit_orders = adapter._invoke_hook("exit_strategy", prefix, positions_view)
            if exit_orders and exit_orders[0].action == "close" and exit_orders[0].price is None:
                exits.append(
                    Signal(
                        action=BacktestSignalAction.CLOSE,
                        symbol=symbol,
                        exit_reason=exit_orders[0].reason or "exit_rule",
                    )
                )

        if not exits and not positions_view:
            if not (day_trade and (bar_time < start_t or bar_time > end_t)):
                entry_orders = adapter._invoke_hook("entry_strategy", prefix, positions_view)
                if entry_orders:
                    first = entry_orders[0]
                    if first.action in ("buy", "sell") and first.kind == "market" and first.price is None:
                        action = BacktestSignalAction.BUY if first.action == "buy" else BacktestSignalAction.SELL
                        entries.append(Signal(action=action, symbol=symbol, entry_reason=first.reason or "entry"))

        rows.append((close_time, exits, entries))

    return rows
