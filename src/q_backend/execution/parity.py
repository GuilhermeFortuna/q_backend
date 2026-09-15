"""Reference queued-signal extraction for parity tests."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from q_backend.backtesting.models import Signal, Trade
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.execution.bars import bar_close_time
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.signal_eval import evaluate_queued_signals, signal_arrays


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
    for position in range(len(augmented)):
        row = augmented.iloc[position]
        open_time = signals.index[position]
        close_time = bar_close_time(open_time.to_pydatetime(), timeframe)
        exits, entries = evaluate_queued_signals(strategy, signals, position, row, open_trades)
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
