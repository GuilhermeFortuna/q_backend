"""Resting stop and limit entries, and amended stops, in research backtests (Q-120)."""

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

# Bar 0 (10:00) is flat at 100; an entry queued there works from bar 1 onward.
FLAT_0 = [("10:00:00", 100.0), ("10:05:00", 100.0)]


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


def _bar(clock: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY.isoformat()} {clock}", tz=BRASILIA_TZ)


def _run(frame, strategy, store=None, **kwargs):
    return backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store, **kwargs)


class Scripted(ResearchStrategy):
    """Places the order named for each bar position, once per bar, and holds no position logic."""

    def __init__(self, orders: dict[int, TradeOrder]) -> None:
        self.orders = orders

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return self.orders.get(len(frame) - 1)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


@pytest.mark.parametrize(
    ("order", "bar_one", "fill_price", "fill_clock", "order_kind"),
    [
        # A buy stop fills at the first trade at or above 102: 102.5, not the order price.
        (
            TradeOrder.buy_stop(102.0, stop_loss=95.0, take_profit=120.0),
            [("10:10:00", 100.0), ("10:12:00", 102.5)],
            102.5,
            "10:12",
            "stop",
        ),
        # A buy limit fills at 98 once a trade prints strictly below it.
        (
            TradeOrder.buy_limit(98.0, stop_loss=95.0, take_profit=120.0),
            [("10:10:00", 100.0), ("10:12:00", 97.5)],
            98.0,
            "10:12",
            "limit",
        ),
        # A sell stop fills at the first trade at or below 98: 97.5.
        (
            TradeOrder.sell_stop(98.0, stop_loss=105.0, take_profit=90.0),
            [("10:10:00", 100.0), ("10:12:00", 97.5)],
            97.5,
            "10:12",
            "stop",
        ),
        # A sell limit fills at 102 once a trade prints strictly above it.
        (
            TradeOrder.sell_limit(102.0, stop_loss=105.0, take_profit=90.0),
            [("10:10:00", 100.0), ("10:12:00", 103.0)],
            102.0,
            "10:12",
            "limit",
        ),
    ],
)
def test_resting_entries_fill_at_kernel_prices_with_kind_and_tick_time(
    root, order, bar_one, fill_price, fill_clock, order_kind
):
    store = _write_session(root, FLAT_0 + bar_one + [("10:14:00", 101.0)])
    result = _run(_m10_frame(store), Scripted({0: order}), store)

    trades = result.trades
    assert trades["order_kind"].tolist() == [order_kind]
    assert trades["entry_price"].tolist() == [fill_price]
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_tick_time"].tolist() == [_bar(fill_clock)]
    assert trades["stop_loss"].tolist() == [order.stop_loss]
    assert trades["take_profit"].tolist() == [order.take_profit]


def test_market_entries_report_market_kind_and_no_tick_time(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:15:00", 101.0)])
    result = _run(_m10_frame(store), Scripted({0: TradeOrder.buy()}), store)

    assert result.trades["order_kind"].tolist() == ["market"]
    assert result.trades["entry_tick_time"].isna().all()


def test_a_limit_does_not_fill_on_an_equal_print(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 98.0), ("10:13:00", 99.0)])
    result = _run(_m10_frame(store), Scripted({0: TradeOrder.buy_limit(98.0)}), store)

    assert len(result.trades) == 0


def test_an_order_fills_only_within_its_expiry(root):
    # Placed on bar 0 with expire_bars=1: it can fill on bar 1 only, so the bar-2 price of 106 is too late.
    rows = FLAT_0 + [("10:10:00", 100.0), ("10:12:00", 101.0), ("10:20:00", 106.0), ("10:22:00", 107.0)]
    store = _write_session(root, rows)
    expired = _run(_m10_frame(store), Scripted({0: TradeOrder.buy_stop(105.0, expire_bars=1)}), store)
    assert len(expired.trades) == 0

    # With expire_bars=2 the same order may fill on bar 2, at the first trade at or above 105.
    live = _run(_m10_frame(store), Scripted({0: TradeOrder.buy_stop(105.0, expire_bars=2)}), store)
    assert live.trades["entry_price"].tolist() == [106.0]
    assert live.trades["entry_tick_time"].tolist() == [_bar("10:20")]


def test_a_new_entry_replaces_the_working_order(root):
    # Bar 0 places a limit at 96. Bar 1 replaces it with a stop at 102 before any trade reaches 96.
    orders = {0: TradeOrder.buy_limit(96.0, expire_bars=None), 1: TradeOrder.buy_stop(102.0, expire_bars=None)}
    rows = FLAT_0 + [("10:10:00", 100.0), ("10:12:00", 99.0), ("10:20:00", 95.0), ("10:22:00", 103.0)]
    store = _write_session(root, rows)
    result = _run(_m10_frame(store), Scripted(orders), store)

    assert result.trades["order_kind"].tolist() == ["stop"]
    assert result.trades["entry_price"].tolist() == [103.0]
    assert result.trades["entry_tick_time"].tolist() == [_bar("10:22")]


def test_cancel_removes_the_working_order(root):
    rows = FLAT_0 + [("10:10:00", 100.0), ("10:12:00", 101.0), ("10:20:00", 103.0)]
    store = _write_session(root, rows)
    orders = {0: TradeOrder.buy_stop(102.0, expire_bars=None), 1: TradeOrder.cancel()}
    result = _run(_m10_frame(store), Scripted(orders), store)

    assert len(result.trades) == 0


