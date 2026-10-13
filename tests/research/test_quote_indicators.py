"""Public liquidity helpers read stored quotes with the Market aggregation rules."""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.quote_spread import quote_spread as aggregate_quotes
from q_backend.market_data.quote_spread import spread_vs_volatility as normalize_spread
from q_backend.research import indicators
from q_backend.research.tick_store import TickStore
from quote_ticks import TRADE_FLAGS, write_quote_ticks

SYMBOL = "WDO$"


def frame_at(times):
    index = pd.DatetimeIndex(times, name="time").tz_localize("America/Sao_Paulo")
    close = 100.0 + np.arange(len(index)) * 0.1
    return pd.DataFrame({"open": close - 0.01, "high": close + 0.2, "low": close - 0.2, "close": close}, index=index)


@pytest.mark.parametrize("output,want", [("time_weighted_mean", 30.0), ("last", 40.0), ("max", 40.0)])
def test_aggregation_and_basis_point_units(tmp_path, output, want):
    nine = datetime(2026, 9, 1, 9)
    write_quote_ticks(tmp_path, SYMBOL, [(nine, 99.9, 100.1), (nine + timedelta(seconds=30), 99.8, 100.2)])
    store = TickStore(SYMBOL, root=tmp_path)
    bars = frame_at([nine])
    bars.attrs["q_research"] = {"symbol": SYMBOL, "timeframe": "M1"}
    original = bars.copy(deep=True)
    result = indicators.quote_spread(bars, ticks=store, timeframe=" m1 ", output=output)
    assert result.iloc[0] == pytest.approx(want)
    assert result.index is bars.index
    assert result.attrs["unavailable_reason"] is None
    pd.testing.assert_frame_equal(bars, original)
    assert bars.attrs == original.attrs


def test_quote_age_coverage_and_utc_index_are_respected(tmp_path):
    nine = datetime(2026, 9, 1, 9)
    write_quote_ticks(tmp_path, SYMBOL, [(nine, 99.9, 100.1)])
    store = TickStore(SYMBOL, root=tmp_path)
    bars = frame_at([nine, nine + timedelta(minutes=1)])
    bars.index = bars.index.tz_convert("UTC")
    result = indicators.quote_spread(bars, ticks=store, timeframe="M1", max_quote_age_ms=30_000, min_coverage=0.5)
    assert result.index is bars.index
    assert result.iloc[0] == pytest.approx(20.0)
    assert np.isnan(result.iloc[1])
    stricter = indicators.quote_spread(bars, ticks=store, timeframe="M1", max_quote_age_ms=30_000, min_coverage=0.51)
    assert stricter.isna().all()
    zero_age = indicators.quote_spread(bars, ticks=store, timeframe="M1", max_quote_age_ms=0)
    assert zero_age.isna().all()


def test_missing_sessions_have_gaps_and_do_not_carry_overnight(tmp_path):
    late = datetime(2026, 9, 1, 23, 59, 30)
    midnight = datetime(2026, 9, 2)
    absent = datetime(2026, 9, 3)
    write_quote_ticks(
        tmp_path, SYMBOL, [(late, 99.9, 100.1), (midnight + timedelta(seconds=30), 99.9, 100.1, TRADE_FLAGS)]
    )
    store = TickStore(SYMBOL, root=tmp_path)
    result = indicators.quote_spread(
        frame_at([midnight, absent]), ticks=store, timeframe="M1", max_quote_age_ms=3_600_000
    )
    assert result.isna().all()
    assert result.attrs["unavailable_reason"] is None  # A session exists, but contains no usable quotes.
    unavailable = indicators.quote_spread(frame_at([absent]), ticks=store, timeframe="M1")
    assert unavailable.isna().all()
    assert "No ticks" in unavailable.attrs["unavailable_reason"]
    assert "2026-09-03" in unavailable.attrs["unavailable_reason"]


