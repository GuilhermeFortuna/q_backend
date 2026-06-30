"""Genome evaluation for ``feature.*`` context nodes (WO159)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.session_context.compute import (
    CTX_D1_PREV_CLOSE,
    CTX_D1_PREV_HIGH,
    CTX_D1_PREV_LOW,
    CTX_D1_TREND,
    CTX_D1_VOLATILITY,
    CTX_PREV_SESSION_CLOSE,
    CTX_PREV_SESSION_HIGH,
    CTX_PREV_SESSION_LOW,
    CTX_SESSION_GAP,
    compute_day_of_week,
    compute_distance_atr,
    compute_minutes_from_open,
    compute_month_of_year,
    compute_opening_range_levels,
    compute_range_compression,
    compute_session_window,
    compute_time_of_day,
    compute_trend_regime,
    compute_vol_regime,
)


def _datetime_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index
    return pd.DatetimeIndex(pd.to_datetime(df["time"]))


def evaluate_feature_node(
    df: pd.DataFrame,
    *,
    kind: str,
    params: dict[str, Any],
    cols: dict[str, str],
    binding_series,
) -> None:
    index = _datetime_index(df)

    if kind == "feature.minutes_from_open":
        df[cols["out"]] = compute_minutes_from_open(index, str(params["session_open"]))
    elif kind == "feature.time_of_day":
        df[cols["out"]] = compute_time_of_day(
            index,
            session_open=str(params["session_open"]),
            session_close=str(params["session_close"]),
        )
    elif kind == "feature.day_of_week":
        df[cols["out"]] = compute_day_of_week(index)
    elif kind == "feature.month_of_year":
        df[cols["out"]] = compute_month_of_year(index)
    elif kind == "feature.session_window":
        df[cols["out"]] = compute_session_window(
            index,
            window_from=str(params["window_from"]),
            window_to=str(params["window_to"]),
        )
    elif kind == "feature.prev_session_high":
        df[cols["out"]] = df[CTX_PREV_SESSION_HIGH]
    elif kind == "feature.prev_session_low":
        df[cols["out"]] = df[CTX_PREV_SESSION_LOW]
    elif kind == "feature.prev_session_close":
        df[cols["out"]] = df[CTX_PREV_SESSION_CLOSE]
    elif kind == "feature.session_gap":
        df[cols["out"]] = df[CTX_SESSION_GAP]
    elif kind == "feature.d1_prev_high":
        df[cols["out"]] = df[CTX_D1_PREV_HIGH]
    elif kind == "feature.d1_prev_low":
        df[cols["out"]] = df[CTX_D1_PREV_LOW]
    elif kind == "feature.d1_prev_close":
        df[cols["out"]] = df[CTX_D1_PREV_CLOSE]
    elif kind == "feature.d1_trend":
        df[cols["out"]] = df[CTX_D1_TREND]
    elif kind == "feature.d1_volatility":
        df[cols["out"]] = df[CTX_D1_VOLATILITY]
    elif kind == "feature.vol_regime":
        source = binding_series(df, 0)
        df[cols["out"]] = compute_vol_regime(
            source,
            window=int(params["window"]),
            regime_lookback=int(params["regime_lookback"]),
        )
    elif kind == "feature.trend_regime":
        source = binding_series(df, 0)
        df[cols["out"]] = compute_trend_regime(source, int(params["ma_period"]))
    elif kind == "feature.range_compression":
        source = binding_series(df, 0)
        df[cols["out"]] = compute_range_compression(
            df["high"],
            df["low"],
            source,
            window=int(params["window"]),
            regime_lookback=int(params["regime_lookback"]),
        )
    elif kind == "feature.dist_prev_session_high_atr":
        df[cols["out"]] = compute_distance_atr(
            df["close"],
            df[CTX_PREV_SESSION_HIGH],
            int(params["atr_period"]),
            high=df["high"],
            low=df["low"],
        )
    elif kind == "feature.dist_prev_session_low_atr":
        df[cols["out"]] = compute_distance_atr(
            df["close"],
            df[CTX_PREV_SESSION_LOW],
            int(params["atr_period"]),
            high=df["high"],
            low=df["low"],
        )
    elif kind == "feature.dist_prev_session_close_atr":
        df[cols["out"]] = compute_distance_atr(
            df["close"],
            df[CTX_PREV_SESSION_CLOSE],
            int(params["atr_period"]),
            high=df["high"],
            low=df["low"],
        )
    elif kind == "feature.opening_range_high":
        high, _low = compute_opening_range_levels(
            df,
            session_open=str(params["session_open"]),
            range_minutes=int(params["range_minutes"]),
        )
        df[cols["out"]] = high
    elif kind == "feature.opening_range_low":
        _high, low = compute_opening_range_levels(
            df,
            session_open=str(params["session_open"]),
            range_minutes=int(params["range_minutes"]),
        )
        df[cols["out"]] = low
    else:
        raise ValueError(f"Unsupported feature node kind '{kind}'.")
