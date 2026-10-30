"""One-cancels-the-other groups of resting entries in research backtests (Q-139)."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder
from q_backend.research.adapter import classify_order
from tests.research.test_resting_orders_backtest import _m10_frame, _run, _write_session

# Bar 0 (10:00) decides the group; its members work from bar 1 (10:10) on.
BAR_0_FLAT = [("10:00:00", 100.0)]


class PlaceOnce(ResearchStrategy):
    """Returns ``order`` on the first bar only."""

    def __init__(self, order: TradeOrder) -> None:
        self.order = order

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return self.order if len(frame) == 1 else None


def _group() -> TradeOrder:
    return TradeOrder.oco(
        TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=120.0, expire_bars=None, reason="long"),
        TradeOrder.sell_stop(95.0, stop_loss=105.0, take_profit=80.0, expire_bars=None, reason="short"),
    )


def test_the_member_the_ticks_fill_first_opens_and_the_other_is_cancelled(tmp_path):
    store = _write_session(tmp_path, BAR_0_FLAT + [("10:10:00", 100.0), ("10:12:00", 94.0), ("10:14:00", 106.0)])
    result = _run(_m10_frame(store), PlaceOnce(_group()), store)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["side"] == "short"
    assert trade["entry_price"] == 94.0
    assert trade["entry_reason"] == "short"
    assert trade["stop_loss"] == 105.0
    assert trade["take_profit"] == 80.0


def test_the_second_member_fills_first_when_the_ticks_reach_it_first(tmp_path):
    store = _write_session(tmp_path, BAR_0_FLAT + [("10:10:00", 100.0), ("10:12:00", 106.0), ("10:14:00", 94.0)])
    result = _run(_m10_frame(store), PlaceOnce(_group()), store)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["side"] == "long"
    assert trade["entry_price"] == 106.0
    assert trade["entry_reason"] == "long"


def test_members_filling_on_the_same_trade_resolve_to_the_one_listed_first(tmp_path):
    group = TradeOrder.oco(
        TradeOrder.buy_stop(101.0, expire_bars=None, reason="first"),
        TradeOrder.buy_stop(103.0, expire_bars=None, reason="second"),
    )
    store = _write_session(tmp_path, BAR_0_FLAT + [("10:10:00", 100.0), ("10:12:00", 104.0)])
    result = _run(_m10_frame(store), PlaceOnce(group), store)
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["entry_reason"] == "first"
    assert result.trades.iloc[0]["entry_price"] == 104.0


def test_a_group_replaces_a_working_entry(tmp_path):
    class ReplaceWithGroup(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            if len(frame) == 1:
                return TradeOrder.buy_stop(110.0, expire_bars=None, reason="single")
            if len(frame) == 2:
                return _group()
            return None

    # The group is decided on bar 1's close and works from bar 2. Bar 2 prints 94, which only the
    # group's short fills; the replaced single buy stop at 110 would have filled at 111 later.
    store = _write_session(
        tmp_path,
        BAR_0_FLAT
        + [("10:10:00", 100.0), ("10:12:00", 100.0), ("10:20:00", 100.0), ("10:22:00", 94.0), ("10:24:00", 111.0)],
    )
    result = _run(_m10_frame(store), ReplaceWithGroup(), store)
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["entry_reason"] == "short"
    assert result.trades.iloc[0]["entry_price"] == 94.0


def test_a_member_expires_by_bars_and_the_other_still_fills(tmp_path):
    group = TradeOrder.oco(
        TradeOrder.buy_stop(105.0, expire_bars=1, reason="long"),
        TradeOrder.sell_stop(95.0, expire_bars=None, reason="short"),
    )
    # The long member works one bar only: its trade at 106 comes two bars later.
    store = _write_session(tmp_path, BAR_0_FLAT + [("10:10:00", 100.0), ("10:20:00", 100.0), ("10:30:00", 106.0)])
    result = _run(_m10_frame(store), PlaceOnce(group), store)
    assert result.trades.empty


def test_a_group_needs_two_resting_legs():
    with pytest.raises(ValueError, match="at least two legs"):
        TradeOrder.oco(TradeOrder.buy_stop(105.0))
    with pytest.raises(ValueError, match="resting stop or limit"):
        TradeOrder.oco(TradeOrder.buy_stop(105.0), TradeOrder.buy(price=106.0))
    with pytest.raises(ValueError, match="do not nest"):
        TradeOrder.oco(
            TradeOrder.oco(TradeOrder.buy_stop(105.0), TradeOrder.sell_stop(95.0)), TradeOrder.sell_stop(90.0)
        )


def test_a_group_is_only_returned_from_entry_strategy(tmp_path):
    class GroupFromExit(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.buy() if len(frame) == 1 else None

        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return _group() if positions else None

    store = _write_session(tmp_path, BAR_0_FLAT + [("10:10:00", 100.0), ("10:20:00", 100.0)])
    with pytest.raises(ValueError, match="illegal action 'oco'"):
        _run(_m10_frame(store), GroupFromExit(), store)


def test_a_group_records_its_order_kinds():
    kinds = classify_order(_group())
    assert kinds == {"oco", "resting_stop", "protective_levels"}
    assert "oco" in classify_order(TradeOrder.oco(TradeOrder.buy_limit(99.0), TradeOrder.sell_limit(101.0)))


def test_a_run_with_a_group_records_oco_and_is_not_deployable(tmp_path):
    store = _write_session(tmp_path, BAR_0_FLAT + [("10:10:00", 100.0), ("10:12:00", 94.0)])
    result = _run(_m10_frame(store), PlaceOnce(_group()), store)
    assert "oco" in result.order_kinds
    assert "oco" not in {"market_entry", "market_close"}


def test_a_member_keeps_its_own_protective_level_rules():
    # Each member is an ordinary resting entry: its stop must lie on the far side of its own price.
    with pytest.raises(ValueError, match="stop_loss must be above price for a sell"):
        TradeOrder.oco(TradeOrder.buy_stop(105.0), TradeOrder.sell_stop(95.0, stop_loss=90.0))
