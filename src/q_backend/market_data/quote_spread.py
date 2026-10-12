"""Quoted bid-ask spread aggregated to bars from the research tick store.

Shared by the Market chart series service and the feature store, so both read the same
ticks through the same rules.

Bars and ticks are aligned on the tick store's clock. MT5 stores ``time_msc`` so that the
UTC wall clock of the epoch reads as Brasília local time; a bar's open is converted to that
same clock and its close is the open plus the timeframe's duration. Each bar belongs to the
session of its open's calendar day and is aggregated from that day's ticks only, so a value
never depends on another day or on where the requested range starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from q_backend.backtesting.technical_indicators import (
    compute_per_bar_volatility,
    compute_quote_spread_bars,
    compute_vol_normalize,
)
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.tick_store import TickStore

SPREAD_OUTPUTS: tuple[str, ...] = ("last", "time_weighted_mean", "max")


@dataclass(frozen=True)
class QuoteSpread:
    """Relative quoted spread per bar; ``unavailable_reason`` is set when no bar has ticks."""

    last: np.ndarray
    time_weighted_mean: np.ndarray
    max: np.ndarray
    coverage: np.ndarray
    unavailable_reason: str | None = None

    def select(self, output: str, min_coverage: float) -> np.ndarray:
        """One statistic, missing where the bar's coverage is below ``min_coverage``."""
        if output not in SPREAD_OUTPUTS:
            raise ValueError(f"Spread output must be one of: {', '.join(SPREAD_OUTPUTS)} (got {output!r}).")
        values = np.array(getattr(self, output), dtype=np.float64)
        values[self.coverage < min_coverage] = np.nan
        return values


def open_tick_store(symbol: str, *, root: str | None = None) -> TickStore | None:
    """The tick store for ``symbol``: ``root``, else ``Q_RESEARCH_TICK_STORE``, else the default."""
    try:
        return TickStore(symbol, root=root)
    except ValueError:
        return None


def _naive_local_index(times: pd.Series | pd.Index) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(times)
    if index.tz is not None:
        index = index.tz_convert(BRASILIA_TZ).tz_localize(None)
    return index


def bar_bounds_msc(times: pd.Series | pd.Index, timeframe: str) -> tuple[np.ndarray, np.ndarray]:
    """Bar ``[open, close)`` bounds on the tick store's ``time_msc`` clock."""
    opens = _naive_local_index(times).as_unit("ms").asi8
    duration_ms = int(bar_duration(timeframe) / pd.Timedelta(milliseconds=1))
    return opens, opens + duration_ms


def _session_key(day: date) -> int:
    return day.year * 10_000 + day.month * 100 + day.day


def quote_spread(
    store: TickStore | None,
    symbol: str,
    times: pd.Series | pd.Index,
    timeframe: str,
    *,
    max_quote_age_ms: int,
) -> QuoteSpread:
    """Aggregate each bar's quoted spread from its own session's ticks."""
    count = len(times)
    last = np.full(count, np.nan)
    mean = np.full(count, np.nan)
    peak = np.full(count, np.nan)
    coverage = np.zeros(count)

    def result(reason: str | None) -> QuoteSpread:
        return QuoteSpread(last, mean, peak, coverage, reason)

    if count == 0:
        return result(None)
    if store is None:
        return result(f"The tick store is not available for {symbol}.")

    bar_open, bar_close = bar_bounds_msc(times, timeframe)
    bar_days = _naive_local_index(times).date
    days_with_ticks = 0
    for day in sorted(set(bar_days)):
        if not store.has_session(day):
            continue
        days_with_ticks += 1
        rows = np.flatnonzero(bar_days == day)
        time_msc, bid, ask, flags = store.quotes(day)
        session = np.full(time_msc.shape, _session_key(day), dtype=np.int64)
        day_last, day_mean, day_peak, day_coverage = compute_quote_spread_bars(
            time_msc, bid, ask, flags, session, bar_open[rows], bar_close[rows], max_quote_age_ms
        )
        last[rows] = day_last
        mean[rows] = day_mean
        peak[rows] = day_peak
        coverage[rows] = day_coverage

    if days_with_ticks == 0:
        first, final = min(bar_days), max(bar_days)
        return result(f"No ticks are stored for {symbol} between {first.isoformat()} and {final.isoformat()}.")
    return result(None)


def spread_vs_volatility(
    frame: pd.DataFrame,
    spread: np.ndarray,
    *,
    vol_estimator: str,
    vol_window: int,
    vol_half_life: float,
    vol_lag: int,
) -> pd.Series:
    """Relative spread divided by per-bar volatility, on ``frame``'s index."""
    sigma = compute_per_bar_volatility(
        frame["open"],
        frame["high"],
        frame["low"],
        frame["close"],
        estimator=vol_estimator,
        window=vol_window if vol_estimator != "ewma" else None,
        half_life=vol_half_life if vol_estimator == "ewma" else None,
    )
    return compute_vol_normalize(pd.Series(spread, index=frame.index), sigma, 1, vol_lag)
