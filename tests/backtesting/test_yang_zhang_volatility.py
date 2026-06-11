import math

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.technical_indicators import (
    compute_realized_vol,
    compute_yang_zhang,
)


def _reference_yang_zhang(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    window: int,
    periods_per_year: int = 252,
) -> np.ndarray:
    n = len(close)
    out = np.full(n, np.nan)
    open_close = np.log(close / open_)
    u = np.log(high / open_)
    d = np.log(low / open_)
    c = np.log(close / open_)
    rs = u * (u - c) + d * (d - c)
    k = 0.34 / (1.34 + (window + 1) / (window - 1))

    for i in range(window, n):
        o_slice = slice(i - window + 1, i + 1)
        overnight = np.log(open_[o_slice] / close[o_slice.start - 1 : i])
        var_o = np.var(overnight, ddof=1)
        var_oc = np.var(open_close[o_slice], ddof=1)
        mean_rs = np.mean(rs[o_slice])
        yz_var = var_o + k * var_oc + (1.0 - k) * mean_rs
        out[i] = math.sqrt(max(yz_var, 0.0) * periods_per_year)
    return out


def test_yang_zhang_matches_numpy_reference():
    rng = np.random.default_rng(42)
    n = 80
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    open_ = close - rng.normal(0, 0.2, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.8, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.8, n)
    window = 10

    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    yz = compute_yang_zhang(
        pd.Series(open_, index=idx),
        pd.Series(high, index=idx),
        pd.Series(low, index=idx),
        pd.Series(close, index=idx),
        window,
    )
    ref = _reference_yang_zhang(open_, high, low, close, window)

    valid = ~np.isnan(ref)
    np.testing.assert_allclose(yz.to_numpy()[valid], ref[valid], rtol=1e-9, atol=1e-9)
    assert yz.iloc[:window].isna().all()


def test_yang_zhang_constant_price_is_zero():
    n = 40
    price = 50.0
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    s = pd.Series(price, index=idx)
    yz = compute_yang_zhang(s, s, s, s, window=10)
    assert (yz.iloc[10:].fillna(-1) == 0.0).all()


def test_yang_zhang_is_causal():
    n = 60
    rng = np.random.default_rng(7)
    close = 100.0 + np.cumsum(rng.normal(0, 0.4, n))
    open_ = close - 0.1
    high = close + 0.5
    low = close - 0.5
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    window = 12

    full = compute_yang_zhang(
        pd.Series(open_, index=idx),
        pd.Series(high, index=idx),
        pd.Series(low, index=idx),
        pd.Series(close, index=idx),
        window,
    )

    mutated_close = close.copy()
    mutated_close[50:] += 50.0
    prefix = compute_yang_zhang(
        pd.Series(open_[:50], index=idx[:50]),
        pd.Series(high[:50], index=idx[:50]),
        pd.Series(low[:50], index=idx[:50]),
        pd.Series(mutated_close[:50], index=idx[:50]),
        window,
    )

    pd.testing.assert_series_equal(full.iloc[:50], prefix, check_names=False)


def test_realized_vol_annualizes_log_return_std():
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    close = pd.Series(np.linspace(100, 130, 30), index=idx)
    vol = compute_realized_vol(close, window=5)
    log_ret = np.log(close / close.shift(1))
    expected = log_ret.rolling(5, min_periods=5).std() * np.sqrt(252)
    pd.testing.assert_series_equal(vol, expected, check_names=False)


@pytest.mark.parametrize("bad_vol", [None, float("nan"), 0.0])
def test_realized_vol_first_window_is_nan(bad_vol):
    del bad_vol
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    close = pd.Series(np.arange(10, 20, dtype=float), index=idx)
    vol = compute_realized_vol(close, window=5)
    assert vol.iloc[:4].isna().all()
