"""Migration-table equivalence: priced orders against their live-placeable replacements (Q-139).

Each pair runs the legacy same-candle form and its replacement on the same ticks. The identical rows
must give ``compare_results(...).identical``; the known-difference rows give the documented differences.
"""

from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from q_backend.research import PricedOrderDeprecationWarning, ResearchStrategy, TradeOrder, compare_results
from tests.research.test_resting_orders_backtest import _m10_frame, _run, _write_session

pytestmark = pytest.mark.filterwarnings("ignore::q_backend.research.orders.PricedOrderDeprecationWarning")


class Legacy(ResearchStrategy):
    """Places the priced order named for each bar position; ``exits`` maps bar positions to exit orders."""

    def __init__(self, entries=None, exits=None, exit_fn=None) -> None:
        self.entries = entries or {}
        self.exits = exits or {}
        self.exit_fn = exit_fn

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return self.entries.get(len(frame) - 1)

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if self.exit_fn is not None:
            return self.exit_fn(frame, positions)
        return self.exits.get(len(frame) - 1) if positions else None


def _run_both(tmp_path, rows, legacy, port):
    store = _write_session(tmp_path, rows)
    frame = _m10_frame(store)
    return _run(frame, legacy, store), _run(frame, port, store)


# Bar 0 (10:00) decides; bar 1 (10:10) is the candle the entry fills on, bar 2 (10:20) the next.


def test_priced_entry_at_a_fixed_level_equals_a_resting_stop_placed_at_the_previous_close(tmp_path):
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 106.0),
        ("10:14:00", 108.0),
        ("10:20:00", 107.0),
        ("10:22:00", 94.0),
    ]
    legacy = Legacy(entries={1: TradeOrder.buy(price=105.0, stop_loss=95.0, take_profit=120.0, reason="break")})
    port = Legacy(
        entries={0: TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=120.0, expire_bars=1, reason="break")}
    )
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert report.identical, str(report)
    assert report.matched == 1


def test_priced_entry_list_equals_an_oco_group_of_resting_entries(tmp_path):
    # The 10:10 candle reaches 106 before 94, so the long leg fills first and the short is cancelled.
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:12:00", 106.0), ("10:14:00", 94.0), ("10:20:00", 100.0)]
    legacy = Legacy(
        entries={
            1: [
                TradeOrder.buy(price=105.0, stop_loss=95.0, take_profit=120.0, reason="long"),
                TradeOrder.sell(price=95.0, stop_loss=105.0, take_profit=80.0, reason="short"),
            ]
        }
    )
    port = Legacy(
        entries={
            0: TradeOrder.oco(
                TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=120.0, expire_bars=1, reason="long"),
                TradeOrder.sell_stop(95.0, stop_loss=105.0, take_profit=80.0, expire_bars=1, reason="short"),
            )
        }
    )
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert report.identical, str(report)
    assert old.trades.iloc[0]["entry_reason"] == "long"
    assert old.trades.iloc[0]["entry_price"] == 106.0


def test_priced_stop_close_at_a_fixed_level_equals_a_stop_amended_at_the_entry_bars_close(tmp_path):
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 100.0), ("10:22:00", 95.0), ("10:30:00", 99.0)]
    legacy = Legacy(
        entries={0: TradeOrder.buy()},
        exits={2: TradeOrder.close(price=95.0, reason="stop")},
    )
    port = Legacy(
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.amend(stop_loss=95.0)},
    )
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    # Fills, times and PnL match. The label does not: the engine names a protective stop-out
    # STOP_LOSS, whereas a priced close carries the reason it was given. A label-only difference.
    report = compare_results(old, new)
    assert report.identical, str(report)
    assert report.differing == ()
    assert [label.field for label in report.labels] == ["exit_reason"]
    assert old.trades.iloc[0]["exit_price"] == new.trades.iloc[0]["exit_price"] == 95.0
    assert old.trades.iloc[0]["exit_tick_time"] == new.trades.iloc[0]["exit_tick_time"]


def test_priced_close_at_the_open_after_a_time_equals_a_timed_exit(tmp_path):
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 101.0), ("10:24:00", 101.5), ("10:30:00", 102.0)]

    def legacy_exit(frame: pd.DataFrame, positions: tuple):
        if not positions or frame.index[-1].time() < time(10, 20):
            return None
        return TradeOrder.close(price=float(frame.iloc[-1]["open"]), reason="time up")

    legacy = Legacy(entries={0: TradeOrder.buy()}, exit_fn=legacy_exit)
    port = Legacy(entries={0: TradeOrder.buy()}, exits={1: TradeOrder.close(at=time(10, 20), reason="time up")})
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert report.identical, str(report)
    assert old.trades.iloc[0]["exit_price"] == 101.0


