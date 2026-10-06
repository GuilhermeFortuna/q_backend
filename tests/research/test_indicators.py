"""Tests for q_backend.research.indicators helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.moving_averages import compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import indicators


@pytest.fixture
def sample_ohlcv() -> pd.DataFrame:
    # 30 bars of synthetic data
    idx = pd.date_range("2026-09-01 09:00", periods=30, freq="5min", tz=BRASILIA_TZ, name="time")
    # deterministic values with slight trend and variation
    open_p = np.linspace(100.0, 115.0, 30)
    high_p = open_p + 1.5
    low_p = open_p - 1.0
    close_p = open_p + 0.5
    return pd.DataFrame(
        {
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
        },
        index=idx,
    )


def test_parity_and_non_mutation(sample_ohlcv: pd.DataFrame) -> None:
    """Test all 8 functions against their delegates for parity, index preservation, and non-mutation."""
    df = sample_ohlcv.copy()
    orig_df = sample_ohlcv.copy()
    close = df["close"].copy()
    orig_close = df["close"].copy()

    # 1. ma (sma, ema, wma, smma, hma)
    for kind in ("sma", "ema", "wma", "smma", "hma"):
        expected = compute_ma(close, period=10, ma_type=kind)
        res = indicators.ma(close, period=10, kind=kind)
        pd.testing.assert_series_equal(res, expected)
        assert res.index is close.index
        pd.testing.assert_series_equal(close, orig_close)

    # 2. rsi
    expected_rsi = compute_rsi(close, period=14)
    res_rsi = indicators.rsi(close, period=14)
    pd.testing.assert_series_equal(res_rsi, expected_rsi)
    assert res_rsi.index is close.index
    pd.testing.assert_series_equal(close, orig_close)

    # 3. atr
    expected_atr = compute_atr(df["high"], df["low"], df["close"], period=14)
    res_atr = indicators.atr(df, period=14)
    pd.testing.assert_series_equal(res_atr, expected_atr)
    assert res_atr.index is df.index
    pd.testing.assert_frame_equal(df, orig_df)

    # 4. bollinger (upper, middle, lower)
    exp_u, exp_m, exp_l = compute_bollinger_bands(close, period=20, num_std=2.0)
    res_u, res_m, res_l = indicators.bollinger(close, period=20, num_std=2.0)
    pd.testing.assert_series_equal(res_u, exp_u)
    pd.testing.assert_series_equal(res_m, exp_m)
    pd.testing.assert_series_equal(res_l, exp_l)
    assert res_u.index is close.index
    assert res_m.index is close.index
    assert res_l.index is close.index
    pd.testing.assert_series_equal(close, orig_close)

    # 5. macd (line, signal, histogram)
    exp_line, exp_sig, exp_hist = compute_macd(close, 12, 26, 9)
    res_line, res_sig, res_hist = indicators.macd(close, fast_period=12, slow_period=26, signal_period=9)
    pd.testing.assert_series_equal(res_line, exp_line)
    pd.testing.assert_series_equal(res_sig, exp_sig)
    pd.testing.assert_series_equal(res_hist, exp_hist)
    assert res_line.index is close.index
    pd.testing.assert_series_equal(close, orig_close)

    # 6. donchian (upper, lower)
    exp_dh, exp_dl = compute_donchian_channels(df["high"], df["low"], period=10)
    res_dh, res_dl = indicators.donchian(df, period=10)
    pd.testing.assert_series_equal(res_dh, exp_dh)
    pd.testing.assert_series_equal(res_dl, exp_dl)
    assert res_dh.index is df.index
    pd.testing.assert_frame_equal(df, orig_df)

    # 7. realized_vol
    exp_rvol = compute_realized_vol(close, window=10, periods_per_year=252)
    res_rvol = indicators.realized_vol(close, window=10, periods_per_year=252)
    pd.testing.assert_series_equal(res_rvol, exp_rvol)
    assert res_rvol.index is close.index
    pd.testing.assert_series_equal(close, orig_close)

    # 8. yang_zhang
    exp_yz = compute_yang_zhang(df["open"], df["high"], df["low"], df["close"], window=10, periods_per_year=252)
    res_yz = indicators.yang_zhang(df, window=10, periods_per_year=252)
    pd.testing.assert_series_equal(res_yz, exp_yz)
    assert res_yz.index is df.index
    pd.testing.assert_frame_equal(df, orig_df)


def test_empty_inputs_preserve_index_and_tz() -> None:
    tz_idx = pd.DatetimeIndex([], tz=BRASILIA_TZ, name="time")
    empty_df = pd.DataFrame(
        {
            "open": pd.Series([], dtype=float),
            "high": pd.Series([], dtype=float),
            "low": pd.Series([], dtype=float),
            "close": pd.Series([], dtype=float),
        },
        index=tz_idx,
    )
    empty_close = empty_df["close"]

    # ma
    res_ma = indicators.ma(empty_close, 10)
    assert res_ma.empty
    assert res_ma.index.equals(tz_idx)
    assert str(res_ma.index.tz) == "America/Sao_Paulo"

    # rsi
    res_rsi = indicators.rsi(empty_close, 14)
    assert res_rsi.empty
    assert res_rsi.index.equals(tz_idx)

    # atr
    res_atr = indicators.atr(empty_df, 14)
    assert res_atr.empty
    assert res_atr.index.equals(tz_idx)

    # bollinger
    u, m, l = indicators.bollinger(empty_close, 20)
    assert u.empty and m.empty and l.empty
    assert u.index.equals(tz_idx)

    # macd
    line, sig, hist = indicators.macd(empty_close)
    assert line.empty and sig.empty and hist.empty
    assert line.index.equals(tz_idx)

    # donchian
    dh, dl = indicators.donchian(empty_df, 10)
    assert dh.empty and dl.empty
    assert dh.index.equals(tz_idx)

    # realized_vol
    res_rv = indicators.realized_vol(empty_close, 10)
    assert res_rv.empty
    assert res_rv.index.equals(tz_idx)

    # yang_zhang
    res_yz = indicators.yang_zhang(empty_df, 10)
    assert res_yz.empty
    assert res_yz.index.equals(tz_idx)


def test_inputs_with_nans_permitted(sample_ohlcv: pd.DataFrame) -> None:
    df = sample_ohlcv.copy()
    df.loc[df.index[0], "close"] = np.nan
    res = indicators.ma(df["close"], period=5)
    assert pd.isna(res.iloc[0])


@pytest.mark.parametrize(
    ("fn", "args", "kwargs", "exc_type", "match"),
    [
        # Unknown MA kind
        (
            indicators.ma,
            (pd.Series([1.0, 2.0]), 5),
            {"kind": "unsupported"},
            ValueError,
            "Invalid MA kind 'unsupported'",
        ),
        (indicators.ma, (pd.Series([1.0, 2.0]), 5), {"kind": 123}, TypeError, "'kind' must be a string"),
        # Bool as period/window
        (indicators.ma, (pd.Series([1.0, 2.0]), True), {}, TypeError, "'period' must be an integer"),
        (indicators.rsi, (pd.Series([1.0, 2.0]), False), {}, TypeError, "'period' must be an integer"),
        (
            indicators.atr,
            (pd.DataFrame({"high": [2.0], "low": [1.0], "close": [1.5]}), True),
            {},
            TypeError,
            "'period' must be an integer",
        ),
        (indicators.realized_vol, (pd.Series([1.0, 2.0]), True), {}, TypeError, "'window' must be an integer"),
        (
            indicators.yang_zhang,
            (pd.DataFrame({"open": [1.0], "high": [2.0], "low": [1.0], "close": [1.5]}), True),
            {},
            TypeError,
            "'window' must be an integer",
        ),
        # Fractional period/window
        (indicators.ma, (pd.Series([1.0, 2.0]), 5.5), {}, TypeError, "'period' must be an integer"),
        (indicators.bollinger, (pd.Series([1.0, 2.0]), 5.5), {}, TypeError, "'period' must be an integer"),
        (indicators.realized_vol, (pd.Series([1.0, 2.0]), 10.2), {}, TypeError, "'window' must be an integer"),
        # Nonpositive periods/windows
        (indicators.ma, (pd.Series([1.0, 2.0]), 0), {}, ValueError, "'period' must be an integer >= 1"),
        (indicators.ma, (pd.Series([1.0, 2.0]), -5), {}, ValueError, "'period' must be an integer >= 1"),
        (indicators.rsi, (pd.Series([1.0, 2.0]), 0), {}, ValueError, "'period' must be an integer >= 1"),
        (indicators.macd, (pd.Series([1.0, 2.0]), 0, 26, 9), {}, ValueError, "'fast_period' must be an integer >= 1"),
        (indicators.macd, (pd.Series([1.0, 2.0]), 12, 0, 9), {}, ValueError, "'slow_period' must be an integer >= 1"),
        (
            indicators.macd,
            (pd.Series([1.0, 2.0]), 12, 26, 0),
            {},
            ValueError,
            "'signal_period' must be an integer >= 1",
        ),
        (indicators.realized_vol, (pd.Series([1.0, 2.0]), 0), {}, ValueError, "'window' must be an integer >= 1"),
        # Yang-Zhang window < 2 (specifically window=1)
        (
            indicators.yang_zhang,
            (pd.DataFrame({"open": [1.0], "high": [2.0], "low": [1.0], "close": [1.5]}), 1),
            {},
            ValueError,
            "'window' must be an integer >= 2",
        ),
        # Infinity in series / dataframe
        (indicators.ma, (pd.Series([1.0, np.inf]), 2), {}, ValueError, "'close' contains infinite values"),
        (indicators.ma, (pd.Series([1.0, -np.inf]), 2), {}, ValueError, "'close' contains infinite values"),
        (
            indicators.atr,
            (pd.DataFrame({"high": [np.inf], "low": [1.0], "close": [1.5]}), 2),
            {},
            ValueError,
            "Column 'high' in 'frame' contains infinite values",
        ),
        # Nonnumeric Series / dataframe
        (indicators.ma, (pd.Series(["1.0", "2.0"]), 2), {}, TypeError, "'close' must have a numeric dtype"),
        (
            indicators.atr,
            (pd.DataFrame({"high": ["a"], "low": [1.0], "close": [1.5]}), 2),
            {},
            TypeError,
            "Column 'high' in 'frame' must have a numeric dtype",
        ),
        # Invalid input type (not Series / DataFrame)
        (indicators.ma, ([1.0, 2.0], 2), {}, TypeError, "'close' must be a pandas Series"),
        (indicators.atr, ({"high": [1.0]}, 2), {}, TypeError, "'frame' must be a pandas DataFrame"),
        # Missing OHLC columns
        (
            indicators.atr,
            (pd.DataFrame({"high": [2.0], "close": [1.5]}), 2),
            {},
            ValueError,
            "'frame' is missing required columns: \\['low'\\]",
        ),
        (
            indicators.donchian,
            (pd.DataFrame({"open": [2.0], "close": [1.5]}), 2),
            {},
            ValueError,
            "'frame' is missing required columns: \\['high', 'low'\\]",
        ),
        (
            indicators.yang_zhang,
            (pd.DataFrame({"open": [2.0], "close": [1.5]}), 2),
            {},
            ValueError,
            "'frame' is missing required columns: \\['high', 'low'\\]",
        ),
        # Invalid num_std
        (
            indicators.bollinger,
            (pd.Series([1.0, 2.0]), 20),
            {"num_std": 0},
            ValueError,
            "'num_std' must be a finite number > 0",
        ),
        (
            indicators.bollinger,
            (pd.Series([1.0, 2.0]), 20),
            {"num_std": -1.5},
            ValueError,
            "'num_std' must be a finite number > 0",
        ),
        (
            indicators.bollinger,
            (pd.Series([1.0, 2.0]), 20),
            {"num_std": np.inf},
            ValueError,
            "'num_std' must be a finite number > 0",
        ),
        (indicators.bollinger, (pd.Series([1.0, 2.0]), 20), {"num_std": True}, TypeError, "'num_std' must be a number"),
        (
            indicators.bollinger,
            (pd.Series([1.0, 2.0]), 20),
            {"num_std": "2.0"},
            TypeError,
            "'num_std' must be a number",
        ),
        # Invalid periods_per_year
        (
            indicators.realized_vol,
            (pd.Series([1.0, 2.0]), 5),
            {"periods_per_year": 0},
            ValueError,
            "'periods_per_year' must be an integer >= 1",
        ),
        (
            indicators.realized_vol,
            (pd.Series([1.0, 2.0]), 5),
            {"periods_per_year": -252},
            ValueError,
            "'periods_per_year' must be an integer >= 1",
        ),
        (
            indicators.realized_vol,
            (pd.Series([1.0, 2.0]), 5),
            {"periods_per_year": 252.5},
            TypeError,
            "'periods_per_year' must be an integer",
        ),
        (
            indicators.yang_zhang,
            (pd.DataFrame({"open": [1.0], "high": [2.0], "low": [1.0], "close": [1.5]}), 5),
            {"periods_per_year": 0},
            ValueError,
            "'periods_per_year' must be an integer >= 1",
        ),
    ],
)
def test_invalid_input_validation(fn, args, kwargs, exc_type, match) -> None:
    with pytest.raises(exc_type, match=match):
        fn(*args, **kwargs)


def test_volume_column_not_required_for_frame_indicators(sample_ohlcv: pd.DataFrame) -> None:
    # sample_ohlcv only has open, high, low, close - no volume column
    assert "tick_volume" not in sample_ohlcv.columns
    assert "real_volume" not in sample_ohlcv.columns
    # These must succeed without volume
    indicators.atr(sample_ohlcv, period=5)
    indicators.donchian(sample_ohlcv, period=5)
    indicators.yang_zhang(sample_ohlcv, window=5)
