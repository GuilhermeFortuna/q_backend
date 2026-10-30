"""Timed exits and expiry instants in research backtests (Q-139)."""

from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TradeOrder
from q_backend.research.adapter import ResearchStrategyAdapter, classify_order
from tests.research.test_resting_orders_backtest import SYMBOL, _m10_frame, _run, _write_session

DAY_TIME = pd.Timestamp("2026-10-05 10:25", tz=BRASILIA_TZ)


class LongFromBarZero(ResearchStrategy):
    """Buys at the first bar and asks ``exit`` for an exit on each later bar while the position is open."""

    def __init__(self, exit_order=None, exits_by_bar=None) -> None:
        self.exit_order = exit_order
        self.exits_by_bar = exits_by_bar

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy(reason="go") if len(frame) == 1 else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not positions:
            return None
        if self.exits_by_bar is not None:
            return self.exits_by_bar.get(len(frame) - 1)
        return self.exit_order


def test_a_timed_exit_closes_at_the_first_trade_at_or_after_its_instant(tmp_path):
    # The 10:00 bar decides the entry and the 10:10 bar, which opens the position, decides the exit.
    # A decision works from the bar after it, so the 10:26 trade is the first at or after 10:25 that counts.
    store = _write_session(
        tmp_path,
        [
            ("10:00:00", 100.0),
            ("10:10:00", 100.0),
            ("10:20:00", 100.0),
            ("10:20:30", 101.0),
            ("10:26:00", 103.0),
            ("10:30:00", 103.0),
        ],
    )
    result = _run(_m10_frame(store), LongFromBarZero(TradeOrder.close(at=time(10, 25), reason="time up")), store)
    trade = result.trades.iloc[0]
    assert trade["exit_price"] == 103.0
    assert trade["exit_tick_time"] == pd.Timestamp("2026-10-05 10:26", tz=BRASILIA_TZ)
    assert trade["exit_reason"] == "time up"


def test_a_timed_exit_with_no_trade_after_its_instant_fills_on_the_next_bars_first_trade(tmp_path):
    store = _write_session(
        tmp_path,
        [
            ("10:00:00", 100.0),
            ("10:10:00", 100.0),
            ("10:20:00", 101.0),
            ("10:21:00", 101.5),
            ("10:30:00", 102.0),
            ("10:35:00", 102.5),
        ],
    )
    result = _run(_m10_frame(store), LongFromBarZero(TradeOrder.close(at=time(10, 25))), store)
    trade = result.trades.iloc[0]
    assert trade["exit_price"] == 102.0
    assert trade["exit_tick_time"] == pd.Timestamp("2026-10-05 10:30", tz=BRASILIA_TZ)


