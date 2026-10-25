"""Post-simulation trade path and post-exit excursion measurements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Iterable, Protocol

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


def post_exit_from_stored(
    trades: Iterable[Trade], bars: pd.DataFrame, *, post_exit_bars: int, day_trade_close: str | time | None = None
) -> list[dict]:
    """Recalculate the post-exit window from saved exit-bar remainder and whole bars."""
    if not 1 <= post_exit_bars <= 500:
        raise ValueError("post_exit_bars must be between 1 and 500")
    output = []
    for trade in trades:
        empty = {
            "trade_id": trade.id,
            "post_exit_mfe_points": None,
            "post_exit_mae_points": None,
            "post_exit_return_points": None,
            "post_exit_bars_observed": 0,
        }
        positions = _indices(trade, bars)
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
        last_close = float(bars.iloc[exit_bar]["close"])
        observed = 0
        for i in range(exit_bar + 1, min(exit_bar + post_exit_bars + 1, len(bars))):
            row = bars.iloc[i]
            if day_trade_close is not None:
                close_time = (
                    time.fromisoformat(day_trade_close) if isinstance(day_trade_close, str) else day_trade_close
                )
                bar_time = _timestamp(bars.index[i]).to_pydatetime().timetz()
                if bar_time.replace(tzinfo=None) > close_time:
                    break
            best = max(
                best,
                (float(row.high) - trade.exit_price) * direction,
                (float(row.low) - trade.exit_price) * direction,
                0.0,
            )
            worst = max(
                worst,
                -(float(row.high) - trade.exit_price) * direction,
                -(float(row.low) - trade.exit_price) * direction,
                0.0,
            )
            last_close = float(row.close)
            observed += 1
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
    cache: dict[int, DealBar | None] = {}

    def get_deals(i: int) -> DealBar | None:
        if i not in cache:
            cache[i] = deals(i) if deals is not None else None
        return cache[i]

    for trade_number, trade in enumerate(trades, start=1):
        positions = _indices(trade, bars)
        if positions is None or trade.exit_price is None:
            continue
        entry_bar, exit_bar = positions
        direction = _direction(trade)
        entry_time = _timestamp(trade.entry_tick_time or trade.entry_time)
        exit_time = _timestamp(trade.exit_tick_time or trade.exit_time)
        candidates: list[tuple[float, pd.Timestamp, str]] = [
            (trade.entry_price, entry_time, "tick"),
            (trade.exit_price, exit_time, "tick"),
        ]
        resolution = "tick"
        partial_bars = set()
        if trade.entry_tick_time is None and trade.order_kind == "market":
            # Entry at bar open includes the full candle.
            partial_bars.discard(entry_bar)
        else:
            partial_bars.add(entry_bar)
        if trade.exit_tick_time is None:
            partial_bars.discard(exit_bar)  # open fill includes only the opening deal
        else:
            partial_bars.add(exit_bar)
        for i in range(entry_bar, exit_bar + 1):
            row = bars.iloc[i]
            if i not in partial_bars:
                candidates.extend(
                    [
                        (float(row.high), _timestamp(bars.index[i]), "tick"),
                        (float(row.low), _timestamp(bars.index[i]), "tick"),
                    ]
                )
                continue
            deals_in_bar = get_deals(i)
            if deals_in_bar is None:
                resolution = "candle"
                candidates.extend(
                    [
                        (float(row.high), _timestamp(bars.index[i]), "candle"),
                        (float(row.low), _timestamp(bars.index[i]), "candle"),
                    ]
                )
                continue
            if deals_in_bar.resolution == "minute":
                resolution = "minute"
            times = [_timestamp(v) for v in deals_in_bar.times]
            prices = list(deals_in_bar.prices)
            for stamp, price in zip(times, prices):
                if i == entry_bar and stamp < entry_time:
                    continue
                if i == exit_bar and stamp > exit_time:
                    continue
                candidates.append((float(price), stamp, deals_in_bar.resolution))
        values = [(price - trade.entry_price) * direction for price, _, _ in candidates]
        max_value, min_value = max(values), min(values)
        max_i = values.index(max_value)
        min_i = values.index(min_value)
        trade.mfe_points = max(0.0, max_value)
        trade.mae_points = max(0.0, -min_value)
        trade.mfe_time = candidates[max_i][1].to_pydatetime() if max_value > 0 else entry_time.to_pydatetime()
        trade.mae_time = candidates[min_i][1].to_pydatetime() if min_value < 0 else entry_time.to_pydatetime()
        realised = (trade.exit_price - trade.entry_price) * direction
        trade.mfe_capture = realised / trade.mfe_points if trade.mfe_points else None
        trade.excursion_resolution = max(
            (resolution, candidates[max_i][2], candidates[min_i][2]), key={"tick": 0, "minute": 1, "candle": 2}.get
        )
        exit_deals = get_deals(exit_bar) if trade.exit_tick_time is not None else None
        if trade.exit_tick_time is None:
            trade.exit_bar_rest_high = float(bars.iloc[exit_bar].high)
            trade.exit_bar_rest_low = float(bars.iloc[exit_bar].low)
        elif exit_deals is not None:
            post_prices = [float(p) for t, p in zip(exit_deals.times, exit_deals.prices) if _timestamp(t) >= exit_time]
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
