"""Shared indicator augmentation for backtesting, forward execution, and charts."""

from __future__ import annotations

import pandas as pd

from q_backend.backtesting import technical_indicators
from q_backend.backtesting.strategy import TradingStrategy


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
        if col.startswith("atr_") and "high" in chunk.columns and "low" in chunk.columns and "close" in chunk.columns:
            period = int(col.split("_", 1)[1])
            chunk[col] = technical_indicators.compute_atr(chunk["high"], chunk["low"], chunk["close"], period)
        elif col.startswith("donchian_high_") or col.startswith("donchian_low_"):
            prefix = "donchian_high_" if col.startswith("donchian_high_") else "donchian_low_"
            donchian_periods.add(int(col.removeprefix(prefix)))

    if donchian_periods and "high" in chunk.columns and "low" in chunk.columns:
        for period in donchian_periods:
            high_col = f"donchian_high_{period}"
            low_col = f"donchian_low_{period}"
            if high_col in chunk.columns and low_col in chunk.columns:
                continue
            upper, lower = technical_indicators.compute_donchian_channels(chunk["high"], chunk["low"], period)
            chunk[high_col] = upper
            chunk[low_col] = lower
    return chunk
