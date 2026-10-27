"""Post-simulation trade path and post-exit excursion measurements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Iterable, Protocol

import numpy as np
import pandas as pd

from q_backend.backtesting.models import OrderAction, Trade


@dataclass(frozen=True)
class DealBar:
    times: Iterable[datetime]
    prices: Iterable[float]
    resolution: str = "tick"


class DealSource(Protocol):
    def __call__(self, bar_index: int) -> DealBar | None: ...


def _timestamp(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    return result.tz_localize("UTC") if result.tzinfo is None else result


def _indices(trade: Trade, bars: pd.DataFrame) -> tuple[int, int] | None:
    index = pd.DatetimeIndex(bars.index)
    entry = trade.entry_bar
    exit_ = trade.exit_bar
    if entry is None:
        entry = int(index.get_indexer([_timestamp(trade.entry_tick_time or trade.entry_time)], method="pad")[0])
    if exit_ is None and trade.exit_time is not None:
        exit_ = int(index.get_indexer([_timestamp(trade.exit_tick_time or trade.exit_time)], method="pad")[0])
    if entry is None or exit_ is None or entry < 0 or exit_ < 0:
        return None
    return entry, exit_


def _direction(trade: Trade) -> float:
    return 1.0 if trade.action == OrderAction.BUY else -1.0


def _time_array(values: Iterable[datetime]) -> tuple[np.ndarray, object]:
    index = pd.DatetimeIndex(values)
    source_timezone = index.tz if index.tz is not None else "UTC"
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    return index.as_unit("ns").asi8, source_timezone


def _times_ns(values: Iterable[datetime]) -> np.ndarray:
    return _time_array(values)[0]


def _bar_positions(trade: Trade, index: pd.DatetimeIndex) -> tuple[int, int] | None:
    entry = trade.entry_bar
    exit_ = trade.exit_bar
    if entry is None:
        entry = int(index.get_indexer([_timestamp(trade.entry_tick_time or trade.entry_time)], method="pad")[0])
    if exit_ is None and trade.exit_time is not None:
        exit_ = int(index.get_indexer([_timestamp(trade.exit_tick_time or trade.exit_time)], method="pad")[0])
    if entry is None or exit_ is None or entry < 0 or exit_ < 0:
        return None
    return entry, exit_


def post_exit_from_stored(
    trades: Iterable[Trade], bars: pd.DataFrame, *, post_exit_bars: int, day_trade_close: str | time | None = None
) -> list[dict]:
    """Recalculate the post-exit window from saved exit-bar remainder and whole bars."""
    if not 1 <= post_exit_bars <= 500:
        raise ValueError("post_exit_bars must be between 1 and 500")
    output = []
    index = pd.DatetimeIndex(bars.index)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    close_time = time.fromisoformat(day_trade_close) if isinstance(day_trade_close, str) else day_trade_close
    times_of_day = index.time
    for trade in trades:
        empty = {
            "trade_id": trade.id,
            "post_exit_mfe_points": None,
            "post_exit_mae_points": None,
            "post_exit_return_points": None,
            "post_exit_bars_observed": 0,
        }
        positions = _bar_positions(trade, index)
        if (
            positions is None
            or trade.exit_price is None
            or trade.exit_bar_rest_high is None
            or trade.exit_bar_rest_low is None
        ):
            output.append(empty)
            continue
        _, exit_bar = positions
        direction = _direction(trade)
        best = max(
            0.0,
            (trade.exit_bar_rest_high - trade.exit_price) * direction,
            (trade.exit_bar_rest_low - trade.exit_price) * direction,
        )
        worst = max(
            0.0,
            -(trade.exit_bar_rest_high - trade.exit_price) * direction,
            -(trade.exit_bar_rest_low - trade.exit_price) * direction,
        )
        last_close = close[exit_bar]
        observed = 0
        stop = min(exit_bar + post_exit_bars + 1, len(bars))
        end = stop
        if close_time is not None:
            for i in range(exit_bar + 1, stop):
                if times_of_day[i].replace(tzinfo=None) > close_time:
                    end = i
                    break
        if end > exit_bar + 1:
            future_high = (high[exit_bar + 1 : end] - trade.exit_price) * direction
            future_low = (low[exit_bar + 1 : end] - trade.exit_price) * direction
            best = max(best, float(future_high.max()), float(future_low.max()), 0.0)
            worst = max(worst, float(-future_high.min()), float(-future_low.min()), 0.0)
            last_close = close[end - 1]
            observed = end - exit_bar - 1
        if (
            exit_bar + 1 >= len(bars)
            and trade.exit_bar_rest_high == trade.exit_price
            and trade.exit_bar_rest_low == trade.exit_price
        ):
            output.append(empty)
            continue
        output.append(
            {
                "trade_id": trade.id,
                "post_exit_mfe_points": best,
                "post_exit_mae_points": worst,
                "post_exit_return_points": (last_close - trade.exit_price) * direction,
                "post_exit_bars_observed": observed,
            }
        )
    return output


def measure_trades(
    trades: Iterable[Trade],
    bars: pd.DataFrame,
    *,
    deals: DealSource | None = None,
    post_exit_bars: int = 10,
    day_trade_close: str | time | None = None,
    on_trade: Callable[[int, int], None] | None = None,
) -> None:
    """Measure closed trade excursions in place, using deal data only where needed."""
    trades = list(trades)
    cache: dict[int, tuple[np.ndarray, np.ndarray, str, object] | None] = {}
    index = pd.DatetimeIndex(bars.index)
    bar_times = _times_ns(index)
    highs = bars["high"].to_numpy(dtype=np.float64)
    lows = bars["low"].to_numpy(dtype=np.float64)

    def get_deals(i: int) -> tuple[np.ndarray, np.ndarray, str, object] | None:
        if i not in cache:
            result = deals(i) if deals is not None else None
            if result is None:
                cache[i] = None
            else:
                times_ns, source_timezone = _time_array(result.times)
                cache[i] = (
                    times_ns,
                    np.asarray(result.prices, dtype=np.float64),
                    result.resolution,
                    source_timezone,
                )
        return cache[i]

    for trade_number, trade in enumerate(trades, start=1):
        positions = _bar_positions(trade, index)
        if positions is None or trade.exit_price is None:
            continue
        entry_bar, exit_bar = positions
        direction = _direction(trade)
        entry_time = _timestamp(trade.entry_tick_time or trade.entry_time)
        exit_time = _timestamp(trade.exit_tick_time or trade.exit_time)
        candidate_prices = [np.asarray([trade.entry_price, trade.exit_price], dtype=np.float64)]
        candidate_times = [np.asarray([entry_time.value, exit_time.value], dtype=np.int64)]
        candidate_resolutions = [np.asarray(["tick", "tick"], dtype=object)]
        candidate_timezones = [np.asarray([entry_time.tz, exit_time.tz], dtype=object)]
        resolution = "tick"
        partial_bars = set()
        if not (trade.entry_tick_time is None and trade.order_kind == "market"):
            partial_bars.add(entry_bar)
        if trade.exit_tick_time is None:
            partial_bars.discard(exit_bar)
        else:
            partial_bars.add(exit_bar)
        for i in range(entry_bar, exit_bar + 1):
            if i not in partial_bars:
                candidate_prices.append(np.asarray([highs[i], lows[i]], dtype=np.float64))
                candidate_times.append(np.asarray([bar_times[i], bar_times[i]], dtype=np.int64))
                candidate_resolutions.append(np.asarray(["tick", "tick"], dtype=object))
                candidate_timezones.append(np.asarray([index.tz or "UTC", index.tz or "UTC"], dtype=object))
                continue
            deals_in_bar = get_deals(i)
            if deals_in_bar is None:
                resolution = "candle"
                candidate_prices.append(np.asarray([highs[i], lows[i]], dtype=np.float64))
                candidate_times.append(np.asarray([bar_times[i], bar_times[i]], dtype=np.int64))
                candidate_resolutions.append(np.asarray(["candle", "candle"], dtype=object))
                candidate_timezones.append(np.asarray([index.tz or "UTC", index.tz or "UTC"], dtype=object))
                continue
            deal_times, deal_prices, deal_resolution, deal_timezone = deals_in_bar
            if deal_resolution == "minute":
                resolution = "minute"
            count = min(len(deal_times), len(deal_prices))
            deal_times, deal_prices = deal_times[:count], deal_prices[:count]
            mask = np.ones(count, dtype=bool)
            if i == entry_bar:
                mask &= deal_times >= entry_time.value
            if i == exit_bar:
                mask &= deal_times <= exit_time.value
            candidate_prices.append(deal_prices[mask])
            candidate_times.append(deal_times[mask])
            candidate_resolutions.append(np.full(int(mask.sum()), deal_resolution, dtype=object))
            candidate_timezones.append(np.full(int(mask.sum()), deal_timezone, dtype=object))
        prices = np.concatenate(candidate_prices)
        times = np.concatenate(candidate_times)
        resolutions = np.concatenate(candidate_resolutions)
        timezones = np.concatenate(candidate_timezones)
        values = (prices - trade.entry_price) * direction
        max_i, min_i = int(np.argmax(values)), int(np.argmin(values))
        max_value, min_value = values[max_i], values[min_i]
        trade.mfe_points = max(0.0, max_value)
        trade.mae_points = max(0.0, -min_value)
        trade.mfe_time = (
            pd.Timestamp(times[max_i], tz="UTC").tz_convert(timezones[max_i]).to_pydatetime()
            if max_value > 0
            else entry_time.to_pydatetime()
        )
        trade.mae_time = (
            pd.Timestamp(times[min_i], tz="UTC").tz_convert(timezones[min_i]).to_pydatetime()
            if min_value < 0
            else entry_time.to_pydatetime()
        )
        realised_money = (trade.exit_price - trade.entry_price) * direction * trade.quantity * trade.point_value
        mfe_money = trade.mfe_points * trade.quantity * trade.point_value
        trade.mfe_capture = realised_money / mfe_money if mfe_money else None
        trade.excursion_resolution = max(
            (resolution, resolutions[max_i], resolutions[min_i]), key={"tick": 0, "minute": 1, "candle": 2}.get
        )
        exit_deals = get_deals(exit_bar) if trade.exit_tick_time is not None else None
        if trade.exit_tick_time is None:
            trade.exit_bar_rest_high = float(bars.iloc[exit_bar].high)
            trade.exit_bar_rest_low = float(bars.iloc[exit_bar].low)
        elif exit_deals is not None:
            exit_times, exit_prices, _, _ = exit_deals
            count = min(len(exit_times), len(exit_prices))
            post_prices = exit_prices[:count][exit_times[:count] >= exit_time.value].tolist()
            post_prices.append(trade.exit_price)
            trade.exit_bar_rest_high, trade.exit_bar_rest_low = max(post_prices), min(post_prices)
        else:
            trade.exit_bar_rest_high = trade.exit_price
            trade.exit_bar_rest_low = trade.exit_price
        if on_trade is not None:
            on_trade(trade_number, len(trades))
    measured = list(trades)
    for trade, values in zip(
        measured, post_exit_from_stored(measured, bars, post_exit_bars=post_exit_bars, day_trade_close=day_trade_close)
    ):
        trade.post_exit_mfe_points = values["post_exit_mfe_points"]
        trade.post_exit_mae_points = values["post_exit_mae_points"]
        trade.post_exit_return_points = values["post_exit_return_points"]
        trade.post_exit_bars_observed = values["post_exit_bars_observed"]
