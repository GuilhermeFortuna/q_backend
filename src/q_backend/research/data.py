"""Fresh MT5 historical bars for research scripts."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import numpy as np

from q_backend.market_data.clients.shared import _time_msc_to_naive_local
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import COPY_TICKS_ALL
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.errors import NoMarketDataError
from q_backend.research.frame import (
    attach_metadata,
    bounds_to_brasilia_naive,
    drop_forming_bar,
    filter_index_range,
    normalize_timeframe,
    ohlcv_models_to_frame,
    parse_query_bound,
    PUBLIC_TZ_NAME,
    validate_bars_frame,
)
from q_backend.research.providers import resolve_gateway_token, resolve_gateway_url

MT5_SOURCE = "mt5"


def _exchange_now() -> datetime:
    return datetime.now(BRASILIA_TZ)


def _resolve_bounds(
    start: str | datetime,
    end: str | datetime | None,
    *,
    now: datetime,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_ts = parse_query_bound(start)
    if end is None:
        end_ts = pd.Timestamp(now)
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize(BRASILIA_TZ)
        else:
            end_ts = end_ts.tz_convert(BRASILIA_TZ)
    else:
        end_ts = parse_query_bound(end)
    if start_ts > end_ts:
        raise ValueError("start must be <= end")
    return start_ts, end_ts


def load_bars(
    symbol: str,
    *,
    timeframe: str,
    start: str | datetime,
    end: str | datetime | None = None,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> pd.DataFrame:
    """Fetch completed OHLCV bars from the connected MT5 terminal via the Q gateway."""
    sym = symbol.strip()
    if not sym:
        raise ValueError("symbol must be non-empty")
    tf = normalize_timeframe(timeframe)
    captured_now = _exchange_now()
    start_ts, end_ts = _resolve_bounds(start, end, now=captured_now)
    start_naive, end_naive = bounds_to_brasilia_naive(start_ts, end_ts)

    resolved_url = resolve_gateway_url(gateway_url)
    if not resolved_url:
        raise ValueError("MT5 gateway URL is not configured. Set Q_MT5_GATEWAY_URL or pass gateway_url=.")
    resolved_token = resolve_gateway_token(gateway_token)
    client = RemoteMt5Client(base_url=resolved_url, token=resolved_token)
    if not client.is_supported():
        raise ValueError("MT5 gateway URL is not configured. Set Q_MT5_GATEWAY_URL or pass gateway_url=.")

    bars = client.get_ohlcv(sym, tf, start_naive, end_naive)
    frame = ohlcv_models_to_frame(bars)
    frame = filter_index_range(frame, start_ts, end_ts)
    frame = drop_forming_bar(frame, tf, now=captured_now)
    if not frame.empty:
        validate_bars_frame(frame)
    if frame.empty:
        raise NoMarketDataError(
            symbol=sym,
            timeframe=tf,
            source=MT5_SOURCE,
            start=start_ts.isoformat(),
            end=end_ts.isoformat(),
        )
    return attach_metadata(
        frame,
        symbol=sym,
        timeframe=tf,
        source=MT5_SOURCE,
        requested_start=start_ts,
        requested_end=end_ts,
    )


def load_ticks(
    symbol: str,
    *,
    start: str | datetime,
    end: str | datetime | None = None,
    flags: int | None = None,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> pd.DataFrame:
    """Fetch fresh MT5 ticks as a DataFrame indexed by Brasília tick time."""
    sym = symbol.strip()
    if not sym:
        raise ValueError("symbol must be non-empty")
    captured_now = _exchange_now()
    start_ts, end_ts = _resolve_bounds(start, end, now=captured_now)
    start_naive, end_naive = bounds_to_brasilia_naive(start_ts, end_ts)

    resolved_url = resolve_gateway_url(gateway_url)
    if not resolved_url:
        raise ValueError("MT5 gateway URL is not configured. Set Q_MT5_GATEWAY_URL or pass gateway_url=.")
    resolved_token = resolve_gateway_token(gateway_token)
    client = RemoteMt5Client(base_url=resolved_url, token=resolved_token)
    if not client.is_supported():
        raise ValueError("MT5 gateway URL is not configured. Set Q_MT5_GATEWAY_URL or pass gateway_url=.")

    ticks = client.get_ticks_columnar(
        sym,
        start_naive,
        end_naive,
        flags=COPY_TICKS_ALL if flags is None else flags,
        use_cache=False,
    )
    times = pd.DatetimeIndex(
        [_time_msc_to_naive_local(int(value)) for value in ticks["time_msc"]],
        name="time",
    ).tz_localize(BRASILIA_TZ)
    frame = pd.DataFrame(
        {
            "bid": np.asarray(ticks["bid"], dtype=np.float64),
            "ask": np.asarray(ticks["ask"], dtype=np.float64),
            "last": np.asarray(ticks["last"], dtype=np.float64),
            "volume": np.asarray(ticks["volume"], dtype=np.float64),
            "flags": np.asarray(ticks["flags"], dtype=np.int32),
        },
        index=times,
    ).sort_index(kind="stable")
    frame = filter_index_range(frame, start_ts, end_ts)
    if frame.empty:
        raise NoMarketDataError(
            symbol=sym,
            timeframe="ticks",
            source=MT5_SOURCE,
            start=start_ts.isoformat(),
            end=end_ts.isoformat(),
        )
    frame.attrs["q_research"] = {
        "symbol": sym,
        "timeframe": "ticks",
        "source": MT5_SOURCE,
        "requested_start": start_ts.isoformat(),
        "requested_end": end_ts.isoformat(),
        "returned_start": frame.index[0].isoformat(),
        "returned_end": frame.index[-1].isoformat(),
        "timezone": PUBLIC_TZ_NAME,
    }
    return frame


def _resample_frequency(timeframe: str) -> str:
    tf = normalize_timeframe(timeframe)
    if tf == "MN1":
        return "MS"
    if tf == "W1":
        return "W-MON"
    if tf.startswith("M"):
        return f"{int(tf[1:])}min"
    if tf.startswith("H"):
        return f"{int(tf[1:])}h"
    return "1D"


def resample_ticks(ticks: pd.DataFrame, *, timeframe: str) -> pd.DataFrame:
    """Aggregate positive last-trade prices into MT5-aligned OHLCV bars."""
    if not isinstance(ticks.index, pd.DatetimeIndex):
        raise ValueError("ticks must have a DatetimeIndex")
    if ticks.index.tz is None:
        raise ValueError("ticks index must be timezone-aware")
    if "last" not in ticks.columns:
        raise ValueError("ticks must contain a 'last' column")
    tf = normalize_timeframe(timeframe)
    frequency = _resample_frequency(tf)
    prices = pd.to_numeric(ticks["last"], errors="raise").astype("float64")
    valid = prices.notna() & np.isfinite(prices) & (prices > 0)
    eligible = prices.loc[valid].sort_index(kind="stable")
    columns = ["open", "high", "low", "close", "tick_volume"]
    if eligible.empty:
        empty = pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz=ticks.index.tz, name="time"))
        empty["tick_volume"] = empty["tick_volume"].astype("int64")
        return empty

    grouped = eligible.resample(frequency, closed="left", label="left", origin="start_day")
    bars = grouped.ohlc()
    bars["tick_volume"] = grouped.count().astype("int64")
    full_index = pd.date_range(bars.index[0], bars.index[-1], freq=frequency, tz=bars.index.tz, name="time")
    bars = bars.reindex(full_index)
    bars["tick_volume"] = bars["tick_volume"].fillna(0).astype("int64")
    bars["close"] = bars["close"].ffill()
    for column in ("open", "high", "low"):
        bars[column] = bars[column].fillna(bars["close"])
    bars.attrs.update(ticks.attrs)
    bars.attrs["q_research"] = {
        **ticks.attrs.get("q_research", {}),
        "timeframe": tf,
    }
    return bars[columns]
