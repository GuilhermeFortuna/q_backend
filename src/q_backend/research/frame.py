"""OHLCV frame normalization and validation for the research library."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from q_backend.execution.bars import bar_close_time as _fixed_bar_close_time
from q_backend.market_data.clients.metatrader import TIMEFRAME_NAMES
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.timezone import BRASILIA_TZ

PUBLIC_TZ = BRASILIA_TZ
PUBLIC_TZ_NAME = "America/Sao_Paulo"

FRAME_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)


def normalize_timeframe(timeframe: str) -> str:
    tf = timeframe.strip().upper()
    if tf not in TIMEFRAME_NAMES:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_NAMES)}")
    return tf


def _is_date_only_string(value: str) -> bool:
    text = value.strip()
    if len(text) != 10:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return "T" not in text and " " not in text


def parse_query_bound(value: str | datetime) -> pd.Timestamp:
    """Parse an inclusive bar-open bound in America/Sao_Paulo."""
    if isinstance(value, str):
        text = value.strip()
        if _is_date_only_string(text):
            ts = pd.Timestamp(text)
            return ts.tz_localize(BRASILIA_TZ)
        ts = pd.Timestamp(text)
        if ts.tzinfo is None:
            return ts.tz_localize(BRASILIA_TZ)
        return ts.tz_convert(BRASILIA_TZ)

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize(BRASILIA_TZ)
    return ts.tz_convert(BRASILIA_TZ)


def parse_bounds(start: str | datetime, end: str | datetime) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_ts = parse_query_bound(start)
    end_ts = parse_query_bound(end)
    if start_ts > end_ts:
        raise ValueError("start must be <= end")
    return start_ts, end_ts


def bounds_to_brasilia_naive(start: pd.Timestamp, end: pd.Timestamp) -> tuple[datetime, datetime]:
    """Catalog and lake reads use naive Brasília wall-clock instants."""
    return (
        start.tz_convert(BRASILIA_TZ).tz_localize(None).to_pydatetime(),
        end.tz_convert(BRASILIA_TZ).tz_localize(None).to_pydatetime(),
    )


def bar_close_time(bar_open_time: datetime, timeframe: str) -> datetime:
    """Return the close instant for a bar open time in the research timezone contract."""
    tf = timeframe.upper()
    if tf == "MN1":
        ts = pd.Timestamp(bar_open_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize(BRASILIA_TZ)
        else:
            ts = ts.tz_convert(BRASILIA_TZ)
        return (ts + pd.DateOffset(months=1)).to_pydatetime()
    if tf == "W1":
        ts = pd.Timestamp(bar_open_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize(BRASILIA_TZ)
        else:
            ts = ts.tz_convert(BRASILIA_TZ)
        return (ts + pd.Timedelta(weeks=1)).to_pydatetime()
    try:
        bar_duration(tf)
    except ValueError:
        raise ValueError(f"Unsupported timeframe '{timeframe}' for bar completion") from None
    return _fixed_bar_close_time(bar_open_time, tf)


def is_bar_complete(bar_open_time: datetime, timeframe: str, *, now: datetime) -> bool:
    close_at = bar_close_time(bar_open_time, timeframe)
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize(BRASILIA_TZ)
    else:
        now_ts = now_ts.tz_convert(BRASILIA_TZ)
    close_ts = pd.Timestamp(close_at)
    if close_ts.tzinfo is None:
        close_ts = close_ts.tz_localize(BRASILIA_TZ)
    else:
        close_ts = close_ts.tz_convert(BRASILIA_TZ)
    return close_ts <= now_ts


def drop_forming_bar(frame: pd.DataFrame, timeframe: str, *, now: datetime) -> pd.DataFrame:
    if frame.empty:
        return frame
    last_open = frame.index[-1]
    open_dt = last_open.to_pydatetime() if hasattr(last_open, "to_pydatetime") else last_open
    if is_bar_complete(open_dt, timeframe, now=now):
        return frame
    return frame.iloc[:-1]


def _validate_prices(frame: pd.DataFrame) -> None:
    for col in ("open", "high", "low", "close"):
        if col not in frame.columns:
            raise ValueError(f"Missing required OHLC column: {col}")
        values = frame[col].to_numpy(dtype=np.float64, copy=False)
        if not np.all(np.isfinite(values)):
            raise ValueError(f"Non-finite values in column '{col}'")
        if np.any(values <= 0):
            raise ValueError(f"Non-positive prices in column '{col}'")
    low = frame["low"].to_numpy(dtype=np.float64, copy=False)
    high = frame["high"].to_numpy(dtype=np.float64, copy=False)
    for col in ("open", "close"):
        mid = frame[col].to_numpy(dtype=np.float64, copy=False)
        if np.any(mid < low) or np.any(mid > high):
            raise ValueError(f"Invalid OHLC ordering: low <= {col} <= high required")


def _validate_volumes(frame: pd.DataFrame) -> None:
    if "tick_volume" not in frame.columns:
        raise ValueError("Missing required column: tick_volume")
    tv = frame["tick_volume"].to_numpy(dtype=np.int64, copy=False)
    if not np.all(np.isfinite(tv)):
        raise ValueError("Non-finite tick_volume values")
    if np.any(tv < 0):
        raise ValueError("Negative tick_volume values")
    for col in ("spread", "real_volume"):
        if col not in frame.columns:
            continue
        values = frame[col].to_numpy(dtype=np.float64, copy=False)
        if not np.all(np.isfinite(values) | np.isnan(values)):
            raise ValueError(f"Non-finite values in optional column '{col}'")
        if np.any(values[np.isfinite(values)] < 0):
            raise ValueError(f"Negative values in optional column '{col}'")


def validate_bars_frame(frame: pd.DataFrame) -> None:
    if frame.index.has_duplicates:
        raise ValueError("Duplicate bar timestamps are not allowed")
    _validate_prices(frame)
    _validate_volumes(frame)


def ohlcv_models_to_frame(bars) -> pd.DataFrame:
    if not bars:
        return empty_bars_frame()
    from q_backend.market_data.models import OHLCV

    rows: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, OHLCV):
            raise TypeError("Expected OHLCV models from the remote provider")
        rows.append(bar.model_dump())
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize(BRASILIA_TZ)
    else:
        df["time"] = df["time"].dt.tz_convert(BRASILIA_TZ)
    df = df.set_index("time").sort_index()
    for col in ("spread", "real_volume"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = df[col].astype("float64")
    return _coerce_frame_dtypes(df)


def _coerce_frame_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in ("open", "high", "low", "close", "spread", "real_volume"):
        if col in out.columns:
            out[col] = out[col].astype("float64")
    if "tick_volume" in out.columns:
        out["tick_volume"] = out["tick_volume"].astype("int64")
    for col in FRAME_COLUMNS:
        if col not in out.columns:
            if col in ("spread", "real_volume"):
                out[col] = np.nan
            else:
                raise ValueError(f"Missing required column: {col}")
    return out[list(FRAME_COLUMNS)]


def empty_bars_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], name="time", tz=BRASILIA_TZ)
    data = {col: pd.Series(dtype="float64" if col != "tick_volume" else "int64") for col in FRAME_COLUMNS}
    data["tick_volume"] = pd.Series(dtype="int64")
    return pd.DataFrame(data, index=idx)


def filter_index_range(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame
    start_aware = start.tz_convert(BRASILIA_TZ)
    end_aware = end.tz_convert(BRASILIA_TZ)
    mask = (frame.index >= start_aware) & (frame.index <= end_aware)
    return frame.loc[mask]


def off_tick_share(frame: pd.DataFrame, tick_size: float) -> float:
    """Fraction of open/high/low/close values that are not multiples of ``tick_size``."""
    if tick_size <= 0 or not np.isfinite(tick_size):
        raise ValueError("tick_size must be a finite positive number")
    columns = ("open", "high", "low", "close")
    chunks = [frame[col].to_numpy(dtype=np.float64, copy=False) for col in columns]
    values = np.concatenate(chunks) if chunks else np.array([], dtype=np.float64)
    if values.size == 0:
        return 0.0
    scaled = values / tick_size
    nearest = np.round(scaled)
    off = ~np.isclose(scaled, nearest, rtol=0.0, atol=1e-9)
    return float(np.mean(off))


def attach_metadata(
    frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source: str,
    requested_start: pd.Timestamp,
    requested_end: pd.Timestamp,
) -> pd.DataFrame:
    out = frame.copy()
    if out.empty:
        returned_start = None
        returned_end = None
    else:
        returned_start = out.index[0].isoformat()
        returned_end = out.index[-1].isoformat()
    out.attrs["q_research"] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": source,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "returned_start": returned_start,
        "returned_end": returned_end,
        "timezone": PUBLIC_TZ_NAME,
    }
    return out