def test_a_timed_exit_at_a_timestamp_reads_it_in_the_frame_timezone(tmp_path):
    store = _write_session(tmp_path, [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:26:00", 103.0)])
    result = _run(_m10_frame(store), LongFromBarZero(TradeOrder.close(at=DAY_TIME)), store)
    assert result.trades.iloc[0]["exit_price"] == 103.0


def test_a_later_timed_exit_replaces_the_working_one(tmp_path):
    # The 10:10 bar decides a 10:45 exit; the 10:20 bar replaces it with 10:25. Only the replacement
    # has passed when the 10:40 trade prints, so the trade closes there.
    store = _write_session(
        tmp_path,
        [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 100.0), ("10:26:00", 103.0), ("10:40:00", 105.0)],
    )
    strategy = LongFromBarZero(
        exits_by_bar={1: TradeOrder.close(at=time(10, 45)), 2: TradeOrder.close(at=time(10, 25), reason="second")}
    )
    result = _run(_m10_frame(store), strategy, store)
    trade = result.trades.iloc[0]
    assert trade["exit_price"] == 105.0
    assert trade["exit_reason"] == "second"


def test_a_timed_exit_while_flat_does_nothing(tmp_path):
    store = _write_session(
        tmp_path,
        [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 100.0), ("10:30:00", 101.0)],
    )

    class ExitFirstThenEnter(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.buy() if len(frame) == 3 else None

        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.close(at=time(10, 5)) if len(frame) == 1 else None

    result = _run(_m10_frame(store), ExitFirstThenEnter(), store)
    assert len(result.trades) == 1
    assert pd.isna(result.trades.iloc[0]["exit_time"])


def test_an_expiry_instant_cancels_a_resting_entry_that_has_not_filled(tmp_path):
    class ExpiringStop(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            if len(frame) == 1:
                return TradeOrder.buy_stop(105.0, expire_bars=None, expire_at=time(10, 15), reason="late")
            return None

    # The trade at 10:14 is before the 10:15 expiry and does not reach 105; the 10:20 trade is too late.
    store = _write_session(tmp_path, [("10:00:00", 100.0), ("10:14:00", 103.0), ("10:20:00", 106.0)])
    result = _run(_m10_frame(store), ExpiringStop(), store)
    assert result.trades.empty


def test_an_expiry_instant_fills_a_resting_entry_that_fills_before_it(tmp_path):
    class ExpiringStop(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            if len(frame) == 1:
                return TradeOrder.buy_stop(105.0, expire_bars=None, expire_at=time(10, 15))
            return None

    store = _write_session(tmp_path, [("10:00:00", 100.0), ("10:12:00", 106.0)])
    result = _run(_m10_frame(store), ExpiringStop(), store)
    assert result.trades.iloc[0]["entry_price"] == 106.0


def test_expire_bars_and_expire_at_together_expire_at_the_first_to_come(tmp_path):
    class BothLimits(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            if len(frame) == 1:
                return TradeOrder.buy_stop(105.0, expire_bars=1, expire_at=time(11, 0))
            return None

    # The bar limit (one bar, the 10:10 bar) ends the order before the 11:00 instant does.
    store = _write_session(tmp_path, [("10:00:00", 100.0), ("10:20:00", 100.0), ("10:30:00", 106.0)])
    assert _run(_m10_frame(store), BothLimits(), store).trades.empty


def test_a_time_of_day_already_passed_means_its_next_occurrence():
    adapter = ResearchStrategyAdapter(research_strategy=LongFromBarZero(), symbol=SYMBOL)
    decided = pd.Timestamp("2026-10-05 10:10", tz=BRASILIA_TZ)
    later = pd.Timestamp("2026-10-06 10:05", tz=BRASILIA_TZ)
    assert adapter._instant_us(time(10, 5), decided) == adapter._instant_us(later, decided)
    assert adapter._instant_us(time(10, 15), decided) == adapter._instant_us(
        pd.Timestamp("2026-10-05 10:15", tz=BRASILIA_TZ), decided
    )


def test_timed_exits_and_expiry_record_their_order_kinds():
    assert classify_order(TradeOrder.close(at=time(10, 25))) == {"timed_exit"}
    assert classify_order(TradeOrder.close()) == {"market_close"}
    assert classify_order(TradeOrder.buy_stop(105.0, expire_at=time(10, 15), expire_bars=None)) == {
        "resting_stop",
        "expiring_entry",
    }


def test_timed_and_expiring_orders_are_validated():
    with pytest.raises(ValueError, match="priced closes are removed"):
        TradeOrder.close(price=100.0, at=time(10, 25))
    with pytest.raises(ValueError, match="at applies to close orders only"):
        TradeOrder(action="buy", at=time(10, 25))
    with pytest.raises(ValueError, match="expire_at applies to stop and limit orders"):
        TradeOrder("buy", expire_at=time(10, 15))
    with pytest.raises(ValueError, match="naive"):
        TradeOrder.close(at=time(10, 25, tzinfo=BRASILIA_TZ))
    with pytest.raises(ValueError, match="datetime.time, a datetime or None"):
        TradeOrder.close(at="10:25")
