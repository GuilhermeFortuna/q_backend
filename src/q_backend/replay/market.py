"""Causal market data for the replay gateway: every answer is limited to a visibility watermark.

Canonical ticks are stored in UTC milliseconds. The MT5 wire convention (used by the existing
``RemoteMt5Client``) encodes broker wall-clock time as if it were UTC, so bar times and tick
``time_msc`` values are converted to Brasília wall clock at the boundary, using the offset that
applies at each instant. ``/v1/trades`` alone uses true UTC, as its contract requires.

``ReplayMarket`` is immutable after construction. Each query takes the ``visible_us`` watermark
captured at request ingress and never reads a tick or bar after it, so a slow or pending request
cannot reveal future market state. Pure functions here are shared by the HTTP gateway and tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np

from q_backend.replay.dataset import SymbolSpec

# MT5 tick flags as used by the terminal and by the research tick store.
TICK_FLAG_LAST = 8
TICK_FLAG_VOLUME = 16
TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64
TRADE_FLAGS = TICK_FLAG_LAST | TICK_FLAG_VOLUME | TICK_FLAG_BUY | TICK_FLAG_SELL
COPY_TICKS_ALL = "all"
COPY_TICKS_TRADE = "trade"

TIMEFRAME_SECONDS: Mapping[str, int] = {
    "M1": 60,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}
MAX_PAGE_ROWS = 1_000_000


class MarketError(Exception):
    """A request the gateway refuses, with the wire status and code it maps to."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class TickSeries:
    """Immutable, time-ordered ticks of one symbol in UTC milliseconds."""

    symbol: str
    time_msc: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    last: np.ndarray
    volume: np.ndarray
    flags: np.ndarray

    def __post_init__(self) -> None:
        count = len(self.time_msc)
        for name in ("bid", "ask", "last", "volume", "flags"):
            if len(getattr(self, name)) != count:
                raise ValueError(f"{self.symbol}: column {name} length differs from time_msc")
        if count and np.any(self.time_msc[1:] < self.time_msc[:-1]):
            raise ValueError(f"{self.symbol}: ticks must be time-ordered")


def offset_ms(utc_ms: np.ndarray, zone: ZoneInfo) -> np.ndarray:
    """UTC offset in milliseconds at each instant, evaluated once per distinct hour."""
    hours = utc_ms // 3_600_000
    unique_hours, inverse = np.unique(hours, return_inverse=True)
    offsets = np.empty(len(unique_hours), dtype=np.int64)
    for index, hour in enumerate(unique_hours):
        instant = datetime.fromtimestamp(int(hour) * 3600, tz=UTC).astimezone(zone)
        offsets[index] = int(instant.utcoffset().total_seconds() * 1000)  # type: ignore[union-attr]
    return offsets[inverse]


def utc_to_wire_ms(utc_ms: np.ndarray, zone: ZoneInfo) -> np.ndarray:
    """Broker wall clock encoded as UTC milliseconds, the MT5 wire convention."""
    return utc_ms + offset_ms(utc_ms, zone)


def wire_to_utc_ms(wire_ms: int, zone: ZoneInfo) -> int:
    """Inverse of :func:`utc_to_wire_ms` for one value (two passes handle offset changes)."""
    guess = wire_ms - int(offset_ms(np.array([wire_ms], dtype=np.int64), zone)[0])
    return wire_ms - int(offset_ms(np.array([guess], dtype=np.int64), zone)[0])


def naive_local_to_utc_ms(text: str, zone: ZoneInfo) -> int:
    """Parse a naive Brasília ISO datetime (the gateway's query convention) to UTC milliseconds."""
    try:
        naive = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarketError(400, "invalid_datetime", f"invalid datetime: {text!r}") from exc
    if naive.tzinfo is not None:
        raise MarketError(400, "invalid_datetime", "this endpoint takes naive Brasília datetimes")
    aware = naive.replace(tzinfo=zone)
    return int(aware.astimezone(UTC).timestamp() * 1000)


