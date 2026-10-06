"""Fresh MT5 historical bars for research scripts."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from q_backend.market_data.clients.remote import RemoteMt5Client
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
