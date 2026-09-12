"""Causal session / regime / HTF context computations (WO159)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from q_backend.backtesting.session_context.config import SessionContextConfig
from q_backend.backtesting.technical_indicators import compute_atr, compute_realized_vol
from q_backend.backtesting.transforms import compute_rolling_rank

_HHMM_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Columns attached once per run by ``attach_context_columns``.
CTX_PREV_SESSION_HIGH = "_ctx_prev_session_high"
CTX_PREV_SESSION_LOW = "_ctx_prev_session_low"
CTX_PREV_SESSION_CLOSE = "_ctx_prev_session_close"
CTX_SESSION_GAP = "_ctx_session_gap"
CTX_D1_PREV_HIGH = "_ctx_d1_prev_high"
CTX_D1_PREV_LOW = "_ctx_d1_prev_low"
CTX_D1_PREV_CLOSE = "_ctx_d1_prev_close"
CTX_D1_TREND = "_ctx_d1_trend"
CTX_D1_VOLATILITY = "_ctx_d1_volatility"

RUN_CONTEXT_COLUMNS = frozenset(
    {
        CTX_PREV_SESSION_HIGH,
        CTX_PREV_SESSION_LOW,
        CTX_PREV_SESSION_CLOSE,
        CTX_SESSION_GAP,
        CTX_D1_PREV_HIGH,
        CTX_D1_PREV_LOW,
        CTX_D1_PREV_CLOSE,
        CTX_D1_TREND,
        CTX_D1_VOLATILITY,
    }
)


@dataclass(frozen=True)
class SessionContextBundle:
    """Precomputed per-run context columns keyed by canonical names."""

    columns: dict[str, pd.Series]


def parse_hhmm(value: str) -> tuple[int, int]:
    match = _HHMM_PATTERN.match(str(value).strip())
    if match is None:
        raise ValueError(f"Expected HH:MM session time, got {value!r}.")
    return int(match.group(1)), int(match.group(2))


def hhmm_to_minutes(value: str) -> int:
    hour, minute = parse_hhmm(value)
    return hour * 60 + minute


def _datetime_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index
    if "time" in df.columns:
        return pd.DatetimeIndex(pd.to_datetime(df["time"]))
    raise ValueError("DataFrame requires a DatetimeIndex or a 'time' column.")


def _session_dates(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series([ts.date() for ts in index], index=index, dtype=object)


def _minutes_of_day(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.hour * 60 + index.minute, index=index, dtype=float)


def compute_minutes_from_open(index: pd.DatetimeIndex, session_open: str) -> pd.Series:
    open_minutes = hhmm_to_minutes(session_open)
    minutes = _minutes_of_day(index)
    delta = minutes - float(open_minutes)
    delta = delta.where(delta >= 0.0)
    return delta


def compute_time_of_day(
    index: pd.DatetimeIndex,
    *,
    session_open: str,
    session_close: str,
) -> pd.Series:
    open_minutes = hhmm_to_minutes(session_open)
    close_minutes = hhmm_to_minutes(session_close)
    if close_minutes <= open_minutes:
        raise ValueError("session_close must be after session_open.")
    minutes = _minutes_of_day(index)
    span = float(close_minutes - open_minutes)
    clamped = minutes.clip(lower=open_minutes, upper=close_minutes)
    return (clamped - open_minutes) / span


def compute_day_of_week(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.dayofweek.astype(float), index=index)


def compute_month_of_year(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.month.astype(float), index=index)


def compute_session_window(
    index: pd.DatetimeIndex,
    *,
    window_from: str,
    window_to: str,
) -> pd.Series:
    start = hhmm_to_minutes(window_from)
    end = hhmm_to_minutes(window_to)
    if end <= start:
        raise ValueError("window_from must be before window_to.")
    minutes = _minutes_of_day(index)
    return (minutes >= start) & (minutes <= end)


def _session_aggregates(df: pd.DataFrame) -> pd.DataFrame:
    index = _datetime_index(df)
    sessions = _session_dates(index)
    grouped = df.groupby(sessions, sort=True)
    return grouped.agg(
        session_open=("open", "first"),
        session_high=("high", "max"),
        session_low=("low", "min"),
        session_close=("close", "last"),
    )


def _map_previous_session_values(
    df: pd.DataFrame,
    session_stats: pd.DataFrame,
    column: str,
) -> pd.Series:
    index = _datetime_index(df)
    sessions = _session_dates(index)
    ordered = session_stats.sort_index()
    previous = ordered[column].shift(1)
    mapped = sessions.map(previous.to_dict())
    return pd.Series(mapped.to_numpy(dtype=float), index=index)


def compute_session_gap(df: pd.DataFrame, session_stats: pd.DataFrame) -> pd.Series:
    index = _datetime_index(df)
    sessions = _session_dates(index)
    ordered = session_stats.sort_index()
    prev_close = ordered["session_close"].shift(1)
    gap_by_session = ordered["session_open"] / prev_close - 1.0
    mapped = sessions.map(gap_by_session.to_dict())
    return pd.Series(mapped.to_numpy(dtype=float), index=index)


def compute_completed_d1_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate by calendar date; each row is one completed day in the data."""
    index = _datetime_index(df)
    daily = df.groupby([ts.date() for ts in index], sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    daily.index = pd.Index(daily.index, name="session_date")
    return daily


def _map_previous_trading_day(
    df: pd.DataFrame,
    daily: pd.DataFrame,
    column: str,
) -> pd.Series:
    index = _datetime_index(df)
    sessions = _session_dates(index)
    previous = daily[column].shift(1)
    mapped = sessions.map(previous.to_dict())
    return pd.Series(mapped.to_numpy(dtype=float), index=index)


def compute_d1_trend(daily: pd.DataFrame, lookback: int) -> pd.Series:
    momentum = daily["close"] / daily["close"].shift(int(lookback)) - 1.0
    return np.sign(momentum)


def compute_d1_volatility(daily: pd.DataFrame, window: int) -> pd.Series:
    return compute_realized_vol(daily["close"], int(window))


def compute_session_context_bundle(
    df: pd.DataFrame,
    config: SessionContextConfig,
) -> SessionContextBundle:
    index = _datetime_index(df)
    session_stats = _session_aggregates(df)
    daily = compute_completed_d1_frame(df)
    d1_trend = compute_d1_trend(daily, config.d1_trend_lookback).shift(1)
    d1_vol = compute_d1_volatility(daily, config.d1_vol_window).shift(1)

    sessions = _session_dates(index)
    d1_trend_mapped = sessions.map(d1_trend.to_dict())
    d1_vol_mapped = sessions.map(d1_vol.to_dict())

    columns = {
        CTX_PREV_SESSION_HIGH: _map_previous_session_values(df, session_stats, "session_high"),
        CTX_PREV_SESSION_LOW: _map_previous_session_values(df, session_stats, "session_low"),
        CTX_PREV_SESSION_CLOSE: _map_previous_session_values(df, session_stats, "session_close"),
        CTX_SESSION_GAP: compute_session_gap(df, session_stats),
        CTX_D1_PREV_HIGH: _map_previous_trading_day(df, daily, "high"),
        CTX_D1_PREV_LOW: _map_previous_trading_day(df, daily, "low"),
        CTX_D1_PREV_CLOSE: _map_previous_trading_day(df, daily, "close"),
        CTX_D1_TREND: pd.Series(d1_trend_mapped.to_numpy(dtype=float), index=index),
        CTX_D1_VOLATILITY: pd.Series(d1_vol_mapped.to_numpy(dtype=float), index=index),
    }
    return SessionContextBundle(columns=columns)


def attach_context_columns(
    df: pd.DataFrame,
    bundle: SessionContextBundle,
) -> pd.DataFrame:
    enriched = df.copy()
    for name, series in bundle.columns.items():
        enriched[name] = series
    return enriched


def compute_vol_regime(source: pd.Series, *, window: int, regime_lookback: int) -> pd.Series:
    vol = compute_realized_vol(source, int(window))
    return compute_rolling_rank(vol, int(regime_lookback))


def compute_trend_regime(source: pd.Series, ma_period: int) -> pd.Series:
    period = int(ma_period)
    ma = source.rolling(window=period, min_periods=period).mean()
    vol = compute_realized_vol(source, period)
    return (source - ma) / vol.replace(0.0, np.nan)


def compute_range_compression(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    window: int,
    regime_lookback: int,
    atr_period: int = 14,
) -> pd.Series:
    atr = compute_atr(high, low, close, int(atr_period))
    compression = (high - low) / atr.replace(0.0, np.nan)
    return compute_rolling_rank(compression, int(regime_lookback))


def compute_distance_atr(
    close: pd.Series,
    level: pd.Series,
    atr_period: int,
    *,
    high: pd.Series,
    low: pd.Series,
) -> pd.Series:
    atr = compute_atr(high, low, close, int(atr_period))
    return (close - level) / atr.replace(0.0, np.nan)


def compute_opening_range_levels(
    df: pd.DataFrame,
    *,
    session_open: str,
    range_minutes: int,
) -> tuple[pd.Series, pd.Series]:
    index = _datetime_index(df)
    minutes_from_open = compute_minutes_from_open(index, session_open)
    sessions = _session_dates(index)

    or_high = pd.Series(np.nan, index=index, dtype=float)
    or_low = pd.Series(np.nan, index=index, dtype=float)
    range_minutes = int(range_minutes)

    for session_date in sorted(sessions.unique()):
        mask = sessions == session_date
        window_mask = mask & (minutes_from_open <= range_minutes)
        if not window_mask.any():
            continue
        high_level = float(df.loc[window_mask, "high"].max())
        low_level = float(df.loc[window_mask, "low"].min())
        reveal_mask = mask & (minutes_from_open >= range_minutes)
        or_high.loc[reveal_mask] = high_level
        or_low.loc[reveal_mask] = low_level

    return or_high, or_low
