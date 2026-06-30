"""Queued-signal evaluation shared with the backtest engine's bar loop."""

from __future__ import annotations

from typing import List

import pandas as pd

from q_backend.backtesting.models import Signal, Trade
from q_backend.backtesting.strategy import TradingStrategy


def evaluate_queued_signals(
    strategy: TradingStrategy,
    current_data: pd.Series,
    open_trades: List[Trade],
) -> tuple[list[Signal], list[Signal]]:
    """Mirror ``BacktestEngine`` section D (evaluate, queue for next bar).

    Exit-rule signals take precedence over strategy exit checks for symbols already
    covered, matching engine ordering.
    """
    if hasattr(strategy, "exit_strategy"):
        pending_exits = strategy.exit_strategy.check_exits(open_trades, current_data)
    else:
        pending_exits = []

    closed_symbols = {sig.symbol for sig in pending_exits}
    strategy_exits = strategy.check_exit_conditions(
        current_data,
        [trade for trade in open_trades if trade.symbol not in closed_symbols],
    )
    pending_exits = [*pending_exits, *strategy_exits]
    pending_entries = strategy.check_entry_conditions(current_data)
    return pending_exits, pending_entries
