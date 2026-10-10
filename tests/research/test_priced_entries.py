"""Priced entry orders in research backtests (Q-106)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
DAY = date(2026, 10, 5)


def _bar(clock: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY.isoformat()} {clock}", tz=BRASILIA_TZ)


def _write_session(root, rows, day=DAY, symbol=SYMBOL):
    times = []
    for clock, _ in rows:
        hour, minute, second = (int(part) for part in clock.split(":"))
        moment = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=timezone.utc)
        times.append(int(moment.timestamp() * 1000))
    count = len(rows)
    prices = np.asarray([price for _, price in rows], dtype=np.float64)
    arrays = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": prices,
        "ask": prices,
        "last": prices,
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.full(count, 32, dtype=np.int32),
    }
    directory = root / _slug_symbol(symbol)
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)
    return TickStore(symbol, root=root)


def _m10_frame(store: TickStore) -> pd.DataFrame:
    return store.bars("M10", start=DAY.isoformat())


class Scripted(ResearchStrategy):
    def __init__(self, orders: dict[int, TradeOrder]) -> None:
        self.orders = orders

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        return self.orders.get(len(frame) - 1)


class PreviousHighBreakout(ResearchStrategy):
    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        if len(frame) >= 2 and frame["high"].iloc[-1] > frame["high"].iloc[-2]:
            return TradeOrder.buy(price=frame["high"].iloc[-2])
        return None


# Criterion 1: Order construction
def test_trade_order_priced_construction_and_validation() -> None:
    order_buy = TradeOrder.buy(price=10.0)
    assert order_buy.price == 10.0
    assert order_buy.action == "buy"

    order_sell = TradeOrder.sell(price=10.0)
    assert order_sell.price == 10.0
    assert order_sell.action == "sell"

    order_close = TradeOrder.close()
    assert order_close.price is None

    # price must be a finite positive number
    for bad in (0.0, -1.0, float("nan"), float("inf"), True, "10"):
        with pytest.raises(ValueError, match="price must be a finite positive price"):
            TradeOrder.buy(price=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="price must be a finite positive price"):
            TradeOrder.sell(price=bad)  # type: ignore[arg-type]

    # Buy level ordering: stop_loss < price < take_profit
    TradeOrder.buy(price=10.0, stop_loss=9.0, take_profit=11.0)
    TradeOrder.buy(price=10.0, stop_loss=9.0)
    TradeOrder.buy(price=10.0, take_profit=11.0)

    with pytest.raises(ValueError, match="stop_loss must be below price"):
        TradeOrder.buy(price=10.0, stop_loss=10.0)
    with pytest.raises(ValueError, match="stop_loss must be below price"):
        TradeOrder.buy(price=10.0, stop_loss=10.5)
    with pytest.raises(ValueError, match="take_profit must be above price"):
        TradeOrder.buy(price=10.0, take_profit=10.0)
    with pytest.raises(ValueError, match="take_profit must be above price"):
        TradeOrder.buy(price=10.0, take_profit=9.5)

    # Sell level ordering: stop_loss > price > take_profit
    TradeOrder.sell(price=10.0, stop_loss=11.0, take_profit=9.0)
    TradeOrder.sell(price=10.0, stop_loss=11.0)
    TradeOrder.sell(price=10.0, take_profit=9.0)

    with pytest.raises(ValueError, match="stop_loss must be above price"):
        TradeOrder.sell(price=10.0, stop_loss=10.0)
    with pytest.raises(ValueError, match="stop_loss must be above price"):
        TradeOrder.sell(price=10.0, stop_loss=9.5)
    with pytest.raises(ValueError, match="take_profit must be below price"):
        TradeOrder.sell(price=10.0, take_profit=10.0)
    with pytest.raises(ValueError, match="take_profit must be below price"):
        TradeOrder.sell(price=10.0, take_profit=10.5)

    # Close rejects price
    with pytest.raises(TypeError):
        TradeOrder.close(price=10.0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="close orders carry no price"):
        TradeOrder("close", price=10.0)


# Criterion 2: Example strategy fills at previous high on deciding bar
def test_previous_high_breakout_fills_on_deciding_bar_at_price(tmp_path) -> None:
    # Bar 0 (10:00): open 99, high 100, low 98, close 99.5
    # Bar 1 (10:10): open 99.5, high 102, low 99, close 101 -> trades through previous high (100)
    # Bar 2 (10:20): open 101, high 103, low 100.5, close 102
    rows = [
        ("10:00:00", 99.0),
        ("10:05:00", 100.0),
        ("10:09:00", 98.0),
        ("10:09:59", 99.5),
        ("10:10:00", 99.5),
        ("10:14:00", 102.0),
        ("10:17:00", 99.0),
        ("10:19:59", 101.0),
        ("10:20:00", 101.0),
        ("10:25:00", 103.0),
        ("10:27:00", 100.5),
        ("10:29:59", 102.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    result = backtest(frame, strategy=PreviousHighBreakout(), symbol=SYMBOL)
    trades = result.trades
    assert len(trades) == 1
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_price"].tolist() == [100.0]
    assert trades["side"].tolist() == ["long"]


# Criterion 3: Pullback price below open fills when low reaches it
def test_pullback_price_below_open_fills_at_price(tmp_path) -> None:
    # Bar 0 (10:00): flat 100
    # Bar 1 (10:10): open 100, high 100.5, low 95, close 97
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 100.5),
        ("10:15:00", 95.0),
        ("10:19:59", 97.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    # Pullback buy at 96.0 on bar 1 (below open of 100.0)
    result = backtest(frame, strategy=Scripted({1: TradeOrder.buy(price=96.0)}), symbol=SYMBOL)
    trades = result.trades
    assert len(trades) == 1
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_price"].tolist() == [96.0]


# Criterion 4: Price outside bar range raises descriptive error
def test_price_outside_bar_range_fails_with_descriptive_error(tmp_path) -> None:
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 101.0),
        ("10:15:00", 99.0),
        ("10:19:59", 100.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    with pytest.raises(ValueError) as exc_info:
        backtest(frame, strategy=Scripted({1: TradeOrder.buy(price=105.0)}), symbol=SYMBOL)

    msg = str(exc_info.value)
    assert "Scripted" in msg
    assert "105" in msg
    assert "bar 1" in msg or "10:10" in msg
    assert "99" in msg and "101" in msg


# Criterion 5: Priced order with target needs ticks; with ticks target resolves after touch
def test_priced_order_with_target_needs_ticks_and_resolves_after_touch(tmp_path) -> None:
    # Bar 0 (10:00): flat 100
    # Bar 1 (10:10): open 100 -> low 95 (touch of 96) -> high 112 -> close 108
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 95.0),
        ("10:15:00", 112.0),
        ("10:19:59", 108.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    # Without ticks=: raises existing ticks error
    with pytest.raises(ValueError, match="ticks="):
        backtest(
            frame,
            strategy=Scripted({1: TradeOrder.buy(price=96.0, take_profit=110.0)}),
            symbol=SYMBOL,
        )

    # With ticks=: target resolves after the touch
    result = backtest(
        frame,
        strategy=Scripted({1: TradeOrder.buy(price=96.0, take_profit=110.0)}),
        symbol=SYMBOL,
        ticks=store,
    )
    trades = result.trades
    assert len(trades) == 1
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_price"].tolist() == [96.0]
    assert trades["exit_reason"].tolist() == ["TAKE_PROFIT"]
    assert trades["exit_price"].tolist() == [110.0]


# Criterion 6: Unpriced order run is identical to today's output
def test_unpriced_orders_unaffected(tmp_path) -> None:
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 101.0),
        ("10:15:00", 102.0),
        ("10:20:00", 103.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    # Unpriced buy on bar 0 fills at bar 1 open (101.0)
    result = backtest(frame, strategy=Scripted({0: TradeOrder.buy()}), symbol=SYMBOL)
    trades = result.trades
    assert len(trades) == 1
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_price"].tolist() == [101.0]


# Review focus: runtime-hook path and position context
def test_priced_order_with_runtime_position_hook(tmp_path) -> None:
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 100.5),
        ("10:15:00", 95.0),
        ("10:19:59", 97.0),
        ("10:20:00", 97.0),
        ("10:25:00", 98.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    observed_positions = []

    class RuntimePositionStrategy(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            observed_positions.append((len(frame) - 1, positions))
            if len(frame) == 2:
                return TradeOrder.buy(price=96.0)
            return None

    result = backtest(frame, strategy=RuntimePositionStrategy(), symbol=SYMBOL)
    trades = result.trades
    assert len(trades) == 1
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_price"].tolist() == [96.0]

    # Bar 2 hook sees the position opened on bar 1
    assert len(observed_positions) >= 3
    bar2_obs = observed_positions[2]
    assert bar2_obs[0] == 2
    assert len(bar2_obs[1]) == 1
    assert bar2_obs[1][0].entry_price == 96.0
    assert bar2_obs[1][0].side == "long"


# Review focus: A price outside [low, high] on a bar the screen skipped still raises
def test_price_outside_range_on_skipped_screen_still_raises(tmp_path) -> None:
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 101.0),
        ("10:15:00", 99.0),
        ("10:19:59", 100.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    class PhasedScreenSkipped(ResearchStrategy):
        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
            # Screen explicitly returns None (skips tick replay)
            return None

        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            if len(frame) == 2:
                return TradeOrder.buy(price=105.0)
            return None

    with pytest.raises(ValueError) as exc_info:
        backtest(frame, strategy=PhasedScreenSkipped(), symbol=SYMBOL, ticks=store)

    msg = str(exc_info.value)
    assert "PhasedScreenSkipped" in msg
    assert "105" in msg
    assert "bar 1" in msg or "10:10" in msg
    assert "99" in msg and "101" in msg
