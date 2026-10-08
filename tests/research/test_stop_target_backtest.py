"""Stop and target orders confirmed from a TickStore in research backtests (Q-104)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.errors import NoMarketDataError
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
DAY = date(2026, 10, 5)


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
    """Places the given order on the bar whose position it names, once."""

    def __init__(self, orders: dict[int, TradeOrder]) -> None:
        self.orders = orders

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        return self.orders.get(len(frame) - 1)


class CountingStore(TickStore):
    def __init__(self, symbol: str, *, root) -> None:
        super().__init__(symbol, root=root)
        self.reads: list[pd.Timestamp] = []

    def trade_prices(self, start, end):
        self.reads.append(pd.Timestamp(start))
        return super().trade_prices(start, end)


def _run(frame, orders, store, **kwargs):
    return backtest(frame, strategy=Scripted(orders), symbol=SYMBOL, ticks=store, **kwargs)


def _bar(clock: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY.isoformat()} {clock}", tz=BRASILIA_TZ)


def _stamp(clock: str) -> pd.Timestamp:
    return _bar(clock)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


# Bar 0 (10:00) is flat at 100 so the entry queued there fills at bar 1's open of 100.
FLAT_0 = [("10:00:00", 100.0), ("10:05:00", 100.0)]


def test_long_stop_trading_first_inside_a_bar_closes_at_its_traded_price(root):
    store = _write_session(
        root,
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0), ("10:15:00", 97.0)],
    )
    frame = _m10_frame(store)
    result = _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)

    trades = result.trades
    assert trades["exit_reason"].tolist() == ["STOP_LOSS"]
    assert trades["exit_price"].tolist() == [94.0]
    assert trades["exit_time"].tolist() == [_bar("10:10")]
    assert trades["exit_tick_time"].tolist() == [_stamp("10:12")]
    assert trades["stop_loss"].tolist() == [95.0]
    assert trades["take_profit"].tolist() == [110.0]


def test_mirrored_short_stop_closes_at_the_first_price_at_or_above_it(root):
    store = _write_session(
        root,
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 104.0), ("10:12:00", 106.0), ("10:14:00", 101.0)],
    )
    frame = _m10_frame(store)
    result = _run(frame, {0: TradeOrder.sell(stop_loss=105.0, take_profit=90.0)}, store)

    trades = result.trades
    assert trades["side"].tolist() == ["short"]
    assert trades["exit_reason"].tolist() == ["STOP_LOSS"]
    assert trades["exit_price"].tolist() == [106.0]
    assert trades["exit_tick_time"].tolist() == [_stamp("10:12")]
    assert trades["pnl"].tolist() == [-6.0]


def test_both_levels_inside_one_bar_resolve_by_trade_order(root):
    target_first = _write_session(
        root / "a",
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 111.0), ("10:12:00", 94.0)],
    )
    stop_first = _write_session(
        root / "b",
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 94.0), ("10:12:00", 111.0)],
    )
    orders = {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}

    first = _run(_m10_frame(target_first), orders, target_first).trades
    assert first["exit_reason"].tolist() == ["TAKE_PROFIT"]
    assert first["exit_price"].tolist() == [110.0]
    assert first["exit_tick_time"].tolist() == [_stamp("10:11")]

    second = _run(_m10_frame(stop_first), orders, stop_first).trades
    assert second["exit_reason"].tolist() == ["STOP_LOSS"]
    assert second["exit_price"].tolist() == [94.0]
    assert second["exit_tick_time"].tolist() == [_stamp("10:11")]


def test_a_price_that_only_touches_the_target_does_not_fill(root):
    store = _write_session(
        root,
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 110.0), ("10:12:00", 105.0), ("10:14:00", 100.0)],
    )
    result = _run(_m10_frame(store), {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)

    assert result.trades["status"].tolist() == ["open"]
    assert result.trades["exit_reason"].isna().all()
    assert result.trades["exit_tick_time"].isna().all()


def test_a_gap_through_the_stop_fills_at_the_bars_open(root):
    store = _write_session(
        root,
        FLAT_0
        + [("10:10:00", 100.0), ("10:11:00", 101.0), ("10:12:00", 99.0)]
        + [("10:20:00", 90.0), ("10:21:00", 92.0), ("10:23:00", 93.0)],
    )
    # Entry queued on bar 0 fills at bar 1's open. Bar 2 opens through the stop.
    result = _run(_m10_frame(store), {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)

    assert result.trades["exit_price"].tolist() == [90.0]
    assert result.trades["exit_time"].tolist() == [_bar("10:20")]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:20")]


def test_an_entry_on_the_wrong_side_of_its_fill_is_rejected_and_reported(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:12:00", 101.0)])
    result = _run(_m10_frame(store), {0: TradeOrder.buy(stop_loss=101.0, take_profit=120.0)}, store)

    assert result.trades.empty
    rejected = result.rejected_entries
    assert rejected["time"].tolist() == [_bar("10:10")]
    assert rejected["side"].tolist() == ["long"]
    assert rejected["fill_price"].tolist() == [100.0]
    assert rejected["stop_loss"].tolist() == [101.0]
    assert rejected["take_profit"].tolist() == [120.0]


def test_equity_includes_the_protective_exit_on_the_bar_it_closed(root):
    store = _write_session(
        root,
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)],
    )
    result = _run(
        _m10_frame(store),
        {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)},
        store,
        initial_capital=1000.0,
    )

    assert result.equity.loc[_bar("10:10"), "realized_equity"] == 1000.0 - 6.0


def test_a_level_without_ticks_names_the_strategy_bar_and_argument(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0)])
    with pytest.raises(ValueError, match="ticks=") as exc_info:
        backtest(
            _m10_frame(store),
            strategy=Scripted({0: TradeOrder.buy(stop_loss=95.0)}),
            symbol=SYMBOL,
        )
    assert "Scripted" in str(exc_info.value)
    assert "2026-10-05 10:00" in str(exc_info.value)


def test_a_store_for_another_symbol_is_rejected(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0)], symbol="WIN$N")
    with pytest.raises(ValueError, match="WIN\\$N"):
        _run(_m10_frame(_write_session(root / "wdo", FLAT_0 + [("10:10:00", 100.0)])), {}, store)


def test_a_frame_that_does_not_match_the_store_is_an_error_naming_the_bar(root):
    store = _write_session(
        root,
        FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)],
    )
    frame = _m10_frame(store).copy()
    frame.iloc[1, frame.columns.get_loc("low")] = 80.0
    with pytest.raises(ValueError, match="TickStore.bars") as exc_info:
        _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)
    assert "Bar 1" in str(exc_info.value)


def test_a_bar_without_stored_ticks_fails_with_the_session_day_and_sync(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0)])
    frame = _m10_frame(store).copy()
    next_day = pd.Timestamp("2026-10-06 10:00", tz=BRASILIA_TZ)
    extra = frame.iloc[[1]].copy()
    extra.index = pd.DatetimeIndex([next_day], name="time")
    extra.loc[:, "low"] = 80.0
    extra.loc[:, "close"] = 85.0
    frame = pd.concat([frame, extra])
    orders = {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0), 2: TradeOrder.buy(stop_loss=95.0)}
    with pytest.raises(NoMarketDataError, match="2026-10-06") as exc_info:
        _run(frame, orders, store)
    assert "TickStore.sync" in str(exc_info.value)


def test_an_empty_needed_interval_fails_with_bar_context(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 101.0)])
    frame = _m10_frame(store)
    # Bar 2 falls on the stored session, but no prints fall in its interval.
    extra = frame.iloc[[1]].copy()
    extra.index = pd.DatetimeIndex([_bar("10:20")], name="time")
    extra.loc[:, ["open", "high", "low", "close"]] = [100.0, 100.0, 80.0, 90.0]
    frame = pd.concat([frame, extra])
    with pytest.raises(NoMarketDataError, match="bar 2") as exc_info:
        _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)
    assert "TickStore.sync" in str(exc_info.value)


def test_only_candidate_candles_read_ticks(root):
    store = _write_session(
        root,
        FLAT_0
        + [("10:10:00", 100.0), ("10:11:00", 101.0), ("10:12:00", 99.0)]
        + [("10:20:00", 100.0), ("10:21:00", 96.0), ("10:22:00", 94.0)],
    )
    counting = CountingStore(SYMBOL, root=root)
    frame = _m10_frame(counting)
    result = _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, counting)

    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:22")]
    assert counting.reads == [_bar("10:20")]


def test_a_strategy_without_levels_is_identical_with_and_without_ticks(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:20:00", 101.0)])
    counting = CountingStore(SYMBOL, root=root)
    frame = _m10_frame(store)
    orders = {0: TradeOrder.buy()}

    plain = backtest(frame, strategy=Scripted(orders), symbol=SYMBOL)
    with_store = backtest(frame, strategy=Scripted(orders), symbol=SYMBOL, ticks=counting)

    pd.testing.assert_frame_equal(plain.trades.drop(columns="trade_id"), with_store.trades.drop(columns="trade_id"))
    pd.testing.assert_frame_equal(plain.equity, with_store.equity)
    pd.testing.assert_frame_equal(plain.data, with_store.data)
    assert plain.metrics == with_store.metrics
    assert counting.reads == []
    assert with_store.rejected_entries.empty


def test_an_empty_frame_returns_the_complete_schemas(root):
    store = _write_session(root, FLAT_0)
    frame = _m10_frame(store).iloc[:0]
    result = _run(frame, {}, store)

    assert result.trades.columns.tolist() == [
        "trade_id",
        "symbol",
        "side",
        "status",
        "entry_time",
        "entry_price",
        "exit_time",
        "exit_price",
        "pnl",
        "quantity",
        "commission",
        "point_value",
        "exit_reason",
        "exit_tick_time",
        "stop_loss",
        "take_profit",
    ]
    assert result.rejected_entries.columns.tolist() == [
        "time",
        "side",
        "fill_price",
        "stop_loss",
        "take_profit",
    ]
    assert result.trades.empty and result.rejected_entries.empty


def test_a_candle_interval_ends_at_the_next_bar_or_the_session_end(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)])
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    class Recording(CountingStore):
        def trade_prices(self, start, end):
            seen.append((pd.Timestamp(start), pd.Timestamp(end)))
            return super().trade_prices(start, end)

    recording = Recording(SYMBOL, root=root)
    _run(_m10_frame(recording), {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, recording)
    assert seen == [(_bar("10:10"), pd.Timestamp("2026-10-06 00:00", tz=BRASILIA_TZ))]


def test_protective_fills_pay_the_configured_per_side_cost(root):
    from q_backend.backtesting.costs import TransactionCostConfig

    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)])
    result = _run(
        _m10_frame(store),
        {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)},
        store,
        costs=TransactionCostConfig(cost_per_contract=2.0, cost_bps=0.0),
    )
    assert result.trades["commission"].tolist() == [4.0]
    assert result.trades["pnl"].tolist() == [-6.0 - 4.0]