def test_a_strictly_beyond_condition_enters_on_an_exact_touch_once_it_is_a_stop(tmp_path):
    # The legacy hook enters only when the candle's high is strictly above the previous high (101).
    # The port's stop at 101 fills on the exact touch, which the legacy form never takes.
    rows = [("10:00:00", 100.0), ("10:00:30", 101.0), ("10:10:00", 100.0), ("10:12:00", 101.0), ("10:14:00", 99.0)]

    def legacy_entry(frame: pd.DataFrame, positions: tuple = ()):
        if len(frame) != 2 or frame.iloc[-1]["high"] <= frame.iloc[-2]["high"]:
            return None
        return TradeOrder.buy(price=float(frame.iloc[-2]["high"]), reason="breakout")

    class LegacyBreakout(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return legacy_entry(frame, positions)

    class PortBreakout(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            if len(frame) == 1:
                return TradeOrder.buy_stop(float(frame.iloc[-1]["high"]), expire_bars=1, reason="breakout")
            return None

    old, new = _run_both(tmp_path, rows, LegacyBreakout(), PortBreakout())
    report = compare_results(old, new)
    assert not report.identical
    assert report.differing == ()
    assert report.left_only == ()
    [touch] = report.right_only
    assert touch["entry_price"] == 101.0
    assert touch["entry_time"] == pd.Timestamp("2026-10-05 10:10", tz="America/Sao_Paulo")


def test_a_bracket_entry_filled_at_the_open_measures_its_stop_from_the_fill(tmp_path):
    # Known difference: the bracket hook prices an entry the bar opened beyond at the open, max(open, level),
    # and computes its stop and target from that price. The resting stop keeps its level's stop: 107 - 10 = 97
    # against 105 - 10 = 95. The fill is the same; a tick at 96 stops out only the legacy form.
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 107.0),
        ("10:12:00", 108.0),
        ("10:20:00", 104.0),
        ("10:21:00", 96.0),
        ("10:22:00", 94.0),
        ("10:24:00", 100.0),
        ("10:40:00", 100.0),
    ]
    legacy = Legacy(
        entries={1: TradeOrder.buy(price=107.0, stop_loss=97.0, take_profit=117.0, reason="Bracket buy stop")},
        exits={4: TradeOrder.close(reason="x")},
    )
    port = Legacy(
        entries={
            0: TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=115.0, expire_bars=1, reason="Bracket buy stop")
        },
        exits={4: TradeOrder.close(reason="x")},
    )
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert report.matched == 0
    assert [difference.field for difference in report.differing] == ["exit_price"]
    assert old.trades.iloc[0]["entry_price"] == new.trades.iloc[0]["entry_price"] == 107.0
    assert old.trades.iloc[0]["exit_price"] == 96.0
    assert new.trades.iloc[0]["exit_price"] == 94.0


def test_a_re_entry_touched_only_before_an_intrabar_exit_is_rejected_while_the_resting_stop_fills_after_it(tmp_path):
    # Known difference: the hook sees no position on the bar its stop-out happens, and prices a new bracket entry
    # at max(open, level) = 103. The only trade at 103 is the bar's open, before the stop-out at 97, so the legacy
    # entry is rejected (touch_before_exit). The replacement rests at the previous close at level 101 while the
    # position is open, and fills at the first trade at or beyond 101 after the exit (102).
    rows = [
        ("10:00:00", 108.0),
        ("10:10:00", 108.0),
        ("10:20:00", 103.0),
        ("10:22:00", 97.0),
        ("10:24:00", 102.0),
        ("10:40:00", 102.0),
    ]
    first = TradeOrder.buy(stop_loss=98.0, take_profit=120.0, reason="first")
    legacy = Legacy(
        entries={0: first, 2: TradeOrder.buy(price=103.0, stop_loss=93.0, take_profit=113.0, reason="again")},
        exits={4: TradeOrder.close(reason="x")},
    )
    port = Legacy(
        entries={
            0: first,
            1: TradeOrder.buy_stop(101.0, stop_loss=91.0, take_profit=111.0, expire_bars=1, reason="again"),
        },
        exits={4: TradeOrder.close(reason="x")},
    )
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    assert old.rejected_entries["reason"].tolist() == ["touch_before_exit"]
    report = compare_results(old, new)
    assert report.matched == 1
    assert report.differing == () and report.left_only == ()
    [touch] = report.right_only
    assert touch["entry_price"] == 102.0
    assert touch["entry_time"] == pd.Timestamp("2026-10-05 10:20", tz="America/Sao_Paulo")


