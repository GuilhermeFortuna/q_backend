"""Rejection reporting and shared tick fixtures."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.candle_kernel import ChunkRun
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.results import rejected_entries_frame
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
DAY_1 = date(2026, 10, 5)
DAY_2 = date(2026, 10, 6)


def _bar(clock: str, day: date = DAY_1) -> pd.Timestamp:
    return pd.Timestamp(f"{day.isoformat()} {clock}", tz=BRASILIA_TZ)


def _write_session(root, rows, day=DAY_1, symbol=SYMBOL) -> TickStore:
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


def _m10_frame(store: TickStore, start: date = DAY_1, end: date | None = None) -> pd.DataFrame:
    end_str = end.isoformat() if end is not None else None
    return store.bars("M10", start=start.isoformat(), end=end_str)


class Scripted(ResearchStrategy):
    def __init__(self, orders: dict[int, TradeOrder]) -> None:
        self.orders = orders

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        return self.orders.get(len(frame) - 1)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


# Acceptance criterion 3:
# A wrong-side level refusal reports level_on_wrong_side, and an unknown code raises ValueError.
def test_wrong_side_refusal_reports_level_on_wrong_side(root) -> None:
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 101.0),
    ]
    store = _write_session(root, rows)
    frame = _m10_frame(store)

    result = backtest(
        frame,
        strategy=Scripted({0: TradeOrder.buy(stop_loss=101.0, take_profit=120.0)}),
        symbol=SYMBOL,
        ticks=store,
    )
    assert result.trades.empty
    rejected = result.rejected_entries
    assert len(rejected) == 1
    assert rejected["reason"].tolist() == ["level_on_wrong_side"]


def test_unknown_rejection_code_raises_value_error() -> None:
    empty_trades = {
        name: np.array([])
        for name in (
            "entry_bar",
            "exit_bar",
            "side",
            "quantity",
            "entry_price",
            "exit_price",
            "commission",
            "pnl",
            "exit_time_us",
            "entry_time_us",
            "entry_kind",
            "stop_loss",
            "take_profit",
            "final_stop_loss",
            "final_take_profit",
        )
    }
    bogus_rejected = {
        "bar": np.array([0]),
        "side": np.array([1]),
        "fill_price": np.array([100.0]),
        "stop_price": np.array([90.0]),
        "target_price": np.array([110.0]),
        "reason": np.array([99]),
    }
    run = ChunkRun(empty_trades, [], [], bogus_rejected)
    index = pd.date_range("2026-10-05 10:00", periods=5, freq="10min", tz=BRASILIA_TZ)

    with pytest.raises(ValueError, match="99"):
        rejected_entries_frame(run, index)


class MultiSessionStrategy(ResearchStrategy):
    def entry_strategy(self, f: pd.DataFrame) -> TradeOrder | None:
        ts = f.index[-1]
        if ts == _bar("10:00", DAY_1):
            return TradeOrder.buy(stop_loss=95.0, take_profit=110.0)
        if ts == _bar("10:00", DAY_2):
            return TradeOrder.buy(stop_loss=101.0, take_profit=120.0)
        return None


# Acceptance criterion 4:
# rejected_entries has the same columns and reasons with workers=1 and workers=2.
def test_parallel_sessions_preserve_rejected_reasons(root) -> None:
    # Session 1: a protective stop closes the trade
    rows_day1 = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:11:00", 102.0),
        ("10:12:00", 94.0),
        ("10:15:00", 96.0),
        ("10:19:59", 97.0),
        ("10:20:00", 97.0),
        ("10:25:00", 97.0),
    ]
    # Session 2: includes a level_on_wrong_side rejection
    rows_day2 = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:15:00", 101.0),
    ]
    _write_session(root, rows_day1, day=DAY_1)
    store = _write_session(root, rows_day2, day=DAY_2)
    frame = _m10_frame(store, start=DAY_1, end=DAY_2 + timedelta(days=1))

    res_seq = backtest(
        frame,
        strategy=MultiSessionStrategy(),
        symbol=SYMBOL,
        ticks=store,
        day_trade=True,
        workers=1,
    )
    res_par = backtest(
        frame,
        strategy=MultiSessionStrategy(),
        symbol=SYMBOL,
        ticks=store,
        day_trade=True,
        workers=2,
    )

    assert len(res_seq.rejected_entries) == 1
    assert "reason" in res_seq.rejected_entries.columns
    assert set(res_seq.rejected_entries["reason"]) == {"level_on_wrong_side"}
    pd.testing.assert_frame_equal(res_par.rejected_entries, res_seq.rejected_entries)
