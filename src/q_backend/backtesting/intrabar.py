"""Lazy, validated trade-price replay for service-backed candle backtests."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc, _to_naive_local
from q_backend.market_data.clients.shared import COPY_TICKS_ALL
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.backtesting.candle_kernel import _exit_params_from_strategy

# MT5 answers an empty array for history it has not synchronized yet and serves the
# same request moments later, so an empty candle is re-read before it is reported.
_EMPTY_READ_ATTEMPTS = 3
_EMPTY_READ_DELAY_SECONDS = 1.0

ReadContext = Callable[[Literal["reading_m1", "reading_ticks"], datetime, datetime], AbstractContextManager]

# MT5 tick flags that mark a deal. Quote-only updates repeat the previous ``last``, so a
# candle's first "price" would be the prior candle's close instead of its open.
_TICK_FLAG_LAST = 8
_TICK_FLAG_VOLUME = 16
_TICK_FLAG_BUY = 32
_TICK_FLAG_SELL = 64
_TRADE_FLAGS = _TICK_FLAG_LAST | _TICK_FLAG_VOLUME | _TICK_FLAG_BUY | _TICK_FLAG_SELL

_M1 = "M1"
_MINUTE_MS = 60_000
# A deal stamped up to a second after its minute's end can still belong to that minute.
_BOUNDARY_MARGIN_MS = 1_000
# Deals of a price-adjusted series count as a minute's own when the bar is their shifted or
# scaled copy to within this many ticks: the adjustment rounds each price separately.
_RESCALE_TOLERANCE_TICKS = 2.0
# A substituted minute stamps its four prices at these offsets from the minute's start.
_MINUTE_OFFSETS_MS = np.array([0, 15_000, 30_000, 45_000], dtype=np.int64)
# A substituted candle spreads its four prices evenly over its duration.
_CANDLE_FRACTIONS = np.array([0.0, 0.25, 0.5, 0.75])

RESOLUTION_MINUTE = "minute"
RESOLUTION_CANDLE = "candle"


@dataclass
class ReplayReport:
    """Candles replayed by one run and the prices that had to be substituted.

    ``breakeven_market_inputs`` gives the same report to every replay a run creates, so the
    counts describe the whole run. ``resolutions`` maps each substituted timestamp, in
    microseconds of wall-clock time as the kernel reports exit ticks, to its resolution.
    ``rescaled`` holds the candles whose deals were moved onto an adjusted series' prices.
    """

    replayed: set[pd.Timestamp] = field(default_factory=set)
    substituted: set[pd.Timestamp] = field(default_factory=set)
    rescaled: set[pd.Timestamp] = field(default_factory=set)
    resolutions: dict[int, str] = field(default_factory=dict)

    def record(self, candle: pd.Timestamp, times_ms: np.ndarray, resolution: str) -> None:
        self.substituted.add(candle)
        for time_ms in times_ms.tolist():
            self.resolutions[int(time_ms) * 1000] = resolution

    @property
    def session_dates(self) -> list[str]:
        return sorted({candle.date().isoformat() for candle in self.substituted})


def _four_prices(ohlc: np.ndarray) -> np.ndarray:
    """Open, then low and high in the order a minute with this close traded them, then close."""
    open_, high, low, close = ohlc
    if close >= open_:
        return np.array([open_, low, high, close])
    return np.array([open_, high, low, close])


class MarketTickReplay:
    """Assemble each candle's trade path from deals, substituting one-minute bars where needed.

    A minute whose deals reproduce its M1 bar (volume, open, high, low and close) contributes
    those deals. On a price-adjusted series the terminal adjusts the bars and serves the deals
    unadjusted, so a minute whose deals carry the bar's volume and trace its shape contributes
    them rescaled onto the bar. Any other minute contributes four prices from its M1 bar, and a
    candle without usable M1 bars contributes its own four prices. Incomplete or inconsistent
    history is recorded in ``report`` instead of failing; provider errors still propagate.
    """

    def __init__(
        self,
        service: Any,
        symbol: str,
        timeframe: str,
        frame: pd.DataFrame,
        tick_size: float | None = None,
        report: ReplayReport | None = None,
        price_digits: int | None = None,
        on_replay: Callable[[int, int, pd.Timestamp], None] | None = None,
        on_replay_completed: Callable[[int, int, pd.Timestamp], None] | None = None,
        read_context: ReadContext | None = None,
    ) -> None:
        self._service = service
        self._on_replay = on_replay
        self._on_replay_completed = on_replay_completed
        self._read_context = read_context
        self._tick_size = tick_size if tick_size is not None and np.isfinite(tick_size) and tick_size > 0 else None
        self._tolerance = 0.0 if self._tick_size is None else self._tick_size * 1e-6
        self._price_digits = price_digits
        self._symbol = symbol
        self._duration = bar_duration(timeframe)
        self._frame = frame
        self._report = report if report is not None else ReplayReport()
        self._last: tuple[int, np.ndarray, np.ndarray] | None = None

    def __call__(self, bar: int) -> tuple[np.ndarray, np.ndarray]:
        if self._last is not None and self._last[0] == bar:
            return self._last[1], self._last[2]
        start = _to_naive_local(self._frame.index[bar].to_pydatetime())
        end = start + self._duration.to_pytimedelta()
        if bar + 1 < len(self._frame):
            end = min(end, _to_naive_local(self._frame.index[bar + 1].to_pydatetime()))
        candle = np.array([float(self._frame.iloc[bar][col]) for col in ("open", "high", "low", "close")])
        self._report.replayed.add(pd.Timestamp(start))
        if self._on_replay is not None:
            # Before the candle's history is read, so a slow read is visible to the observer.
            self._on_replay(bar, len(self._frame), pd.Timestamp(start))
        times, prices = self._replay(start, end, candle)
        if self._on_replay_completed is not None:
            self._on_replay_completed(bar, len(self._frame), pd.Timestamp(start))
        self._last = (bar, np.ascontiguousarray(times * 1000), np.ascontiguousarray(prices))
        return self._last[1], self._last[2]

    def _replay(self, start: datetime, end: datetime, candle: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        start_ms = _naive_local_to_time_msc(start)
        end_ms = _naive_local_to_time_msc(end)
        # One minute past the candle is read so the last minute can see whether its successor exists.
        bar_times, bar_ohlc, bar_volume = self._m1_bars(start, _from_ms(end_ms + _MINUTE_MS))
        inside = np.flatnonzero((bar_times >= start_ms) & (bar_times < end_ms))
        if inside.size == 0 or not self._aggregates(bar_ohlc[inside], candle):
            return self._substitute_candle(start, start_ms, end_ms - start_ms, candle)

        day_end_ms = _naive_local_to_time_msc(_next_midnight(start))
        windows = [self._window_end_ms(start, bar_times, k, day_end_ms) for k in inside]
        deal_times, deal_prices, deal_volumes = self._read_deals(start, _from_ms(max(windows)))

        segments: list[tuple[np.ndarray, np.ndarray]] = []
        pointer = 0
        for position, k in enumerate(inside):
            minute_start = int(bar_times[k])
            minute_ohlc = bar_ohlc[k]
            window_stop = int(np.searchsorted(deal_times, windows[position], side="left"))
            run = self._match_minute(pointer, window_stop, deal_prices, deal_volumes, minute_ohlc, bar_volume[k])
            if run is not None:
                first, stop = run
                segments.append((deal_times[first:stop], deal_prices[first:stop]))
                pointer = stop
                continue
            # A deal is never stamped before its own minute, so an adjusted run starts inside it.
            in_minute = max(pointer, int(np.searchsorted(deal_times, minute_start, side="left")))
            rescaled = self._rescale_minute(
                in_minute, window_stop, deal_prices, deal_volumes, minute_ohlc, bar_volume[k]
            )
            if rescaled is not None:
                first, stop, minute_prices = rescaled
                self._report.rescaled.add(pd.Timestamp(start))
                segments.append((deal_times[first:stop], minute_prices))
                pointer = stop
                continue
            pointer = max(pointer, int(np.searchsorted(deal_times, minute_start + _MINUTE_MS, side="left")))
            minute_times = minute_start + _MINUTE_OFFSETS_MS
            self._report.record(pd.Timestamp(start), minute_times, RESOLUTION_MINUTE)
            segments.append((minute_times, _four_prices(minute_ohlc)))
        times = np.concatenate([segment[0] for segment in segments]).astype(np.int64)
        prices = np.concatenate([segment[1] for segment in segments]).astype(np.float64)
        return times, prices

    def _match_minute(
        self,
        lower: int,
        upper: int,
        prices: np.ndarray,
        volumes: np.ndarray,
        minute_ohlc: np.ndarray,
        minute_volume: float,
    ) -> tuple[int, int] | None:
        """First contiguous run of deals in ``[lower, upper)`` that reproduces the minute's bar."""
        if upper <= lower or not np.isfinite(minute_volume) or minute_volume <= 0:
            return None
        open_, high, low, close = minute_ohlc
        cumulative = np.cumsum(volumes[lower:upper])
        for first in np.flatnonzero(np.abs(prices[lower:upper] - open_) <= self._tolerance):
            before = cumulative[first - 1] if first > 0 else 0.0
            totals = cumulative[first:] - before
            for offset in np.flatnonzero(np.abs(totals - minute_volume) <= 1e-6):
                last = first + offset
                if abs(prices[lower + last] - close) > self._tolerance:
                    continue
                run = prices[lower + first : lower + last + 1]
                if abs(run.max() - high) <= self._tolerance and abs(run.min() - low) <= self._tolerance:
                    return lower + first, lower + last + 1
        return None

    def _rescale_minute(
        self,
        lower: int,
        upper: int,
        prices: np.ndarray,
        volumes: np.ndarray,
        minute_ohlc: np.ndarray,
        minute_volume: float,
    ) -> tuple[int, int, np.ndarray] | None:
        """First run of deals in ``[lower, upper)`` with the bar's volume that the bar is an adjusted copy of.

        Returns the run and its prices on the bar's scale.
        """
        if self._tick_size is None or upper <= lower or not np.isfinite(minute_volume) or minute_volume <= 0:
            return None
        cumulative = np.cumsum(volumes[lower:upper])
        if not np.isfinite(cumulative[-1]):
            return None
        targets = np.concatenate(([0.0], cumulative[:-1])) + minute_volume
        lasts = np.searchsorted(cumulative, targets - 1e-6, side="left")
        reachable = np.flatnonzero(lasts < cumulative.size)
        for first in reachable[np.abs(cumulative[lasts[reachable]] - targets[reachable]) <= 1e-6]:
            run = prices[lower + first : lower + lasts[first] + 1]
            adjusted = self._onto_bar(run, minute_ohlc)
            if adjusted is not None:
                return lower + int(first), lower + int(lasts[first]) + 1, adjusted
        return None

    def _onto_bar(self, run: np.ndarray, minute_ohlc: np.ndarray) -> np.ndarray | None:
        """``run`` moved onto its bar's prices, or ``None`` when the bar is not its adjusted copy.

        The bar fixes where the run's low, open, close and high land; the deals between them are
        interpolated. That holds for an adjustment by difference and for a proportional one, whose
        per-price rounding leaves no single offset or factor that reproduces the bar exactly.
        """
        open_, high, low, close = minute_ohlc
        raw = np.array([run.min(), run[0], run[-1], run.max()])
        bar = np.array([low, open_, close, high])
        if raw[0] <= 0 or not np.all(np.isfinite(bar)):
            return None
        shifted = np.abs(bar - (raw + (open_ - run[0]))).max()
        scaled = np.abs(bar - raw * (open_ / run[0])).max()
        if min(shifted, scaled) > _RESCALE_TOLERANCE_TICKS * self._tick_size:
            return None
        order = np.argsort(raw, kind="stable")
        raw, bar = raw[order], bar[order]
        rise = np.diff(bar)
        repeated = np.diff(raw) <= self._tolerance
        # The bar must order the four prices as the deals do, and give one deal price one bar price.
        if np.any(rise < -self._tolerance) or np.any(np.abs(rise[repeated]) > self._tolerance):
            return None
        distinct = np.concatenate(([True], ~repeated))
        adjusted = np.interp(run, raw[distinct], bar[distinct])
        return adjusted if self._price_digits is None else np.round(adjusted, self._price_digits)

    def _aggregates(self, minute_ohlc: np.ndarray, candle: np.ndarray) -> bool:
        observed = np.array([minute_ohlc[0, 0], minute_ohlc[:, 1].max(), minute_ohlc[:, 2].min(), minute_ohlc[-1, 3]])
        return bool(np.all(np.abs(observed - candle) <= self._tolerance))

    def _substitute_candle(
        self, start: datetime, start_ms: int, duration_ms: int, candle: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        times = start_ms + (_CANDLE_FRACTIONS * duration_ms).astype(np.int64)
        self._report.record(pd.Timestamp(start), times, RESOLUTION_CANDLE)
        return times, _four_prices(candle)

    def _window_end_ms(self, start: datetime, bar_times: np.ndarray, k: int, day_end_ms: int) -> int:
        """Deals a minute may claim: to one second past its end, or up to the next bar when it has no successor."""
        minute_end = int(bar_times[k]) + _MINUTE_MS
        later = bar_times[k + 1 :]
        if later.size and later[0] == minute_end:
            return minute_end + _BOUNDARY_MARGIN_MS
        if later.size:
            return int(later[0])
        return self._first_bar_after_ms(start, minute_end, day_end_ms)

    def _first_bar_after_ms(self, start: datetime, after_ms: int, day_end_ms: int) -> int:
        times, _, _ = self._m1_bars(_from_ms(after_ms), _from_ms(day_end_ms))
        times = times[times > after_ms]
        return int(times[0]) if times.size else day_end_ms

    def _m1_bars(self, start: datetime, end: datetime) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        context = self._read_context("reading_m1", start, end) if self._read_context else nullcontext()
        with context:
            rows = self._service.get_ohlcv(self._symbol, _M1, start, end)
        if not rows:
            return np.empty(0, np.int64), np.empty((0, 4)), np.empty(0)
        times = np.array([_naive_local_to_time_msc(row.time) for row in rows], dtype=np.int64)
        ohlc = np.array([[row.open, row.high, row.low, row.close] for row in rows], dtype=np.float64)
        volume = np.array(
            [np.nan if row.real_volume is None else float(row.real_volume) for row in rows], dtype=np.float64
        )
        order = np.argsort(times, kind="stable")
        return times[order], ohlc[order], volume[order]

    def _read_deals(self, start: datetime, end: datetime) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        deals = self._read_trades(start, end)
        for _ in range(_EMPTY_READ_ATTEMPTS - 1):
            if deals[1].size:
                break
            time.sleep(_EMPTY_READ_DELAY_SECONDS)
            deals = self._read_trades(start, end)
        return deals

    def _read_trades(self, start: Any, end: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        context = self._read_context("reading_ticks", start, end) if self._read_context else nullcontext()
        with context:
            arrays = self._service.get_ticks_columnar(self._symbol, start, end, flags=COPY_TICKS_ALL)
        times = np.asarray(arrays["time_msc"], dtype=np.int64)
        prices = np.asarray(arrays["last"], dtype=np.float64)
        if times.ndim != 1 or prices.ndim != 1 or times.size != prices.size:
            raise ValueError(f"{self._symbol}: tick times and prices must be equal-length vectors")
        if "volume" in arrays:
            volumes = np.asarray(arrays["volume"], dtype=np.float64)
        else:
            volumes = np.full(times.size, np.nan)
        mask = (
            (times >= _naive_local_to_time_msc(start))
            & (times < _naive_local_to_time_msc(end))
            & np.isfinite(prices)
            & (prices > 0)
        )
        if "flags" in arrays:
            flags = np.asarray(arrays["flags"], dtype=np.int64)
            if flags.shape == times.shape:
                mask &= (flags & _TRADE_FLAGS) != 0
        times, prices, volumes = times[mask], prices[mask], volumes[mask]
        order = np.argsort(times, kind="stable")
        times, prices, volumes = times[order], prices[order], volumes[order]
        if self._tick_size is not None:
            # Terminal ticks are rounded to the symbol's decimals (4513.25 arrives as
            # 4513.3); the instrument trades on its tick grid, so snap back onto it.
            prices = np.round(prices / self._tick_size) * self._tick_size
        return times, prices, volumes


def _from_ms(value_ms: int) -> datetime:
    return pd.Timestamp(int(value_ms), unit="ms").to_pydatetime()


def _next_midnight(value: datetime) -> datetime:
    return (pd.Timestamp(value).normalize() + pd.Timedelta(days=1)).to_pydatetime()


def classify_exit_resolutions(trades: list[Any], report: ReplayReport) -> None:
    """Set ``exit_resolution`` on each closed trade from the tick that decided its exit."""
    for trade in trades:
        if trade.exit_tick_time is None:
            trade.exit_resolution = None
            continue
        wall_us = int(pd.Timestamp(trade.exit_tick_time).tz_localize(None).value // 1000)
        trade.exit_resolution = report.resolutions.get(wall_us, "tick")


def intrabar_notes(trades: list[Any], report: ReplayReport) -> list[dict[str, Any]]:
    """The run-level note for substituted prices; empty when every replayed candle used real ticks."""
    if not report.substituted:
        return []
    ticks = sum(trade.exit_resolution == "tick" for trade in trades)
    minute = sum(trade.exit_resolution == RESOLUTION_MINUTE for trade in trades)
    candle = sum(trade.exit_resolution == RESOLUTION_CANDLE for trade in trades)
    bars = sum(trade.exit_resolution is None for trade in trades)
    substituted_net = float(
        sum(trade.pnl or 0.0 for trade in trades if trade.exit_resolution in (RESOLUTION_MINUTE, RESOLUTION_CANDLE))
    )
    parts = [f"{ticks} exited on real ticks"]
    if bars:
        parts.append(f"{bars} used normal candle-based exits")
    message = f"{len(trades)} trades: {', '.join(parts)}. "
    substituted_parts = []
    if minute:
        substituted_parts.append(f"{minute} at one-minute resolution")
    if candle:
        substituted_parts.append(f"{candle} at candle resolution")
    if substituted_parts:
        message += (
            f"Incomplete or inconsistent tick history required substituted exits: "
            f"{', '.join(substituted_parts)} (net R$ {substituted_net:,.2f} on the substituted exits)."
        )
    else:
        message += (
            "Incomplete or inconsistent tick history required substituted prices during replay, "
            "but no trades exited on those prices."
        )
    return [
        {
            "code": "intrabar_incomplete_ticks",
            "message": message,
            "details": {
                "trades": len(trades),
                "tick_exits": ticks,
                "minute_exits": minute,
                "candle_exits": candle,
                "bar_exits": bars,
                "substituted_net_pnl": substituted_net,
                "candles_replayed": len(report.replayed),
                "candles_substituted": len(report.substituted),
                "candles_rescaled": len(report.rescaled),
                "session_dates": report.session_dates,
            },
        }
    ]


def breakeven_market_inputs(
    strategy: Any,
    service: Any,
    timeframe: str,
    report: ReplayReport | None = None,
    on_replay: Callable[[int, int, pd.Timestamp], None] | None = None,
    on_replay_completed: Callable[[int, int, pd.Timestamp], None] | None = None,
    read_context: ReadContext | None = None,
) -> dict[str, Any]:
    """Resolve instrument metadata only for an enabled breakeven rule.

    ``report`` collects the substitutions of every replay the returned factory creates.
    ``on_replay`` is told each candle before its history is read. ``on_replay_completed``
    observes successful completion; ``read_context`` brackets M1/tick reads, including retries.
    """
    params = _exit_params_from_strategy(strategy)
    if float(params.get("breakeven_trigger_pct", 0.0)) <= 0.0:
        return {}
    if service is None:
        from q_backend.market_data.service import MarketDataService

        service = MarketDataService()
    lookup = getattr(service, "get_symbol_info", None)
    grid = None
    digits = None
    if lookup is not None:
        info = lookup(strategy.symbol) or {}
        value = float(info.get("trade_tick_size") or 0.0)
        grid = value if np.isfinite(value) and value > 0 else None
        digits = int(info["digits"]) if info.get("digits") is not None else None
    tick_size = None
    if float(params["breakeven_emergency_stop_ticks"]) > 0:
        if grid is None:
            raise ValueError(
                f"{strategy.symbol}: Breakeven Emergency Stop requires a finite positive instrument trade_tick_size"
            )
        tick_size = grid
    return {
        "tick_size": tick_size,
        "intrabar_factory": lambda frame: MarketTickReplay(
            service,
            strategy.symbol,
            timeframe,
            frame,
            grid,
            report=report,
            price_digits=digits,
            on_replay=on_replay,
            on_replay_completed=on_replay_completed,
            read_context=read_context,
        ),
    }
