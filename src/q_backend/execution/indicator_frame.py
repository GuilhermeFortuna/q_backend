"""Shared indicator augmentation for forward execution and the chart endpoint.

The forward :class:`~q_backend.execution.evaluator.StrategyEvaluator` and the
read-only deployment chart endpoint must see *identical* indicator series for a
given bar. Both funnel through :func:`augment_indicator_frame` so the strategy's
own ``compute_indicators`` output plus the exit-rule ATR/Donchian completion can
never drift between the two code paths.
"""

from __future__ import annotations

import pandas as pd

from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_donchian_channels,
)


def augment_indicator_frame(
    strategy: TradingStrategy,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """Return ``frame`` augmented with strategy + exit-rule indicator columns.

    Runs the strategy's ``compute_indicators`` and then fills in any ATR/Donchian
    columns the exit strategy requires but the strategy itself did not emit. The
    input frame is never mutated (``compute_indicators`` receives a copy).
    """
    chunk = strategy.compute_indicators(frame.copy())
    exit_strategy = getattr(strategy, "exit_strategy", None)
    if exit_strategy is None:
        return chunk

    donchian_periods: set[int] = set()
    for col in exit_strategy.required_columns():
        if col in chunk.columns:
            continue
        if (
            col.startswith("atr_")
            and "high" in chunk.columns
            and "low" in chunk.columns
            and "close" in chunk.columns
        ):
            period = int(col.split("_", 1)[1])
            chunk[col] = compute_atr(
                chunk["high"], chunk["low"], chunk["close"], period
            )
        elif col.startswith("donchian_high_") or col.startswith("donchian_low_"):
            prefix = (
                "donchian_high_"
                if col.startswith("donchian_high_")
                else "donchian_low_"
            )
            donchian_periods.add(int(col.removeprefix(prefix)))

    if donchian_periods and "high" in chunk.columns and "low" in chunk.columns:
        for period in donchian_periods:
            high_col = f"donchian_high_{period}"
            low_col = f"donchian_low_{period}"
            if high_col in chunk.columns and low_col in chunk.columns:
                continue
            upper, lower = compute_donchian_channels(
                chunk["high"], chunk["low"], period
            )
            chunk[high_col] = upper
            chunk[low_col] = lower
    return chunk
