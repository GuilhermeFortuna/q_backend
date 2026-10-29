"""Closed-bar context: coarse bars joined to a finer frame without look-ahead (Q-140)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest, closed_bars
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
DAY_ONE = date(2026, 10, 5)
DAY_TWO = date(2026, 10, 6)
CONTEXT_COLUMNS = ["open", "high", "low", "close", "tick_volume"]


def _write_dense_session(root, day: date, seed: int, minutes: int) -> None:
    """One print every 20 seconds from 10:00 Brasília time, so every M1 and M5 bin has trades."""
    rng = np.random.default_rng(seed)
    start = datetime(day.year, day.month, day.day, 10, 0, 0, tzinfo=timezone.utc)
    count = minutes * 3
    moments = [start + timedelta(seconds=20 * step) for step in range(count)]
    prices = np.round(100.0 + np.cumsum(rng.normal(0.0, 0.5, count)), 1)
    arrays = {
        # MT5 stores broker wall-clock time as the UTC components of the epoch.
        "time_msc": np.asarray([int(moment.timestamp() * 1000) for moment in moments], dtype=np.int64),
        "bid": prices,
        "ask": prices,
        "last": prices,
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.full(count, 32, dtype=np.int32),
    }
    directory = root / _slug_symbol(SYMBOL)
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "ticks"
    _write_dense_session(root, DAY_ONE, seed=1, minutes=40)
    _write_dense_session(root, DAY_TWO, seed=2, minutes=40)
    return TickStore(SYMBOL, root=root)


@pytest.fixture
def m1(store):
    return store.bars("M1", start=DAY_ONE.isoformat())


def _independent_m5_visible_at_each_row(m1: pd.DataFrame) -> pd.DataFrame:
    """M5 bars built from the M1 rows by a plain groupby, each visible from its close onward.

    Each M1 row takes the M5 bar with the latest close at or before the row's close.
    """
    group = m1.groupby(m1.index.floor("5min"))
    m5 = pd.DataFrame(
        {
            "open": group["open"].first(),
            "high": group["high"].max(),
            "low": group["low"].min(),
            "close": group["close"].last(),
            "tick_volume": group["tick_volume"].sum(),
        }
    )
    closed = m5.set_axis(m5.index + pd.Timedelta(minutes=5))
    row_closes = m1.index + pd.Timedelta(minutes=1)
    return closed.reindex(row_closes, method="ffill").set_axis(m1.index)


def test_closed_bars_matches_m5_bars_built_independently_and_shifted_to_completed_bars(m1):
    context = closed_bars(m1, "M5")

    expected = _independent_m5_visible_at_each_row(m1)
    pd.testing.assert_frame_equal(
        context[CONTEXT_COLUMNS], expected[CONTEXT_COLUMNS], check_dtype=False, check_freq=False
    )


def test_a_row_inside_a_forming_coarse_bar_sees_the_previous_coarse_bar(m1):
    context = closed_bars(m1, "M5")
    first_open = m1.index[0].floor("5min")
    first_bar_close = m1.loc[first_open : first_open + pd.Timedelta(minutes=4), "close"].iloc[-1]

    # The first M5 bar closes at 10:05. The rows opening 10:00 through 10:03 see no closed bar yet.
    assert context.loc[first_open : first_open + pd.Timedelta(minutes=3)].isna().all().all()

    # The row opening 10:04 closes at 10:05, the moment the first M5 bar completes.
    assert context.loc[first_open + pd.Timedelta(minutes=4), "close"] == pytest.approx(first_bar_close)

    # Rows inside the next forming bar (opening 10:05) still see the bar that closed at 10:05.
    forming_row = first_open + pd.Timedelta(minutes=7)
    assert context.loc[forming_row, "close"] == pytest.approx(first_bar_close)


def test_the_output_is_aligned_to_the_frame_index(m1):
    context = closed_bars(m1, "M10")

    assert context.index.equals(m1.index)
    assert list(context.columns) == CONTEXT_COLUMNS


def test_closed_bars_refuses_a_timeframe_that_is_not_a_multiple_of_the_frame(m1):
    ten_minute_frame = m1.iloc[::10].copy()
    ten_minute_frame.attrs["q_research"] = {**m1.attrs["q_research"], "timeframe": "M10"}

    with pytest.raises(ValueError, match="not a multiple"):
        closed_bars(ten_minute_frame, "M15")


class ForwardingForming(ResearchStrategy):
    """Reads each M5 bar's final close on its first M1 row, before that bar has completed."""

    decision_timeframe = "M1"

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["m5_close"] = frame["close"].groupby(frame.index.floor("5min")).transform("last")
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy() if len(frame) == 1 else None


class ReadsClosedBars(ResearchStrategy):
    decision_timeframe = "M1"

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["m5_close"] = closed_bars(frame, "M5")["close"]
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy() if len(frame) == 1 else None


def test_a_strategy_reading_the_forming_coarse_bar_fails_the_causality_check(m1):
    with pytest.raises(ValueError, match=r"column 'm5_close'"):
        backtest(m1, strategy=ForwardingForming(), symbol=SYMBOL, check_causality=True)


def test_a_strategy_using_closed_bars_passes_the_causality_check(m1):
    result = backtest(m1, strategy=ReadsClosedBars(), symbol=SYMBOL, check_causality=True)

    assert len(result.trades) == 1
