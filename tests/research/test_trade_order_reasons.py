"""Entry and close reasons carried by TradeOrder into research trades (Q-129)."""

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

# Bars are M10. Bar k opens on the first print of its window; fills happen at the next bar's open.
SESSION = [
    ("10:00:00", 100.0),
    ("10:05:00", 100.0),
    ("10:10:00", 101.0),
    ("10:15:00", 102.0),
    ("10:20:00", 103.0),
    ("10:25:00", 104.0),
    ("10:30:00", 103.0),
    ("10:35:00", 102.5),
    ("10:40:00", 101.0),
    ("10:45:00", 101.0),
]


def _write_session(root, rows, day=DAY, symbol=SYMBOL):
    """``rows``: ``(HH:MM:SS, price)`` trade prints for one session, in Brasília time."""
    times = []
    for clock, _ in rows:
        hour, minute, second = (int(part) for part in clock.split(":"))
        # MT5 stores broker wall-clock time as the UTC components of the epoch.
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
    """Places the orders named for each bar position; ``entries`` and ``exits`` are keyed by bar."""

    def __init__(self, entries: dict[int, TradeOrder] | None = None, exits: dict[int, TradeOrder] | None = None):
        self.entries = entries or {}
        self.exits = exits or {}

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return self.entries.get(len(frame) - 1)

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return self.exits.get(len(frame) - 1)


def _run(store: TickStore, strategy: ResearchStrategy, **kwargs):
    return backtest(_m10_frame(store), strategy=strategy, symbol=SYMBOL, ticks=store, **kwargs)


@pytest.fixture
def store(tmp_path):
    return _write_session(tmp_path / "ticks", SESSION)


def test_market_entry_and_plain_close_carry_their_reasons(store):
    result = _run(
        store,
        Scripted(
            entries={1: TradeOrder.buy(reason="VWAP reclaim")},
            exits={3: TradeOrder.close(reason="Momentum faded")},
        ),
    )
    trade = result.trades.iloc[0]
    assert trade["entry_reason"] == "VWAP reclaim"
    assert trade["exit_reason"] == "Momentum faded"
    assert trade["exit_time"] == pd.Timestamp(f"{DAY.isoformat()} 10:40", tz=BRASILIA_TZ)


def test_unreasoned_orders_keep_signal_and_null_reasons(store):
    result = _run(store, Scripted(entries={1: TradeOrder.buy()}, exits={3: TradeOrder.close()}))
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "SIGNAL"
    assert trade["entry_reason"] is None


def test_stop_exit_keeps_its_engine_label_over_a_reasoned_close(tmp_path):
    # Bar 3 trades down to 101.5, through the stop at 101.8, so the stop exits before the close is requested.
    rows = [*SESSION[:7], ("10:35:00", 101.5), ("10:40:00", 101.0), ("10:45:00", 101.0)]
    store = _write_session(tmp_path / "ticks", rows)
    result = _run(
        store,
        Scripted(
            entries={1: TradeOrder.buy(stop_loss=101.8, take_profit=110.0, reason="Entry with stop")},
            exits={3: TradeOrder.close(reason="Momentum faded")},
        ),
    )
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["entry_reason"] == "Entry with stop"


@pytest.mark.parametrize(
    ("entry", "bar_one", "entry_kind"),
    [
        # A buy stop fills at the first trade at or above 102: 102.5 at 10:12.
        (
            TradeOrder.buy_stop(102.0, stop_loss=95.0, take_profit=120.0, reason="Breakout"),
            [("10:10:00", 100.0), ("10:12:00", 102.5)],
            "stop",
        ),
        # A buy limit fills at 98 once a trade prints strictly below it.
        (
            TradeOrder.buy_limit(98.0, stop_loss=95.0, take_profit=120.0, reason="Pullback"),
            [("10:10:00", 100.0), ("10:12:00", 97.5)],
            "limit",
        ),
    ],
)
def test_resting_entries_carry_the_reason_of_the_order_that_placed_them(tmp_path, entry, bar_one, entry_kind):
    rows = [("10:00:00", 100.0), ("10:05:00", 100.0), *bar_one, ("10:20:00", 100.0), ("10:30:00", 100.0)]
    store = _write_session(tmp_path / "ticks", rows)
    result = _run(store, Scripted(entries={0: entry}), day_trade=True, day_trade_end_time="10:30")
    trade = result.trades.iloc[0]
    assert trade["order_kind"] == entry_kind
    assert trade["entry_reason"] == entry.reason


def test_a_reasoned_resting_order_replaced_by_an_unreasoned_one_loses_its_reason(tmp_path):
    # The bar-1 order replaces the working bar-0 order before it fills at 10:20, so the fill is the bar-1 order's.
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 100.0),
        ("10:20:00", 102.5),
        ("10:25:00", 102.5),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    result = _run(
        store,
        Scripted(
            entries={
                0: TradeOrder.buy_stop(102.0, stop_loss=95.0, take_profit=120.0, reason="Old"),
                1: TradeOrder.buy_stop(102.0, stop_loss=95.0, take_profit=120.0),
            }
        ),
    )
    assert result.trades["entry_reason"].isna().all()


@pytest.mark.parametrize("reason", ["", "   ", 3, b"bytes"])
def test_blank_or_non_string_reasons_are_rejected(reason):
    with pytest.raises(ValueError, match="reason"):
        TradeOrder.buy(reason=reason)
    with pytest.raises(ValueError, match="reason"):
        TradeOrder.close(reason=reason)


@pytest.mark.parametrize(
    "build",
    [
        lambda reason: TradeOrder.sell(reason=reason),
        lambda reason: TradeOrder.buy_stop(102.0, reason=reason),
        lambda reason: TradeOrder.buy_limit(98.0, reason=reason),
        lambda reason: TradeOrder.sell_stop(98.0, reason=reason),
        lambda reason: TradeOrder.sell_limit(102.0, reason=reason),
    ],
)
def test_every_entry_factory_carries_the_reason(build):
    assert build("label").reason == "label"
    assert build(None).reason is None


def test_amend_and_cancel_reject_a_reason():
    with pytest.raises(ValueError, match="amend"):
        TradeOrder("amend", stop_loss=95.0, reason="label")
    with pytest.raises(ValueError, match="cancel"):
        TradeOrder("cancel", reason="label")


def test_orders_without_a_reason_are_unchanged():
    order = TradeOrder.buy(stop_loss=95.0, take_profit=110.0)
    assert order == TradeOrder("buy", stop_loss=95.0, take_profit=110.0)
    assert order.reason is None
