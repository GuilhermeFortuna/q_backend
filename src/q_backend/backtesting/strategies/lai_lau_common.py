"""Shared helpers for Lai & Lau (2006) technical rule strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd

MA_TYPE_CHOICES = sorted(["sma", "ema", "wma", "smma", "hma"])


def add_bar_index(df: pd.DataFrame) -> pd.DataFrame:
    df["bar_index"] = np.arange(len(df), dtype=int)
    return df


def compute_ma_band_signals(
    df: pd.DataFrame,
    ma_series: pd.Series,
    band_pct: float,
) -> pd.DataFrame:
    upper = ma_series * (1.0 + band_pct / 100.0)
    lower = ma_series * (1.0 - band_pct / 100.0)
    prev_close = df["close"].shift(1)
    prev_upper = upper.shift(1)
    prev_lower = lower.shift(1)

    df["ma_band_upper"] = upper
    df["ma_band_lower"] = lower
    df["buy_signal"] = (prev_close <= prev_upper) & (df["close"] > upper)
    df["sell_signal"] = (prev_close >= prev_lower) & (df["close"] < lower)
    return df


def compute_trb_channel_signals(
    df: pd.DataFrame,
    period: int,
    band_pct: float,
) -> pd.DataFrame:
    channel_high = df["close"].shift(1).rolling(period).max()
    channel_low = df["close"].shift(1).rolling(period).min()
    upper = channel_high * (1.0 + band_pct / 100.0)
    lower = channel_low * (1.0 - band_pct / 100.0)
    prev_close = df["close"].shift(1)

    df["channel_high"] = channel_high
    df["channel_low"] = channel_low
    df["trb_upper"] = upper
    df["trb_lower"] = lower
    df["buy_signal"] = (prev_close <= channel_high) & (df["close"] > upper)
    df["sell_signal"] = (prev_close >= channel_low) & (df["close"] < lower)
    return df
