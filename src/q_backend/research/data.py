"""Fresh MT5 historical bars for research scripts."""

from __future__ import annotations

import warnings
from datetime import datetime
from numbers import Real

import pandas as pd
import numpy as np

from q_backend.market_data.clients.shared import _time_msc_to_naive_local
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import COPY_TICKS_ALL
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.errors import AdjustedSeriesWarning, NoMarketDataError
from q_backend.research.frame import (
    attach_metadata,
    bounds_to_brasilia_naive,
    drop_forming_bar,
    filter_index_range,
    normalize_timeframe,
    off_tick_share,
    ohlcv_models_to_frame,
    parse_query_bound,
    PUBLIC_TZ_NAME,
    validate_bars_frame,
)
from q_backend.research.providers import resolve_gateway_token, resolve_gateway_url

MT5_SOURCE = "mt5"
OFF_TICK_WARN_THRESHOLD = 0.01
_GUIDE_SECTION = "Choosing a price series (docs/research-library.md)"


def _trade_tick_size(symbol_info: dict[str, object] | None) -> float | None:
    if not isinstance(symbol_info, dict):
        return None
    raw = symbol_info.get("trade_tick_size")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, Real):
        return None
    tick = float(raw)
    if not np.isfinite(tick) or tick <= 0:
        return None
    return tick


def _record_tick_grid_check(
    frame: pd.DataFrame,
    *,
    symbol: str,
    client: RemoteMt5Client,
) -> pd.DataFrame:
    try:
        symbol_info = client.get_symbol_info(symbol)
    except ConnectionError:
        return frame
    tick_size = _trade_tick_size(symbol_info)
    if tick_size is None:
        return frame
    share = off_tick_share(frame, tick_size)
    meta = dict(frame.attrs.get("q_research", {}))
    meta["tick_size"] = tick_size
    meta["off_tick_share"] = share
    out = frame.copy()
    out.attrs["q_research"] = meta
    if share > OFF_TICK_WARN_THRESHOLD:
        pct = share * 100.0
        warnings.warn(
            (
                f"{symbol}: {pct:.1f}% of loaded OHLC prices are off the {tick_size} tick grid. "
                "Point-based profit and loss and per-contract costs are distorted on "
                f"price-adjusted history. See {_GUIDE_SECTION}."
            ),
            AdjustedSeriesWarning,
            stacklevel=3,
        )
    return out


def _ticks_frame_from_columnar(arrays: dict[str, np.ndarray]) -> pd.DataFrame:
    """Build the research tick frame from gateway columnar arrays."""
    times = pd.DatetimeIndex(
        [_time_msc_to_naive_local(int(value)) for value in arrays["time_msc"]],
        name="time",
    ).tz_localize(BRASILIA_TZ)
    frame = pd.DataFrame(
        {
            "bid": np.asarray(arrays["bid"], dtype=np.float64),
            "ask": np.asarray(arrays["ask"], dtype=np.float64),
            "last": np.asarray(arrays["last"], dtype=np.float64),
            "volume": np.asarray(arrays["volume"], dtype=np.float64),
            "flags": _decode_tick_flags(np.asarray(arrays["flags"], dtype=np.int64)),
        },
        index=times,
    ).sort_index(kind="stable")
    return frame