def naive_local_to_wire_seconds(text: str) -> int:
    """Wire bar time of a naive Brasília datetime: its wall clock read as UTC epoch seconds."""
    try:
        naive = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarketError(400, "invalid_datetime", f"invalid datetime: {text!r}") from exc
    if naive.tzinfo is not None:
        raise MarketError(400, "invalid_datetime", "this endpoint takes naive Brasília datetimes")
    return int(naive.replace(tzinfo=UTC).timestamp())


def _ensure_timeframe(timeframe: str) -> int:
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        raise MarketError(400, "invalid_timeframe", f"unsupported timeframe {timeframe!r}")
    return seconds


def _bars_from_ticks(
    series: TickSeries,
    *,
    visible_ms: int,
    timeframe: str,
    zone: ZoneInfo,
    point: float,
    lower_utc_ms: int | None = None,
) -> tuple[dict[str, np.ndarray], int | None]:
    """Causal OHLCV from positive last prices visible at ``visible_ms``.

    Returns the bars and the wire time of the bar still forming at the watermark (or None).
    Bars are aligned to local wall-clock periods, as the research tick store does with its
    ``origin='start_day'`` resampling, and only trade-carrying ticks contribute real volume.
    """
    period = _ensure_timeframe(timeframe)
    stop = int(np.searchsorted(series.time_msc, visible_ms, side="right"))
    start = 0 if lower_utc_ms is None else int(np.searchsorted(series.time_msc, lower_utc_ms, side="left"))
    times = series.time_msc[start:stop]
    last = series.last[start:stop]
    valid = np.isfinite(last) & (last > 0)
    times = times[valid]
    prices = last[valid]
    volume = series.volume[start:stop][valid]
    flags = series.flags[start:stop][valid]
    bid = series.bid[start:stop][valid]
    ask = series.ask[start:stop][valid]
    empty = {
        "time": np.array([], dtype=np.int64),
        "open": np.array([], dtype=np.float64),
        "high": np.array([], dtype=np.float64),
        "low": np.array([], dtype=np.float64),
        "close": np.array([], dtype=np.float64),
        "tick_volume": np.array([], dtype=np.int64),
        "spread": np.array([], dtype=np.int64),
        "real_volume": np.array([], dtype=np.int64),
    }
    if len(times) == 0:
        return empty, None

    wire = utc_to_wire_ms(times, zone)
    local_seconds = wire // 1000
    bucket = local_seconds - (local_seconds % period)
    change = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [len(bucket)]))
    trade_volume = np.where((flags & TRADE_FLAGS) != 0, volume, 0.0)
    spread = np.rint((ask - bid) / point).astype(np.int64)
    bars = {
        "time": bucket[starts].astype(np.int64),
        "open": prices[starts],
        "high": np.maximum.reduceat(prices, starts),
        "low": np.minimum.reduceat(prices, starts),
        "close": prices[ends - 1],
        "tick_volume": (ends - starts).astype(np.int64),
        "spread": spread[ends - 1],
        "real_volume": np.add.reduceat(trade_volume, starts).astype(np.int64),
    }
    # The last bar is still forming while the watermark has not reached its wall-clock end.
    last_bucket_end_wire = int(bucket[-1]) + period
    last_bucket_end_utc = wire_to_utc_ms(last_bucket_end_wire * 1000, zone)
    forming = int(bucket[-1]) if visible_ms < last_bucket_end_utc else None
    return bars, forming


