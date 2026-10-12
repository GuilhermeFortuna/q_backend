"""Quoted spread aggregation from the tick store: clock, bar bounds and sessions."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.quote_spread import bar_bounds_msc, open_tick_store, quote_spread
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.tick_store import TickStore, naive_local_to_time_msc
from quote_ticks import TRADE_FLAGS, write_quote_ticks

SYMBOL = "WDO$"


def _spread(store: TickStore | None, times: list[datetime], timeframe: str = "M10", max_age_ms: int = 60_000):
    return quote_spread(store, SYMBOL, pd.Series(pd.to_datetime(times)), timeframe, max_quote_age_ms=max_age_ms)


def test_bar_bounds_use_the_tick_clock_and_the_timeframe_duration():
    times = pd.Series(pd.to_datetime(["2024-01-02 09:00", "2024-01-02 09:10", "2024-01-02 17:50", "2024-01-03 09:00"]))
    opens, closes = bar_bounds_msc(times, "M10")
    assert opens.tolist() == [naive_local_to_time_msc(ts.to_pydatetime()) for ts in times]
    # Every bar lasts its timeframe, whatever follows it: an intraday gap or the next day.
    assert (closes - opens).tolist() == [600_000] * 4


def test_bar_bounds_convert_aware_times_to_brasilia():
    aware = pd.Series(pd.to_datetime(["2024-01-02 12:00"]).tz_localize("UTC"))
    naive = pd.Series(pd.to_datetime(["2024-01-02 09:00"]))
    assert aware.dt.tz_convert(BRASILIA_TZ).dt.hour.tolist() == [9]
    assert bar_bounds_msc(aware, "M10")[0].tolist() == bar_bounds_msc(naive, "M10")[0].tolist()


def test_bar_reads_the_ticks_of_its_own_wall_clock_time(tmp_path):
    nine = datetime(2024, 1, 2, 9, 0)
    noon = datetime(2024, 1, 2, 12, 0)
    write_quote_ticks(tmp_path, SYMBOL, [(nine, 99.95, 100.05), (noon, 99.0, 101.0)])
    result = _spread(TickStore(SYMBOL, root=tmp_path), [nine, noon])
    np.testing.assert_allclose(result.time_weighted_mean * 1e4, [10.0, 200.0])
    # One quote, carried for 60 s of a 600 s bar.
    np.testing.assert_allclose(result.coverage, [0.1, 0.1])


def test_quote_before_the_bar_carries_in_and_is_clipped_to_the_bar(tmp_path):
    bar = datetime(2024, 1, 2, 9, 10)
    write_quote_ticks(tmp_path, SYMBOL, [(bar - timedelta(seconds=30), 99.9, 100.1)])
    result = _spread(TickStore(SYMBOL, root=tmp_path), [bar], max_age_ms=120_000)
    # 90 s of the quote's life fall inside the 600 s bar.
    assert result.coverage[0] == pytest.approx(0.15)
    assert result.time_weighted_mean[0] == pytest.approx(0.002)


def test_last_bar_of_the_day_is_not_stretched_to_midnight(tmp_path):
    last_bar = datetime(2024, 1, 2, 17, 50)
    ticks = [(last_bar + timedelta(seconds=30 * k), 99.9, 100.1) for k in range(20)]
    after_hours = [(datetime(2024, 1, 2, 19, 0), 90.0, 110.0)]
    write_quote_ticks(tmp_path, SYMBOL, ticks + after_hours)
    result = _spread(TickStore(SYMBOL, root=tmp_path), [last_bar])
    assert result.coverage[0] == pytest.approx(1.0)
    assert result.max[0] == pytest.approx(0.002)


def test_each_day_is_aggregated_from_its_own_session_only(tmp_path):
    late = datetime(2024, 1, 2, 23, 59, 30)
    midnight_bar = datetime(2024, 1, 3, 0, 0)
    write_quote_ticks(
        tmp_path,
        SYMBOL,
        [(late, 99.9, 100.1), (midnight_bar + timedelta(minutes=5), 99.95, 100.05, TRADE_FLAGS)],
    )
    result = _spread(TickStore(SYMBOL, root=tmp_path), [midnight_bar], max_age_ms=3_600_000)
    assert result.coverage[0] == 0.0
    assert np.isnan(result.time_weighted_mean[0])
    assert result.unavailable_reason is None


def test_values_do_not_depend_on_where_the_bars_start(tmp_path):
    rng = np.random.default_rng(7)
    bars = [datetime(2024, 1, day, 9, 0) + timedelta(minutes=10 * k) for day in (2, 3, 4) for k in range(12)]
    ticks = []
    for bar in bars:
        for second in sorted(rng.integers(0, 600, size=4).tolist()):
            width = float(rng.uniform(0.01, 0.4))
            ticks.append((bar + timedelta(seconds=second), 100.0 - width, 100.0 + width))
    write_quote_ticks(tmp_path, SYMBOL, ticks)
    store = TickStore(SYMBOL, root=tmp_path)
    full = _spread(store, bars)
    assert np.isfinite(full.time_weighted_mean).all()
    for first in (5, 12, 30):
        part = _spread(store, bars[first:])
        np.testing.assert_array_equal(part.time_weighted_mean, full.time_weighted_mean[first:])
        np.testing.assert_array_equal(part.coverage, full.coverage[first:])
        np.testing.assert_array_equal(part.last, full.last[first:])


def test_select_applies_the_minimum_coverage(tmp_path):
    nine = datetime(2024, 1, 2, 9, 0)
    write_quote_ticks(tmp_path, SYMBOL, [(nine, 99.9, 100.1)])
    result = _spread(TickStore(SYMBOL, root=tmp_path), [nine])
    assert result.select("max", 0.1)[0] == pytest.approx(0.002)
    assert np.isnan(result.select("max", 0.2)[0])
    with pytest.raises(ValueError, match="Spread output"):
        result.select("median", 0.0)


def test_unavailable_when_no_bar_day_has_ticks(tmp_path):
    write_quote_ticks(tmp_path, SYMBOL, [(datetime(2024, 1, 2, 9, 0), 99.9, 100.1)])
    result = _spread(TickStore(SYMBOL, root=tmp_path), [datetime(2024, 1, 5, 9, 0)])
    assert "2024-01-05" in result.unavailable_reason
    assert np.isnan(result.last).all() and (result.coverage == 0.0).all()
    assert "not available" in _spread(None, [datetime(2024, 1, 5, 9, 0)]).unavailable_reason


def test_open_tick_store_resolves_the_environment_root(tmp_path, monkeypatch):
    write_quote_ticks(tmp_path, SYMBOL, [(datetime(2024, 1, 2, 9, 0), 99.9, 100.1)])
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    assert open_tick_store(SYMBOL).sessions() == [datetime(2024, 1, 2).date()]
    assert open_tick_store("  ") is None