class PhasedTrail(ResearchStrategy):
    """A long trailing stop written as a phase-aware exit: the stop is 95 or the lowest closed low so far."""

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy(stop_loss=95.0) if len(frame) == 1 else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        if phase == "bar" or not positions:
            return None
        # The amendment decided on bar k applies from bar k+1, so bar j is stopped at the lows of bars 1..j-1.
        stop = max([95.0, *frame["low"].iloc[1:-1]])
        if phase == "screen":
            reached = frame["low"].iloc[-1] <= stop
        else:
            reached = frame["close"].iloc[-1] <= stop
        return TradeOrder.close() if reached else None


class AmendTrail(ResearchStrategy):
    """The same trailing stop written with ``amend``, recording the stop each decision sees."""

    def __init__(self) -> None:
        self.seen: list[float | None] = []

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy(stop_loss=95.0) if len(frame) == 1 else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not positions:
            return None
        self.seen.append(positions[0].stop_loss)
        low = frame["low"].iloc[-1]
        if positions[0].stop_loss is None or low > positions[0].stop_loss:
            return TradeOrder.amend(stop_loss=low)
        return None


# Trailing levels: 95 at entry, 100 from bar 1's low, 103 from bar 2's low. Bar 3 trades down to 102.5.
TRAIL_ROWS = FLAT_0 + [
    ("10:10:00", 100.0),
    ("10:12:00", 101.0),
    ("10:14:00", 102.0),
    ("10:20:00", 103.0),
    ("10:22:00", 105.0),
    ("10:24:00", 104.0),
    ("10:30:00", 104.0),
    ("10:32:00", 106.0),
    ("10:34:00", 102.5),
    ("10:36:00", 102.9),
    ("10:38:00", 102.0),
]


def test_amended_stop_exits_at_the_latest_level_and_positions_show_each_amendment(root):
    store = _write_session(root, TRAIL_ROWS)
    strategy = AmendTrail()
    result = _run(_m10_frame(store), strategy, store)

    trades = result.trades
    assert trades["exit_reason"].tolist() == ["STOP_LOSS"]
    assert trades["exit_price"].tolist() == [102.5]
    assert trades["exit_tick_time"].tolist() == [_bar("10:34")]
    assert trades["stop_loss"].tolist() == [95.0]
    assert trades["final_stop_loss"].tolist() == [103.0]
    # Bar 1 sees the entry's 95, bar 2 sees the 100 amended on bar 1. Bar 3's callback runs after its
    # intrabar stop, so the trade has already closed; 103 is the level in force at that exit.
    assert strategy.seen == [95.0, 100.0]


def test_amend_trailing_stop_matches_the_phase_aware_exit(root):
    store = _write_session(root, TRAIL_ROWS)
    phased = _run(_m10_frame(store), PhasedTrail(), store).trades
    amended = _run(_m10_frame(store), AmendTrail(), store).trades

    assert phased["exit_time"].tolist() == amended["exit_time"].tolist() == [_bar("10:30")]
    assert phased["exit_tick_time"].tolist() == amended["exit_tick_time"].tolist() == [_bar("10:34")]
    assert phased["exit_price"].tolist() == amended["exit_price"].tolist() == [102.5]
    assert phased["exit_reason"].tolist() == ["SIGNAL"]
    assert amended["exit_reason"].tolist() == ["STOP_LOSS"]


def test_amend_while_flat_does_nothing(root):
    class AmendAlways(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return None

        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.amend(stop_loss=99.0)

    store = _write_session(root, TRAIL_ROWS)
    result = _run(_m10_frame(store), AmendAlways(), store)

    assert len(result.trades) == 0


def test_resting_orders_and_amendments_require_ticks(root):
    class AmendOnly(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.buy() if len(frame) == 1 else None

        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.amend(stop_loss=99.0) if positions else None

    store = _write_session(root, TRAIL_ROWS)
    frame = _m10_frame(store)

    with pytest.raises(ValueError, match=r"Scripted entry at bar .* rests a stop order.*pass ticks= to backtest"):
        _run(frame, Scripted({0: TradeOrder.buy_stop(102.0)}))
    with pytest.raises(ValueError, match=r"AmendOnly amend at bar .* moves stop or take-profit levels.*pass ticks="):
        _run(frame, AmendOnly())


def test_misplaced_actions_raise_with_hook_phase_and_bar(root):
    class AmendFromEntry(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.amend(stop_loss=99.0)

    class CancelFromExit(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.buy() if len(frame) == 1 else None

        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return TradeOrder.cancel()

    class AmendInScreen(PhasedTrail):
        def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
            return TradeOrder.amend(stop_loss=99.0) if phase == "screen" and positions else None

    store = _write_session(root, TRAIL_ROWS)
    frame = _m10_frame(store)
    with pytest.raises(ValueError, match=r"AmendFromEntry\.entry_strategy at bar .*illegal action 'amend'"):
        _run(frame, AmendFromEntry(), store)
    with pytest.raises(ValueError, match=r"CancelFromExit\.exit_strategy at bar .*illegal action 'cancel'"):
        _run(frame, CancelFromExit(), store)
    with pytest.raises(
        ValueError, match=r"AmendInScreen\.exit_strategy at bar .*\(phase screen\) returned illegal action 'amend'"
    ):
        _run(frame, AmendInScreen(), store)