class ReplayMarket:
    """Immutable market data for one run generation. Each query takes a visibility watermark."""

    def __init__(
        self,
        *,
        generation: str,
        series: Mapping[str, TickSeries],
        specs: Mapping[str, SymbolSpec],
        zone: ZoneInfo,
    ) -> None:
        if set(series) != set(specs):
            raise ValueError("every series needs a symbol spec and the reverse")
        self._generation = generation
        self._series = dict(series)
        self._specs = dict(specs)
        self._zone = zone

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def _series_for(self, symbol: str) -> TickSeries:
        series = self._series.get(symbol)
        if series is None:
            raise MarketError(404, "symbol_not_found", f"unknown symbol {symbol!r}")
        return series

    def _spec_for(self, symbol: str) -> SymbolSpec:
        spec = self._specs.get(symbol)
        if spec is None:
            raise MarketError(404, "symbol_not_found", f"unknown symbol {symbol!r}")
        return spec

    def quote_at(self, symbol: str, utc_us: int) -> tuple[float, float, int] | None:
        """Latest priced quote at or before ``utc_us``: (bid, ask, time_msc UTC), or None."""
        series = self._series_for(symbol)
        index = int(np.searchsorted(series.time_msc, utc_us // 1000, side="right")) - 1
        while index >= 0:
            bid = float(series.bid[index])
            ask = float(series.ask[index])
            if bid > 0 and ask > 0 and ask >= bid:
                return bid, ask, int(series.time_msc[index])
            index -= 1
        return None

    def tick_count(self, symbol: str) -> int:
        return len(self._series_for(symbol).time_msc)

    def tick_at(self, symbol: str, index: int) -> tuple[int, float, float]:
        """(time_msc UTC, bid, ask) of one tick in source order."""
        series = self._series_for(symbol)
        return int(series.time_msc[index]), float(series.bid[index]), float(series.ask[index])

    def first_index_at_or_after(self, symbol: str, utc_ms: int) -> int:
        return int(np.searchsorted(self._series_for(symbol).time_msc, utc_ms, side="left"))

    def spec(self, symbol: str) -> SymbolSpec:
        return self._spec_for(symbol)

    # -- symbols ------------------------------------------------------------
    def symbol_info(self, symbol: str) -> dict:
        spec = self._spec_for(symbol)
        return {
            "name": symbol,
            "description": spec.description,
            "point": spec.point,
            "digits": spec.digits,
            "volume_min": spec.volume_min,
            "volume_step": spec.volume_step,
            "volume_max": spec.volume_max,
            "trade_contract_size": spec.trade_contract_size,
            "margin_per_contract": spec.margin_per_contract,
            "visible": True,
            "select": True,
        }

    def search_symbols(self, query: str) -> list[dict]:
        needle = query.upper()
        return [
            {"name": name, "description": spec.description}
            for name, spec in sorted(self._specs.items())
            if needle in name.upper()
        ]

    # -- bars ---------------------------------------------------------------
    def ohlcv(
        self, symbol: str, timeframe: str, *, start_wire_s: int, end_wire_s: int, visible_us: int
    ) -> tuple[dict[str, np.ndarray], int | None]:
        """Bars whose wire open time lies in ``[start, end)``, visible at the watermark."""
        series = self._series_for(symbol)
        spec = self._spec_for(symbol)
        visible_ms = visible_us // 1000
        bars, forming = _bars_from_ticks(
            series,
            visible_ms=visible_ms,
            timeframe=timeframe,
            zone=self._zone,
            point=spec.point,
            lower_utc_ms=wire_to_utc_ms(start_wire_s * 1000, self._zone),
        )
        keep = (bars["time"] >= start_wire_s) & (bars["time"] < end_wire_s)
        return {name: column[keep] for name, column in bars.items()}, forming

    def recent_ohlcv(self, symbol: str, timeframe: str, count: int, visible_us: int):
        series = self._series_for(symbol)
        spec = self._spec_for(symbol)
        bars, forming = _bars_from_ticks(
            series,
            visible_ms=visible_us // 1000,
            timeframe=timeframe,
            zone=self._zone,
            point=spec.point,
        )
        return {name: column[-count:] for name, column in bars.items()}, forming

    def available_range(self, symbol: str, timeframe: str, visible_us: int) -> dict | None:
        # 1970 to 2100 covers every wire timestamp a dataset can contain.
        bars, _ = self.ohlcv(symbol, timeframe, start_wire_s=0, end_wire_s=4_102_444_800, visible_us=visible_us)
        count = len(bars["time"])
        if count == 0:
            return None
        first = datetime.fromtimestamp(int(bars["time"][0]), tz=UTC).replace(tzinfo=None)
        last = datetime.fromtimestamp(int(bars["time"][-1]), tz=UTC).replace(tzinfo=None)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "start": first.isoformat(),
            "end": last.isoformat(),
            "bar_count": count,
        }

    # -- ticks --------------------------------------------------------------
    def ticks(self, symbol: str, *, start_utc_ms: int, end_utc_ms: int, flags: str, visible_us: int):
        """Columns for ticks in ``[start, end)`` visible at the watermark, time in wire encoding."""
        series = self._series_for(symbol)
        if flags not in (COPY_TICKS_ALL, COPY_TICKS_TRADE):
            raise MarketError(400, "invalid_flags", f"unsupported flags {flags!r}")
        visible_ms = visible_us // 1000
        stop = int(np.searchsorted(series.time_msc, min(end_utc_ms, visible_ms + 1), side="left"))
        start = int(np.searchsorted(series.time_msc, start_utc_ms, side="left"))
        columns = self._slice(series, start, stop)
        if flags == COPY_TICKS_TRADE:
            keep = (columns["flags"] & TRADE_FLAGS) != 0
            columns = {name: column[keep] for name, column in columns.items()}
        if len(columns["time_msc"]):
            columns["time_msc"] = utc_to_wire_ms(columns["time_msc"], self._zone)
        return columns

    def ticks_page(self, symbol: str, *, start_wire: int, end_wire: int, limit: int, visible_us: int):
        """Bounded forward page following the MT5 gateway cursor rule.

        A saturated page withholds its final timestamp group, and the cursor is that group's
        time, so duplicates are never split or lost across pages. A single group larger than
        the limit is returned whole.
        """
        if limit < 1 or limit > MAX_PAGE_ROWS:
            raise MarketError(400, "invalid_limit", "limit must be between 1 and 1000000")
        series = self._series_for(symbol)
        start_utc = wire_to_utc_ms(start_wire, self._zone)
        end_utc = wire_to_utc_ms(end_wire, self._zone)
        upper = min(end_utc, visible_us // 1000 + 1)
        start = int(np.searchsorted(series.time_msc, start_utc, side="left"))
        stop = int(np.searchsorted(series.time_msc, upper, side="left"))
        cursor_utc: int | None = None
        if stop - start <= limit:
            take_end = stop
        else:
            last_time = int(series.time_msc[start + limit - 1])
            take_end = int(np.searchsorted(series.time_msc, last_time, side="left"))
            if take_end > start:
                cursor_utc = last_time
            else:
                # The first timestamp group alone exceeds the limit: return it whole.
                take_end = int(np.searchsorted(series.time_msc, last_time, side="right"))
                if take_end < stop:
                    cursor_utc = int(series.time_msc[take_end])
        columns = self._slice(series, start, take_end)
        if len(columns["time_msc"]):
            columns["time_msc"] = utc_to_wire_ms(columns["time_msc"], self._zone)
        cursor = None
        if cursor_utc is not None:
            cursor = int(utc_to_wire_ms(np.array([cursor_utc], dtype=np.int64), self._zone)[0])
        count = len(columns["time_msc"])
        meta = {
            "next_cursor_msc": cursor,
            "exhausted": cursor is None,
            "first_msc": int(columns["time_msc"][0]) if count else None,
            "last_msc": int(columns["time_msc"][-1]) if count else None,
            "row_count": count,
        }
        return columns, meta

    # -- trades -------------------------------------------------------------
    def trades(self, symbol: str, *, start_utc_ms: int, end_utc_ms: int, visible_us: int) -> dict:
        """Eligible trades in ``[start, end)`` UTC visible at the watermark, with range metadata."""
        series = self._series_for(symbol)
        spec = self._spec_for(symbol)
        visible_ms = visible_us // 1000
        upper = min(end_utc_ms, visible_ms + 1)
        start = int(np.searchsorted(series.time_msc, start_utc_ms, side="left"))
        stop = int(np.searchsorted(series.time_msc, upper, side="left"))
        rows = self._slice(series, start, stop)
        eligible = (
            ((rows["flags"] & TRADE_FLAGS) != 0)
            & np.isfinite(rows["last"])
            & (rows["last"] > 0)
            & np.isfinite(rows["volume"])
            & (rows["volume"] > 0)
        )
        times = rows["time_msc"][eligible]
        occurrence = np.zeros(len(times), dtype=np.int64)
        if len(times):
            group_start = np.r_[True, times[1:] != times[:-1]]
            index = np.arange(len(times))
            first_index = np.maximum.accumulate(np.where(group_start, index, 0))
            occurrence = (index - first_index).astype(np.int64)
        complete = end_utc_ms <= visible_ms + 1
        return {
            "columns": {
                "time_msc": times.astype(np.int64),
                "price": rows["last"][eligible],
                "volume": rows["volume"][eligible],
                "volume_real": rows["volume"][eligible],  # MT5 real volume is not carried by replay feeds
                "raw_flags": rows["flags"][eligible].astype(np.int32),
                "occurrence": occurrence,
            },
            "metadata": {
                "provider_id": "replay",
                "symbol": symbol,
                "source_generation": self._generation,
                "volume_field": "volume",
                "volume_unit": "contracts",
                "availability": "available",
                "range_complete": bool(complete),
                "covered_from_utc": _iso_utc(start_utc_ms) if complete else None,
                "covered_to_utc": _iso_utc(min(end_utc_ms, visible_ms + 1)),
                "coverage_reason": None if complete else "range_not_yet_visible",
                "invalid_trade_count": 0,
                "truncated": False,
            },
        }

    def coverage_bounds(self, symbol: str) -> tuple[int, int] | None:
        """First and last tick instants of a symbol (UTC ms), for coverage checks."""
        series = self._series_for(symbol)
        if len(series.time_msc) == 0:
            return None
        return int(series.time_msc[0]), int(series.time_msc[-1])

    @staticmethod
    def _slice(series: TickSeries, start: int, stop: int) -> dict[str, np.ndarray]:
        return {
            "time_msc": series.time_msc[start:stop].copy(),
            "bid": series.bid[start:stop].copy(),
            "ask": series.ask[start:stop].copy(),
            "last": series.last[start:stop].copy(),
            "volume": series.volume[start:stop].copy(),
            "flags": series.flags[start:stop].copy(),
        }


def _iso_utc(utc_ms: int) -> str:
    return datetime.fromtimestamp(utc_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_market(
    manifest,
    root,
    *,
    symbols,
    generation: str,
    zone: ZoneInfo,
) -> ReplayMarket:
    """Build the immutable market from a verified manifest, one tick file per symbol.

    Verification (checksums, coverage) is the caller's responsibility and runs first.
    """
    from pathlib import Path

    from q_backend.replay.dataset import load_tick_file

    base = Path(root)
    series: dict[str, TickSeries] = {}
    specs: dict[str, SymbolSpec] = {}
    for symbol in symbols:
        item = manifest.file_for(symbol)
        columns = load_tick_file(base / item.path, symbol)
        series[symbol] = TickSeries(symbol=symbol, **columns)
        specs[symbol] = manifest.symbol_specs[symbol]
    return ReplayMarket(generation=generation, series=series, specs=specs, zone=zone)