@pytest.mark.parametrize("estimator", ["realized", "ewma", "rogers_satchell", "yang_zhang"])
def test_spread_ratio_is_dimensionless_and_matches_the_shared_service(tmp_path, estimator):
    nine = datetime(2026, 9, 1, 9)
    times = [nine + timedelta(minutes=i) for i in range(30)]
    write_quote_ticks(tmp_path, SYMBOL, [(time, 99.9, 100.1) for time in times])
    store = TickStore(SYMBOL, root=tmp_path)
    bars = frame_at(times)
    original = bars.copy(deep=True)
    quote = aggregate_quotes(store, SYMBOL, bars.index, "M1", max_quote_age_ms=60_000)
    expected = normalize_spread(
        bars, quote.select("time_weighted_mean", 0), vol_estimator=estimator, vol_window=3, vol_half_life=1.0, vol_lag=1
    )
    result = indicators.spread_vs_volatility(
        bars, ticks=store, timeframe="M1", vol_estimator=estimator, vol_window=3, vol_half_life=1.0
    )
    pd.testing.assert_series_equal(result, expected)
    assert result.index is bars.index
    assert np.isfinite(result.iloc[-1])
    pd.testing.assert_frame_equal(bars, original)
    # A prefix must not see the later quotes or bars.
    part = indicators.spread_vs_volatility(
        bars.iloc[:25], ticks=store, timeframe="M1", vol_estimator=estimator, vol_window=3, vol_half_life=1.0
    )
    pd.testing.assert_series_equal(result.iloc[:25], part)


@pytest.mark.parametrize("name", ["quote_spread", "spread_vs_volatility"])
def test_empty_and_unavailable_frames_keep_the_index_without_sync(tmp_path, name, monkeypatch):
    store = TickStore(SYMBOL, root=tmp_path)

    def reject_sync(*args, **kwargs):
        pytest.fail("Liquidity indicator must not sync or fetch ticks")

    monkeypatch.setattr(store, "sync", reject_sync)
    bars = frame_at([datetime(2026, 9, 1, 9)])
    empty = bars.iloc[:0]
    result = getattr(indicators, name)(empty, ticks=store, timeframe="M1")
    assert result.empty and result.index is empty.index
    assert result.attrs["unavailable_reason"] is None
    result = getattr(indicators, name)(bars, ticks=store, timeframe="M1")
    assert result.isna().all()
    assert result.attrs["unavailable_reason"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"output": "median"},
        {"max_quote_age_ms": True},
        {"max_quote_age_ms": -1},
        {"min_coverage": True},
        {"min_coverage": -0.1},
        {"min_coverage": 1.1},
        {"min_coverage": float("nan")},
        {"timeframe": "MN1"},
        {"ticks": None},
    ],
)
def test_invalid_quote_parameters_fail_even_on_empty_input(tmp_path, kwargs):
    bars = frame_at([])
    args = {"ticks": TickStore(SYMBOL, root=tmp_path), "timeframe": "M1", **kwargs}
    with pytest.raises((TypeError, ValueError)):
        indicators.quote_spread(bars, **args)


@pytest.mark.parametrize("metadata", [{"symbol": "WIN$"}, {"timeframe": "M5"}])
def test_conflicting_frame_metadata_is_rejected(tmp_path, metadata):
    bars = frame_at([datetime(2026, 9, 1, 9)])
    bars.attrs["q_research"] = metadata
    with pytest.raises(ValueError, match="metadata"):
        indicators.quote_spread(bars, ticks=TickStore(SYMBOL, root=tmp_path), timeframe="M1")


@pytest.mark.parametrize(
    "index",
    [
        pd.Index([0]),
        pd.DatetimeIndex([pd.NaT]),
        pd.to_datetime(["2026-09-01", "2026-09-01"]),
        pd.to_datetime(["2026-09-02", "2026-09-01"]),
    ],
)
def test_invalid_bar_timestamps_fail_before_aggregation(tmp_path, index):
    bars = pd.DataFrame(index=index)
    with pytest.raises((TypeError, ValueError)):
        indicators.quote_spread(bars, ticks=TickStore(SYMBOL, root=tmp_path), timeframe="M1")


def test_overlapping_quote_bars_are_rejected(tmp_path):
    bars = frame_at([datetime(2026, 9, 1, 9), datetime(2026, 9, 1, 9, 0, 30)])
    with pytest.raises(ValueError, match="overlap"):
        indicators.quote_spread(bars, ticks=TickStore(SYMBOL, root=tmp_path), timeframe="M1")
