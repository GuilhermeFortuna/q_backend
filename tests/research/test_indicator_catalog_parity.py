"""The public research helpers cover the Market catalog with the same calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.indicator_catalog import INDICATOR_CATALOG, merge_params
from q_backend.research import indicators

MA_KINDS = {"sma", "ema", "wma", "hma", "smma"}
SERIES_HELPERS = {
    "rsi",
    "bollinger",
    "macd",
    "rolling_zscore",
    "rolling_rank",
    "pct_change",
    "log_return",
    "price_zscore_log",
    "ewma_trend_spread",
    "realized_vol",
    "per_bar_realized_vol",
    "per_bar_ewma_vol",
}


@pytest.fixture
def bars() -> pd.DataFrame:
    index = pd.date_range("2026-09-01 09:00", periods=350, freq="5min", tz="America/Sao_Paulo", name="time")
    close = 100 + np.cumsum(np.random.default_rng(42).normal(0, 0.2, len(index)))
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.4,
            "low": close - 0.5,
            "close": close,
            "volume": np.arange(len(index), dtype=float) + 100,
        },
        index=index,
    )


def public_result(entry, frame, params):
    if entry.id in MA_KINDS:
        data = frame["close"]
        result = indicators.ma(data, kind=entry.id, **params)
    else:
        data = frame["volume"] if entry.id == "volume_ma" else frame["close"] if entry.id in SERIES_HELPERS else frame
        result = getattr(indicators, entry.id)(data, **params)
    for output in result if isinstance(result, tuple) else (result,):
        pd.testing.assert_index_equal(output.index, data.index)
    return result


@pytest.mark.parametrize("entry", [e for e in INDICATOR_CATALOG if e.required_data == "bars"], ids=lambda e: e.id)
def test_catalog_parity_preserves_index_inputs_and_causality(entry, bars):
    original = bars.copy(deep=True)
    params = merge_params(entry, None)
    result = public_result(entry, bars, params)
    actual = result if isinstance(result, tuple) else (result,)
    expected = tuple(entry.compute(bars, params).values())
    for output, delegate in zip(actual, expected, strict=True):
        pd.testing.assert_series_equal(output, delegate)
        pd.testing.assert_index_equal(output.index, bars.index)
    pd.testing.assert_frame_equal(bars, original)
    cutoff = 260
    prefix = public_result(entry, bars.iloc[:cutoff], params)
    prefix_outputs = prefix if isinstance(prefix, tuple) else (prefix,)
    for full, part in zip(actual, prefix_outputs, strict=True):
        pd.testing.assert_series_equal(full.iloc[:cutoff], part)


@pytest.mark.parametrize("entry", [e for e in INDICATOR_CATALOG if e.required_data == "bars"], ids=lambda e: e.id)
def test_empty_inputs_keep_the_exact_index(entry, bars):
    empty = bars.iloc[:0]
    result = public_result(entry, empty, merge_params(entry, None))
    for output in result if isinstance(result, tuple) else (result,):
        assert output.empty
        pd.testing.assert_index_equal(output.index, empty.index)


def test_every_catalog_id_has_a_public_research_helper():
    for entry in INDICATOR_CATALOG:
        name = "ma" if entry.id in MA_KINDS else entry.id
        assert name in indicators.__all__, entry.id
        assert callable(getattr(indicators, name)), entry.id


def test_transform_units_and_tie_behavior():
    close = pd.Series([10.0, 12.0, 11.0, 11.0], name="close")
    assert indicators.rolling_rank(close, window=4).iloc[-1] == 0.75
    assert indicators.rolling_rank(pd.Series([2.0] * 4), window=4).iloc[-1] == 1.0
    change = indicators.pct_change(pd.Series([100.0, 105.0]), change_bars=1)
    assert np.isnan(change.iloc[0])
    assert change.iloc[1] == pytest.approx(0.05)
    assert indicators.log_return(pd.Series([100.0, 105.0]), horizon=1).iloc[1] == pytest.approx(np.log(1.05))


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("rolling_rank", {"window": True}),
        ("rolling_zscore", {"window": 1.5}),
        ("pct_change", {"change_bars": 0}),
        ("log_return", {"horizon": np.bool_(True)}),
        ("price_zscore_log", {"window": 1}),
        ("per_bar_realized_vol", {"window": -1}),
        ("per_bar_ewma_vol", {"half_life": float("inf")}),
        ("ewma_trend_spread", {"fast_half_life": 32, "slow_half_life": 8}),
    ],
)
def test_series_parameter_validation(name, kwargs, bars):
    with pytest.raises((TypeError, ValueError)):
        getattr(indicators, name)(bars["close"], **kwargs)


@pytest.mark.parametrize("estimator", ["realized", "ewma", "rogers_satchell", "yang_zhang"])
@pytest.mark.parametrize("name", ["normalized_momentum", "normalized_ewma_trend_spread", "volatility_log_ratio"])
def test_composite_estimators_match_the_catalog(name, estimator, bars):
    entry = next(e for e in INDICATOR_CATALOG if e.id == name)
    params = merge_params(entry, {"vol_estimator": estimator})
    result = getattr(indicators, name)(bars, **params)
    pd.testing.assert_series_equal(result, entry.compute(bars, params)["line"])


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("normalized_momentum", {"horizon": False}),
        ("normalized_momentum", {"vol_estimator": "invalid"}),
        ("normalized_momentum", {"vol_lag": True}),
        ("normalized_momentum", {"vol_lag": 2}),
        ("normalized_momentum", {"vol_estimator": "yang_zhang", "vol_window": 1}),
        ("normalized_ewma_trend_spread", {"fast_half_life": 32, "slow_half_life": 8}),
        ("volatility_log_ratio", {"short_window": 40, "long_window": 20}),
        ("volatility_log_ratio", {"vol_estimator": "ewma", "short_half_life": 32, "long_half_life": 8}),
        ("volatility_log_ratio", {"vol_estimator": "ewma", "short_half_life": True}),
        ("rogers_satchell", {"window": 0}),
        ("per_bar_yang_zhang", {"window": 1}),
        ("atr_fraction", {"period": 0}),
    ],
)
def test_frame_parameter_validation(name, kwargs, bars):
    with pytest.raises((TypeError, ValueError)):
        getattr(indicators, name)(bars, **kwargs)


@pytest.mark.parametrize(
    "name,kwargs", [("normalized_momentum", {}), ("atr_fraction", {"period": 14}), ("rogers_satchell", {"window": 20})]
)
def test_frame_helpers_reject_missing_or_infinite_data(name, kwargs, bars):
    with pytest.raises(ValueError, match="missing"):
        getattr(indicators, name)(bars.drop(columns="high"), **kwargs)
    bad = bars.copy()
    bad.iloc[0, bad.columns.get_loc("close")] = np.inf
    with pytest.raises(ValueError, match="infinite"):
        getattr(indicators, name)(bad, **kwargs)


def test_window_and_half_life_parameters_unused_by_the_estimator_are_ignored(bars):
    expected = indicators.normalized_momentum(bars, vol_estimator="ewma")
    actual = indicators.normalized_momentum(bars, vol_estimator=" EWMA ", vol_window=None)
    pd.testing.assert_series_equal(actual, expected)
    expected = indicators.volatility_log_ratio(bars, vol_estimator="realized")
    actual = indicators.volatility_log_ratio(bars, short_half_life=None, long_half_life=None)
    pd.testing.assert_series_equal(actual, expected)


@pytest.mark.parametrize(
    "kwargs,param",
    [({"short_window": 0}, "short_window"), ({"vol_estimator": "ewma", "short_half_life": 0}, "short_half_life")],
)
def test_ratio_validation_names_the_public_parameter(bars, kwargs, param):
    with pytest.raises(ValueError, match=param):
        indicators.volatility_log_ratio(bars, **kwargs)


@pytest.mark.parametrize("entry", [e for e in INDICATOR_CATALOG if e.required_data == "bars"], ids=lambda e: e.id)
@pytest.mark.parametrize("nullable", [False, True], ids=["float64", "nullable-Float64"])
def test_interior_missing_values_match_the_backend_without_filling(entry, nullable, bars):
    data = bars.astype("Float64") if nullable else bars.copy()
    data.iloc[120, :] = pd.NA if nullable else np.nan
    original = data.copy(deep=True)
    params = merge_params(entry, None)
    result = public_result(entry, data, params)
    actual = result if isinstance(result, tuple) else (result,)
    expected = tuple(entry.compute(data, params).values())
    for output, delegate in zip(actual, expected, strict=True):
        pd.testing.assert_series_equal(output, delegate)
    pd.testing.assert_frame_equal(data, original)


@pytest.mark.parametrize("name,kwargs", [("log_return", {}), ("rolling_rank", {"window": 3}), ("per_bar_ewma_vol", {})])
def test_series_helpers_reject_complex_prices(name, kwargs):
    close = pd.Series([100 + 100j, 110 + 500j])
    with pytest.raises(TypeError, match="complex"):
        getattr(indicators, name)(close, **kwargs)


def test_frame_helpers_reject_complex_prices(bars):
    bad = bars.astype({"close": "complex128"})
    with pytest.raises(TypeError, match="complex"):
        indicators.normalized_momentum(bad)


def test_true_range_first_bar_is_undefined():
    frame = pd.DataFrame({"high": [103.0, 104.0], "low": [99.0, 100.0], "close": [100.0, 101.0]})
    result = indicators.true_range(frame)
    assert np.isnan(result.iloc[0])
    assert result.iloc[1] == 4.0
