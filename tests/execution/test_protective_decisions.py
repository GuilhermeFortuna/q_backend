"""What the forward evaluator asks for on a bar: resting entries, OCO groups, levels, amendments, cancels and timed exits."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from q_backend.execution.domain import PositionSide, SignalAction
from q_backend.execution.position_adapter import execution_position_to_trade
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.orders import TradeOrder
from q_backend.research.strategy import ResearchStrategy
from tests.execution.test_research_evaluator import _make_identity, _sample_day_bars


class _Scripted(ResearchStrategy):
    """Returns the given entry and exit orders on the bars where a scripted hook is set."""

    parameters = {}
    entry_orders: dict[str, tuple[TradeOrder, ...]] = {}
    exit_orders: dict[str, tuple[TradeOrder, ...]] = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        return self.entry_orders.get(_bar_key(frame), ())

    def exit_strategy(self, frame, positions=()):
        return self.exit_orders.get(_bar_key(frame), ())


def _bar_key(frame) -> str:
    return frame.index[-1].astimezone(BRASILIA_TZ).strftime("%H:%M")


def _scripted(entries=None, exits=None) -> _Scripted:
    strategy = _Scripted()
    strategy.entry_orders = dict(entries or {})
    strategy.exit_orders = dict(exits or {})
    return strategy


def _result_at(results, open_hhmm: str):
    """The decision for the bar that opened at ``open_hhmm`` (local time)."""
    for result in results:
        opened = result.bar_close_time.astimezone(BRASILIA_TZ) - timedelta(minutes=30)
        if opened.strftime("%H:%M") == open_hhmm:
            return result
    raise AssertionError(f"no result for the bar opening at {open_hhmm}")


def _evaluate(strategy, day_trade: bool = True, open_trade=None):
    bars = _sample_day_bars()
    evaluator = ResearchEvaluator(
        deployment_id="dep-decisions",
        identity=_make_identity(day_trade=day_trade),
        strategy_instance=strategy,
        window_bound=100,
        open_trade=open_trade,
    )
    return evaluator.ingest_completed_bars(bars)


def _open_long_trade():
    """A long position opened before the first bar, so exit hooks run from the start."""
    return execution_position_to_trade(
        deployment_id="dep-decisions",
        symbol="WIN$N",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        opened_at=datetime(2024, 1, 2, 9, 0, tzinfo=BRASILIA_TZ),
        point_value=1.0,
        stop_loss=Decimal("95"),
        take_profit=Decimal("150"),
    )


def test_resting_stop_entry_asks_for_a_pending_order_with_expiry_in_bars():
    # The 10:00 bar is a 30-minute bar: expire_bars=2 leaves the order to 11:30 local.
    strategy = _scripted(
        entries={"10:00": (TradeOrder.buy_stop(price=110.0, stop_loss=105.0, take_profit=120.0, expire_bars=2),)}
    )

    result = _result_at(_evaluate(strategy), "10:00")

    assert result.signal_action == SignalAction.HOLD
    assert result.pending_entry is not None
    assert result.pending_entry["side"] == "buy"
    assert result.pending_entry["order_type"] == "stop"
    assert result.pending_entry["price"] == 110.0
    assert result.pending_entry["stop_loss"] == 105.0
    assert result.pending_entry["take_profit"] == 120.0
    expires = result.pending_entry["expires_at"].astimezone(BRASILIA_TZ)
    assert expires.strftime("%H:%M") == "11:30"
    assert result.requested_quantity == 1.0
    assert result.manages_orders


def test_expire_at_a_time_of_day_is_its_next_occurrence_after_the_bar():
    strategy = _scripted(
        entries={"10:00": (TradeOrder.sell_limit(price=130.0, expire_bars=None, expire_at=time(10, 45)),)}
    )

    result = _result_at(_evaluate(strategy), "10:00")

    assert result.pending_entry["order_type"] == "limit"
    assert result.pending_entry["expires_at"].astimezone(BRASILIA_TZ).strftime("%H:%M") == "10:45"


def test_the_earlier_of_expire_bars_and_expire_at_wins():
    strategy = _scripted(entries={"10:00": (TradeOrder.buy_stop(price=110.0, expire_bars=5, expire_at=time(10, 45)),)})

    result = _result_at(_evaluate(strategy), "10:00")

    assert result.pending_entry["expires_at"].astimezone(BRASILIA_TZ).strftime("%H:%M") == "10:45"


def test_an_oco_group_asks_for_one_resting_entry_per_leg():
    group = TradeOrder.oco(
        TradeOrder.buy_stop(price=110.0, stop_loss=105.0, expire_bars=2),
        TradeOrder.sell_stop(price=90.0, stop_loss=95.0, expire_bars=2),
    )
    result = _result_at(_evaluate(_scripted(entries={"10:00": (group,)})), "10:00")

    assert result.pending_group is not None
    assert [leg["side"] for leg in result.pending_group] == ["buy", "sell"]
    assert [leg["price"] for leg in result.pending_group] == [110.0, 90.0]
    assert result.requested_quantity == 1.0


def test_a_market_entry_carries_its_protective_levels():
    result = _result_at(
        _evaluate(_scripted(entries={"10:00": (TradeOrder.buy(stop_loss=95.0, take_profit=120.0),)})),
        "10:00",
    )

    assert result.signal_action == SignalAction.BUY
    assert result.entry_stop_loss == 95.0
    assert result.entry_take_profit == 120.0
    assert result.pending_entry is None


def test_amend_on_an_open_position_asks_for_new_levels_without_closing():
    amend = {"10:00": (TradeOrder.amend(stop_loss=97.0),)}
    results = _evaluate(_scripted(exits=amend), open_trade=_open_long_trade())

    result = _result_at(results, "10:00")
    assert result.signal_action == SignalAction.HOLD
    assert result.protect_levels == {"stop_loss": 97.0, "take_profit": None}


def test_cancel_asks_for_the_working_entry_to_be_removed():
    result = _result_at(_evaluate(_scripted(entries={"10:00": (TradeOrder.cancel(),)})), "10:00")

    assert result.cancel_pending is True
    assert result.pending_entry is None


def test_a_timed_exit_records_its_instant_and_keeps_the_position_open():
    timed = {"10:00": (TradeOrder.close(at=time(15, 0)),)}
    results = _evaluate(_scripted(exits=timed), open_trade=_open_long_trade())

    result = _result_at(results, "10:00")
    assert result.signal_action == SignalAction.HOLD
    assert result.timed_exit_at is not None
    assert result.timed_exit_at.astimezone(BRASILIA_TZ).strftime("%H:%M") == "15:00"


def test_resting_entries_lapse_after_the_day_trade_window():
    result = _result_at(_evaluate(_scripted()), "16:30")

    assert result.cancel_pending is True


def test_a_removed_priced_entry_in_a_hook_pauses_the_deployment():
    class PricedEntry(_Scripted):
        def entry_strategy(self, frame, positions=()):
            return TradeOrder.buy(price=110.0) if _bar_key(frame) == "10:00" else None

    result = _result_at(_evaluate(PricedEntry()), "10:00")

    assert result.pause_requested is True
    assert result.risk_rejection_code == "hook_error"
    assert "priced entries are removed" in result.risk_rejection_message


@pytest.mark.parametrize("hhmm", ["09:00", "09:30"])
def test_quiet_bars_ask_for_nothing(hhmm):
    result = _result_at(_evaluate(_scripted()), hhmm)

    assert result.manages_orders is False
    assert result.pending_group is None
    assert result.timed_exit_at is None