def _decode_tick_flags(flags: np.ndarray) -> np.ndarray:
    """Describe public MT5 bits, preserving undocumented bits without guessing their meaning."""
    public_bits = (
        (2, "bid update"),
        (4, "ask update"),
        (8, "last-price update"),
        (16, "volume update"),
        (32, "buy trade"),
        (64, "sell trade"),
    )
    public_mask = sum(bit for bit, _ in public_bits)
    values, inverse = np.unique(flags, return_inverse=True)
    labels = []
    for value in values:
        value = int(value)
        parts = [label for bit, label in public_bits if value & bit]
        undocumented = value & ~public_mask
        if undocumented:
            parts.append(f"undocumented bits ({undocumented})")
        labels.append(" | ".join(parts) if parts else "no flags")
    return np.asarray(labels, dtype=object)[inverse]


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
    """
    Fetch completed OHLCV bars from MetaTrader 5 via the Q gateway.

    Requests completed candles for the specified symbol and timeframe, automatically
    paging through the gateway in 50,000-bar chunks. Excludes the current forming bar.

    Parameters
    ----------
    symbol : str
        Trading symbol name (e.g., ``'WIN$N'``, ``'WDO$N'``). Must be non-empty.
    timeframe : str
        Canonical MT5 timeframe identifier (e.g., ``'M1'``, ``'M5'``, ``'H1'``, ``'D1'``).
        Case-insensitive.
    start : str or datetime
        Inclusive start timestamp. Date-only strings (``'YYYY-MM-DD'``) or naive datetimes
        are interpreted in Brasília exchange time (``America/Sao_Paulo``).
    end : str or datetime, optional
        Inclusive end timestamp. Defaults to current exchange time. A date-only string
        evaluates to midnight at the start of that day.
    gateway_url : str, optional
        Override for ``Q_MT5_GATEWAY_URL`` for this call only.
    gateway_token : str, optional
        Override for ``Q_MT5_GATEWAY_TOKEN`` for this call only.

    Returns
    -------
    pandas.DataFrame
        DataFrame indexed by timezone-aware Brasília timestamps (``time``) with columns:
        ``open``, ``high``, ``low``, ``close``, ``tick_volume``, ``spread``, ``real_volume``.
        Metadata is attached to ``frame.attrs["q_research"]``.

    Raises
    ------
    ValueError
        If ``symbol`` is empty, ``timeframe`` is invalid, or ``start > end``.
    ConnectionError
        If the Q MT5 gateway is unreachable.
    NoMarketDataError
        If no completed bars exist in the requested interval.

    Warns
    -----
    AdjustedSeriesWarning
        Emitted when more than 1% of prices fall off the symbol's tick grid (typical
        for proportionally adjusted continuous series like ``WIN$``).

    See Also
    --------
    load_ticks : Fetch raw trade and quote ticks from MetaTrader 5.
    resample_ticks : Resample trade ticks into custom OHLCV bars.

    Examples
    --------
    >>> from q_backend.research import load_bars
    >>> bars = load_bars("WIN$N", timeframe="M5", start="2026-09-01")
    >>> bars[["open", "high", "low", "close"]].head(2)
                               open      high       low     close
    time
    2026-09-01 09:00:00-03:00  135000.0  135250.0  134950.0  135100.0
    2026-09-01 09:05:00-03:00  135100.0  135300.0  135050.0  135200.0
    """
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
    frame = attach_metadata(
        frame,
        symbol=sym,
        timeframe=tf,
        source=MT5_SOURCE,
        requested_start=start_ts,
        requested_end=end_ts,
    )
    return _record_tick_grid_check(frame, symbol=sym, client=client)


def load_ticks(
    symbol: str,
    *,
    start: str | datetime,
    end: str | datetime | None = None,
    flags: int | None = None,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> pd.DataFrame:
    """
    Fetch fresh MT5 ticks indexed by Brasília time with human-readable flag strings.

    Retrieves high-resolution trade and quote events. Consecutive rows maintain
    stable ordering for same-millisecond timestamps.

    Parameters
    ----------
    symbol : str
        Trading symbol name (e.g., ``'WIN$N'``). Must be non-empty.
    start : str or datetime
        Inclusive start timestamp in Brasília time.
    end : str or datetime, optional
        Inclusive end timestamp. Defaults to current exchange time.
    flags : int, optional
        Numeric MT5 tick category mask to fetch (e.g., ``COPY_TICKS_ALL``). Defaults
        to all available tick events.
    gateway_url : str, optional
        Override for ``Q_MT5_GATEWAY_URL`` for this call only.
    gateway_token : str, optional
        Override for ``Q_MT5_GATEWAY_TOKEN`` for this call only.

    Returns
    -------
    pandas.DataFrame
        DataFrame indexed by timezone-aware Brasília timestamps (``time``) with columns:
        ``bid``, ``ask``, ``last``, ``volume``, ``flags``. The ``flags`` column contains
        readable strings (e.g., ``'last-price update | volume update | buy trade'``).

    Raises
    ------
    ValueError
        If ``symbol`` is empty or ``start > end``.
    ConnectionError
        If the Q MT5 gateway is unreachable.
    NoMarketDataError
        If no ticks exist in the requested interval.

    See Also
    --------
    load_bars : Fetch completed OHLCV candles.
    resample_ticks : Resample tick events into candles.
    """
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
    frame = _ticks_frame_from_columnar(ticks)
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
    """
    Aggregate positive last-trade prices into MT5-aligned OHLCV bars.

    Filters ticks for positive traded prices (excluding quote-only updates),
    resamples into candle intervals matching MetaTrader 5 frequency rules,
    and forward-fills empty intervals between the first and last trade.

    Parameters
    ----------
    ticks : pandas.DataFrame
        DataFrame of raw tick events containing a ``'last'`` column and a
        timezone-aware DatetimeIndex.
    timeframe : str
        Canonical MT5 timeframe identifier (e.g., ``'M1'``, ``'M5'``, ``'H1'``).

    Returns
    -------
    pandas.DataFrame
        DataFrame indexed by candle open timestamp with columns:
        ``open``, ``high``, ``low``, ``close``, and ``tick_volume``.

    Raises
    ------
    ValueError
        If ``ticks`` does not have a timezone-aware DatetimeIndex, lacks a ``'last'``
        column, or if ``timeframe`` is invalid.

    See Also
    --------
    load_ticks : Fetch raw tick events from MetaTrader 5.
    load_bars : Fetch pre-aggregated completed candles.
    """
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