def test_a_stop_exit_on_an_exact_touch_fills_where_the_legacy_stop_needs_a_strict_breach(tmp_path):
    # Known difference, an extension of the strictly-beyond one to exits: the legacy stop closes only when
    # low < stop, so a trade at exactly 95 does not stop the long. A resting stop at 95, amended at the previous
    # close, fills on that exact touch at 95. The legacy form exits at its time close at 99.
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 100.0),
        ("10:20:00", 100.0),
        ("10:22:00", 95.0),
        ("10:24:00", 99.0),
        ("10:30:00", 99.0),
        ("10:40:00", 99.0),
    ]

    def legacy_exit(frame: pd.DataFrame, positions: tuple):
        if positions and frame.iloc[-1]["low"] < 95.0:
            return TradeOrder.close(price=95.0, reason="stop")
        if positions and len(frame) == 4:
            return TradeOrder.close(reason="time")
        return None

    legacy = Legacy(entries={0: TradeOrder.buy()}, exit_fn=legacy_exit)
    port = Legacy(
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.amend(stop_loss=95.0), 3: TradeOrder.close(reason="time")},
    )
    old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert [difference.field for difference in report.differing] == ["exit_time"]
    assert new.trades.iloc[0]["exit_price"] == 95.0
    assert old.trades.iloc[0]["exit_price"] == 99.0


def test_an_entry_bar_take_profit_on_the_fill_price_is_not_set_until_the_next_bar(tmp_path):
    # Known difference: the legacy form checks a target measured from the fill price on the entry bar itself
    # (entry 100 + 3 = 103), and the 10:12 trade reaches it. The replacement can set that target only once the
    # fill is known, at the close of the entry bar, so it first applies on the next bar and holds the trade.
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 104.0),
        ("10:20:00", 101.0),
        ("10:22:00", 101.0),
        ("10:30:00", 101.0),
        ("10:40:00", 101.0),
    ]

    def legacy_exit(frame: pd.DataFrame, positions: tuple):
        if positions and frame.iloc[-1]["high"] > positions[0].entry_price + 3.0:
            return TradeOrder.close(price=positions[0].entry_price + 3.0, reason="target")
        if positions and len(frame) == 4:
            return TradeOrder.close(reason="time")
        return None

    legacy = Legacy(entries={0: TradeOrder.buy()}, exit_fn=legacy_exit)
    port = Legacy(
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.amend(take_profit=103.0), 3: TradeOrder.close(reason="time")},
    )
    with pytest.warns(PricedOrderDeprecationWarning):
        old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert [difference.field for difference in report.differing] == ["exit_time"]
    assert old.trades.iloc[0]["exit_price"] == 103.0
    assert new.trades.iloc[0]["exit_price"] == 101.0


def test_several_priced_stops_exit_at_the_level_reached_first_while_one_stop_may_differ(tmp_path):
    # The legacy hook returns two stop closes; the ticks reach 97 first and the trade exits there.
    # A port with a single stop at 95 does not exit at 97, so the trade's exit differs.
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 100.0), ("10:22:00", 97.0), ("10:24:00", 95.0)]
    legacy = Legacy(
        entries={0: TradeOrder.buy()},
        exit_fn=lambda frame, positions: (
            [TradeOrder.close(price=97.0, reason="first"), TradeOrder.close(price=95.0, reason="second")]
            if positions and len(frame) == 3
            else None
        ),
    )
    port = Legacy(entries={0: TradeOrder.buy()}, exits={1: TradeOrder.amend(stop_loss=95.0)})
    old, new = _run_both(tmp_path, rows, legacy, port)
    report = compare_results(old, new)
    assert not report.identical
    [difference] = report.differing
    assert difference.field == "exit_price"
    assert (difference.left_value, difference.right_value) == (97.0, 95.0)
    assert difference.left["exit_reason"] == "first"
