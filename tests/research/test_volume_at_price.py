"""Volume-at-price research helpers and tick store trades."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from q_backend.research import indicators
from q_backend.research.tick_store import TickStore
from quote_ticks import BUY_FLAG, SELL_FLAG, write_quote_ticks, write_trade_ticks

SYMBOL = "WDO$"
TICK = 0.5


def _bars(times):
    index = pd.DatetimeIndex(times, name="time").tz_localize("America/Sao_Paulo")
    close = 100.0 + np.arange(len(index)) * 0.1
    return pd.DataFrame(
        {"open": close - 0.01, "high": close + 0.2, "low": close - 0.2, "close": close},
        index=index,
    )


def test_trades_returns_ordered_columns(tmp_path):
    nine = datetime(2026, 9, 1, 9)
    write_trade_ticks(
        tmp_path,
        SYMBOL,
        [
            (nine, 100.0, 10.0, BUY_FLAG),
            (nine + timedelta(seconds=1), 100.5, 5.0, BUY_FLAG),
            (nine + timedelta(seconds=2), 100.0, 1.0, 0),
        ],
    )
    store = TickStore(SYMBOL, root=tmp_path)
    time_msc, last, volume, flags = store.trades(nine.date())
    assert time_msc.shape == last.shape == volume.shape == flags.shape
    assert np.all(time_msc[1:] >= time_msc[:-1])
    assert flags[2] == 0


def test_session_totals_match_real_volume(tmp_path):
    day1 = datetime(2026, 9, 1, 10)
    day2 = datetime(2026, 9, 2, 10)
    write_trade_ticks(
        tmp_path,
        SYMBOL,
        [
            (day1, 100.0, 10.0, BUY_FLAG),
            (day1 + timedelta(minutes=1), 100.5, 5.0, SELL_FLAG),
            (day2, 101.0, 7.0, BUY_FLAG),
        ],
    )
    store = TickStore(SYMBOL, root=tmp_path)
    profile = indicators.volume_at_price(
        store,
        start="2026-09-01",
        end="2026-09-03",
        tick_size=TICK,
        by="session",
    )
    assert profile.unavailable_reason is None
    assert len(profile.segments) == 2
    m1 = store.bars("M1", start="2026-09-01", end="2026-09-02")
    m2 = store.bars("M1", start="2026-09-02", end="2026-09-03")
    assert profile.segments.iloc[0]["total"] == pytest.approx(m1["real_volume"].sum())
    assert profile.segments.iloc[1]["total"] == pytest.approx(m2["real_volume"].sum())


def test_range_without_ticks_reports_reason(tmp_path):
    store = TickStore(SYMBOL, root=tmp_path)
    profile = indicators.volume_at_price(
        store,
        start="2026-09-01",
        end="2026-09-02",
        tick_size=TICK,
        by="range",
    )
    assert profile.rows.empty
    assert profile.unavailable_reason is not None


@pytest.mark.parametrize(
    "param,value,message",
    [
        ("row_ticks", 0, "row_ticks"),
        ("value_area", 1.5, "value_area"),
    ],
)
def test_invalid_parameters_name_the_field(tmp_path, param, value, message):
    store = TickStore(SYMBOL, root=tmp_path)
    kwargs = {
        "start": "2026-09-01",
        "end": "2026-09-02",
        "tick_size": TICK,
        "by": "range",
        param: value,
    }
    with pytest.raises(ValueError, match=message):
        indicators.volume_at_price(store, **kwargs)


def test_missing_tick_size_on_value_area_levels(tmp_path):
    store = TickStore(SYMBOL, root=tmp_path)
    bars = _bars([datetime(2026, 9, 1, 9)])
    with pytest.raises(ValueError, match="tick_size"):
        indicators.value_area_levels(bars, ticks=store, timeframe="M5")


def test_bar_profiles_use_timeframe_and_exclusive_range(tmp_path):
    nine = datetime(2026, 9, 2, 9)
    write_trade_ticks(
        tmp_path,
        SYMBOL,
        [
            (nine, 100.0, 10.0, BUY_FLAG),
            (nine + timedelta(minutes=4), 100.5, 5.0, SELL_FLAG),
            (nine + timedelta(minutes=5), 101.0, 7.0, BUY_FLAG),
            (nine + timedelta(minutes=10), 102.0, 9.0, BUY_FLAG),
        ],
    )
    profile = indicators.volume_at_price(
        TickStore(SYMBOL, root=tmp_path),
        start="2026-09-02T09:00",
        end="2026-09-02T09:10",
        tick_size=TICK,
        by="bar",
        timeframe="M5",
    )
    assert profile.segments["total"].tolist() == [15.0, 7.0]
    assert profile.segments.index.hour.tolist() == [9, 9]
    assert profile.segments.index.minute.tolist() == [0, 5]


def test_developing_levels_include_session_history_before_frame(tmp_path):
    write_trade_ticks(
        tmp_path,
        SYMBOL,
        [
            (datetime(2026, 9, 2, 9), 100.0, 100.0, BUY_FLAG),
            (datetime(2026, 9, 2, 10), 102.0, 1.0, BUY_FLAG),
            (datetime(2026, 9, 2, 10, 5), 103.0, 1000.0, BUY_FLAG),
        ],
    )
    store = TickStore(SYMBOL, root=tmp_path)
    frame = _bars([datetime(2026, 9, 2, 10)])
    levels = indicators.value_area_levels(frame, ticks=store, timeframe="M5", tick_size=TICK)
    assert levels["poc"].tolist() == [100.0]
    assert levels.index.equals(frame.index)
    # Even a window with no new ticks still has a developing session profile.
    later = indicators.value_area_levels(
        _bars([datetime(2026, 9, 2, 11)]),
        ticks=store,
        timeframe="M5",
        tick_size=TICK,
    )
    assert later["poc"].tolist() == [103.0]


def test_prior_session_uses_stored_predecessor_outside_frame(tmp_path):
    write_trade_ticks(
        tmp_path,
        SYMBOL,
        [
            (datetime(2026, 9, 1, 9), 100.0, 100.0, BUY_FLAG),
            (datetime(2026, 9, 3, 9), 102.0, 1.0, BUY_FLAG),
        ],
    )
    store = TickStore(SYMBOL, root=tmp_path)
    # Skip a missing calendar day; the current day need not contain stored ticks.
    for day in [2, 3]:
        frame = _bars([datetime(2026, 9, day, 10)])
        levels = indicators.value_area_levels(
            frame,
            ticks=store,
            timeframe="M5",
            tick_size=TICK,
            mode="prior_session",
        )
        assert levels["poc"].tolist() == [100.0]
        assert levels.attrs["unavailable_reason"] is None
