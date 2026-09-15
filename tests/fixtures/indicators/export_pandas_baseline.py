"""Export pandas indicator and transform baseline reference data.

Run at pre-change commit: 067e29cdf8db67d8b8c237599fced0b8eabb921f
Records full-precision values, series name, dtype, and provenance metadata.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.moving_averages import VALID_MA_TYPES, compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)
from q_backend.backtesting.transforms import (
    compute_clip,
    compute_pct_change,
    compute_rolling_rank,
    compute_rolling_zscore,
)


def synthetic_ohlcv(
    n: int = 400,
    *,
    seed: int = 20240609,
    freq: str = "h",
    start: str = "2023-01-02",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def serialize_series(s: pd.Series) -> dict[str, Any]:
    values: list[Any] = []
    for v in s:
        if pd.isna(v):
            values.append("nan")
        elif np.isposinf(v):
            values.append("inf")
        elif np.isneginf(v):
            values.append("-inf")
        elif isinstance(v, (int, np.integer)):
            values.append(int(v))
        else:
            values.append(float(v))
    return {
        "name": s.name,
        "dtype": str(s.dtype),
        "values": values,
    }


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # 1. synthetic_ohlcv (400 bars, seed 20240609)
    df_syn = synthetic_ohlcv(400, seed=20240609)
    c, o, h, l = df_syn["close"], df_syn["open"], df_syn["high"], df_syn["low"]

    # Two parameter sets per function (short and default/near-default)
    # Realized vol
    for win in (5, 20):
        res = compute_realized_vol(c, window=win, periods_per_year=252)
        cases.append(
            {
                "case_id": f"synthetic_realized_vol_w{win}",
                "function": "compute_realized_vol",
                "dataset": "synthetic_ohlcv",
                "params": {"window": win, "periods_per_year": 252},
                "outputs": [serialize_series(res)],
            }
        )

    # Yang-Zhang
    for win in (5, 20):
        res = compute_yang_zhang(o, h, l, c, window=win, periods_per_year=252)
        cases.append(
            {
                "case_id": f"synthetic_yang_zhang_w{win}",
                "function": "compute_yang_zhang",
                "dataset": "synthetic_ohlcv",
                "params": {"window": win, "periods_per_year": 252},
                "outputs": [serialize_series(res)],
            }
        )

    # RSI
    for p in (7, 14):
        res = compute_rsi(c, period=p)
        cases.append(
            {
                "case_id": f"synthetic_rsi_p{p}",
                "function": "compute_rsi",
                "dataset": "synthetic_ohlcv",
                "params": {"period": p},
                "outputs": [serialize_series(res)],
            }
        )

    # Bollinger Bands (upper, middle, lower)
    for p, num_std in ((10, 2.0), (20, 2.0)):
        u, m, lo = compute_bollinger_bands(c, period=p, num_std=num_std)
        cases.append(
            {
                "case_id": f"synthetic_bollinger_p{p}_std{num_std}",
                "function": "compute_bollinger_bands",
                "dataset": "synthetic_ohlcv",
                "params": {"period": p, "num_std": num_std},
                "outputs": [serialize_series(u), serialize_series(m), serialize_series(lo)],
            }
        )

    # MACD (macd_line, signal_line, histogram)
    for fast, slow, sig in ((6, 13, 4), (12, 26, 9)):
        ml, sl, hist = compute_macd(c, fast_period=fast, slow_period=slow, signal_period=sig)
        cases.append(
            {
                "case_id": f"synthetic_macd_{fast}_{slow}_{sig}",
                "function": "compute_macd",
                "dataset": "synthetic_ohlcv",
                "params": {"fast_period": fast, "slow_period": slow, "signal_period": sig},
                "outputs": [serialize_series(ml), serialize_series(sl), serialize_series(hist)],
            }
        )

    # Donchian (upper, lower)
    for p in (10, 20):
        u, lo = compute_donchian_channels(h, l, period=p)
        cases.append(
            {
                "case_id": f"synthetic_donchian_p{p}",
                "function": "compute_donchian_channels",
                "dataset": "synthetic_ohlcv",
                "params": {"period": p},
                "outputs": [serialize_series(u), serialize_series(lo)],
            }
        )

    # ATR
    for p in (7, 14):
        res = compute_atr(h, l, c, period=p)
        cases.append(
            {
                "case_id": f"synthetic_atr_p{p}",
                "function": "compute_atr",
                "dataset": "synthetic_ohlcv",
                "params": {"period": p},
                "outputs": [serialize_series(res)],
            }
        )

    # Moving averages (all 5 types)
    for ma_type in sorted(VALID_MA_TYPES):
        for p in (5, 20):
            res = compute_ma(c, period=p, ma_type=ma_type)
            cases.append(
                {
                    "case_id": f"synthetic_ma_{ma_type}_p{p}",
                    "function": "compute_ma",
                    "dataset": "synthetic_ohlcv",
                    "params": {"period": p, "ma_type": ma_type},
                    "outputs": [serialize_series(res)],
                }
            )

    # Rolling z-score
    for win in (5, 20):
        res = compute_rolling_zscore(c, window=win)
        cases.append(
            {
                "case_id": f"synthetic_rolling_zscore_w{win}",
                "function": "compute_rolling_zscore",
                "dataset": "synthetic_ohlcv",
                "params": {"window": win},
                "outputs": [serialize_series(res)],
            }
        )

    # Rolling rank
    for win in (5, 20):
        res = compute_rolling_rank(c, window=win)
        cases.append(
            {
                "case_id": f"synthetic_rolling_rank_w{win}",
                "function": "compute_rolling_rank",
                "dataset": "synthetic_ohlcv",
                "params": {"window": win},
                "outputs": [serialize_series(res)],
            }
        )

    # Percent change
    for cb in (1, 5):
        res = compute_pct_change(c, change_bars=cb)
        cases.append(
            {
                "case_id": f"synthetic_pct_change_lag{cb}",
                "function": "compute_pct_change",
                "dataset": "synthetic_ohlcv",
                "params": {"change_bars": cb},
                "outputs": [serialize_series(res)],
            }
        )

    # Clip
    for low_b, high_b in ((95.0, 105.0), (90.0, 110.0)):
        res = compute_clip(c, clip_low=low_b, clip_high=high_b)
        cases.append(
            {
                "case_id": f"synthetic_clip_{low_b}_{high_b}",
                "function": "compute_clip",
                "dataset": "synthetic_ohlcv",
                "params": {"clip_low": low_b, "clip_high": high_b},
                "outputs": [serialize_series(res)],
            }
        )

    # 2. Window larger than series (n=10, window/period=25)
    df_short = synthetic_ohlcv(10, seed=20240609)
    sc, so, sh, sl = df_short["close"], df_short["open"], df_short["high"], df_short["low"]

    cases.append(
        {
            "case_id": "large_window_realized_vol",
            "function": "compute_realized_vol",
            "dataset": "short_ohlcv",
            "params": {"window": 25, "periods_per_year": 252},
            "outputs": [serialize_series(compute_realized_vol(sc, window=25, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "large_window_yang_zhang",
            "function": "compute_yang_zhang",
            "dataset": "short_ohlcv",
            "params": {"window": 25, "periods_per_year": 252},
            "outputs": [serialize_series(compute_yang_zhang(so, sh, sl, sc, window=25, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "large_window_rsi",
            "function": "compute_rsi",
            "dataset": "short_ohlcv",
            "params": {"period": 25},
            "outputs": [serialize_series(compute_rsi(sc, period=25))],
        }
    )
    bu, bm, blo = compute_bollinger_bands(sc, period=25, num_std=2.0)
    cases.append(
        {
            "case_id": "large_window_bollinger",
            "function": "compute_bollinger_bands",
            "dataset": "short_ohlcv",
            "params": {"period": 25, "num_std": 2.0},
            "outputs": [serialize_series(bu), serialize_series(bm), serialize_series(blo)],
        }
    )
    ml, sl_line, hist = compute_macd(sc, fast_period=15, slow_period=25, signal_period=10)
    cases.append(
        {
            "case_id": "large_window_macd",
            "function": "compute_macd",
            "dataset": "short_ohlcv",
            "params": {"fast_period": 15, "slow_period": 25, "signal_period": 10},
            "outputs": [serialize_series(ml), serialize_series(sl_line), serialize_series(hist)],
        }
    )
    du, dlo = compute_donchian_channels(sh, sl, period=25)
    cases.append(
        {
            "case_id": "large_window_donchian",
            "function": "compute_donchian_channels",
            "dataset": "short_ohlcv",
            "params": {"period": 25},
            "outputs": [serialize_series(du), serialize_series(dlo)],
        }
    )
    cases.append(
        {
            "case_id": "large_window_atr",
            "function": "compute_atr",
            "dataset": "short_ohlcv",
            "params": {"period": 25},
            "outputs": [serialize_series(compute_atr(sh, sl, sc, period=25))],
        }
    )
    for ma_type in sorted(VALID_MA_TYPES):
        cases.append(
            {
                "case_id": f"large_window_ma_{ma_type}",
                "function": "compute_ma",
                "dataset": "short_ohlcv",
                "params": {"period": 25, "ma_type": ma_type},
                "outputs": [serialize_series(compute_ma(sc, period=25, ma_type=ma_type))],
            }
        )
    cases.append(
        {
            "case_id": "large_window_rolling_zscore",
            "function": "compute_rolling_zscore",
            "dataset": "short_ohlcv",
            "params": {"window": 25},
            "outputs": [serialize_series(compute_rolling_zscore(sc, window=25))],
        }
    )
    cases.append(
        {
            "case_id": "large_window_rolling_rank",
            "function": "compute_rolling_rank",
            "dataset": "short_ohlcv",
            "params": {"window": 25},
            "outputs": [serialize_series(compute_rolling_rank(sc, window=25))],
        }
    )
    cases.append(
        {
            "case_id": "large_window_pct_change",
            "function": "compute_pct_change",
            "dataset": "short_ohlcv",
            "params": {"change_bars": 25},
            "outputs": [serialize_series(compute_pct_change(sc, change_bars=25))],
        }
    )

    # 3. Series with NaN runs inside it (50 bars, rows 15..20 NaN)
    df_nan = synthetic_ohlcv(50, seed=20240609).copy()
    df_nan.iloc[15:20, :] = np.nan
    nc, no, nh, nl = df_nan["close"], df_nan["open"], df_nan["high"], df_nan["low"]

    cases.append(
        {
            "case_id": "nan_runs_realized_vol",
            "function": "compute_realized_vol",
            "dataset": "nan_ohlcv",
            "params": {"window": 5, "periods_per_year": 252},
            "outputs": [serialize_series(compute_realized_vol(nc, window=5, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "nan_runs_yang_zhang",
            "function": "compute_yang_zhang",
            "dataset": "nan_ohlcv",
            "params": {"window": 5, "periods_per_year": 252},
            "outputs": [serialize_series(compute_yang_zhang(no, nh, nl, nc, window=5, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "nan_runs_rsi",
            "function": "compute_rsi",
            "dataset": "nan_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(compute_rsi(nc, period=5))],
        }
    )
    nbu, nbm, nblo = compute_bollinger_bands(nc, period=5, num_std=2.0)
    cases.append(
        {
            "case_id": "nan_runs_bollinger",
            "function": "compute_bollinger_bands",
            "dataset": "nan_ohlcv",
            "params": {"period": 5, "num_std": 2.0},
            "outputs": [serialize_series(nbu), serialize_series(nbm), serialize_series(nblo)],
        }
    )
    nml, nsl, nhist = compute_macd(nc, fast_period=4, slow_period=8, signal_period=3)
    cases.append(
        {
            "case_id": "nan_runs_macd",
            "function": "compute_macd",
            "dataset": "nan_ohlcv",
            "params": {"fast_period": 4, "slow_period": 8, "signal_period": 3},
            "outputs": [serialize_series(nml), serialize_series(nsl), serialize_series(nhist)],
        }
    )
    ndu, ndlo = compute_donchian_channels(nh, nl, period=5)
    cases.append(
        {
            "case_id": "nan_runs_donchian",
            "function": "compute_donchian_channels",
            "dataset": "nan_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(ndu), serialize_series(ndlo)],
        }
    )
    cases.append(
        {
            "case_id": "nan_runs_atr",
            "function": "compute_atr",
            "dataset": "nan_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(compute_atr(nh, nl, nc, period=5))],
        }
    )
    for ma_type in sorted(VALID_MA_TYPES):
        cases.append(
            {
                "case_id": f"nan_runs_ma_{ma_type}",
                "function": "compute_ma",
                "dataset": "nan_ohlcv",
                "params": {"period": 5, "ma_type": ma_type},
                "outputs": [serialize_series(compute_ma(nc, period=5, ma_type=ma_type))],
            }
        )
    cases.append(
        {
            "case_id": "nan_runs_rolling_zscore",
            "function": "compute_rolling_zscore",
            "dataset": "nan_ohlcv",
            "params": {"window": 5},
            "outputs": [serialize_series(compute_rolling_zscore(nc, window=5))],
        }
    )
    cases.append(
        {
            "case_id": "nan_runs_rolling_rank",
            "function": "compute_rolling_rank",
            "dataset": "nan_ohlcv",
            "params": {"window": 5},
            "outputs": [serialize_series(compute_rolling_rank(nc, window=5))],
        }
    )
    cases.append(
        {
            "case_id": "nan_runs_pct_change",
            "function": "compute_pct_change",
            "dataset": "nan_ohlcv",
            "params": {"change_bars": 2},
            "outputs": [serialize_series(compute_pct_change(nc, change_bars=2))],
        }
    )
    cases.append(
        {
            "case_id": "nan_runs_clip",
            "function": "compute_clip",
            "dataset": "nan_ohlcv",
            "params": {"clip_low": 95.0, "clip_high": 105.0},
            "outputs": [serialize_series(compute_clip(nc, clip_low=95.0, clip_high=105.0))],
        }
    )

    # 4. Constant series (zero standard deviation, RSI 0/0)
    idx_const = pd.date_range("2023-01-02", periods=50, freq="h", tz="UTC")
    df_const = pd.DataFrame(
        {
            "open": np.full(50, 100.0),
            "high": np.full(50, 100.0),
            "low": np.full(50, 100.0),
            "close": np.full(50, 100.0),
        },
        index=idx_const,
    )
    cc, co, ch, cl = df_const["close"], df_const["open"], df_const["high"], df_const["low"]
    cases.append(
        {
            "case_id": "const_realized_vol",
            "function": "compute_realized_vol",
            "dataset": "const_ohlcv",
            "params": {"window": 5, "periods_per_year": 252},
            "outputs": [serialize_series(compute_realized_vol(cc, window=5, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "const_yang_zhang",
            "function": "compute_yang_zhang",
            "dataset": "const_ohlcv",
            "params": {"window": 5, "periods_per_year": 252},
            "outputs": [serialize_series(compute_yang_zhang(co, ch, cl, cc, window=5, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "const_rsi",
            "function": "compute_rsi",
            "dataset": "const_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(compute_rsi(cc, period=5))],
        }
    )
    cbu, cbm, cblo = compute_bollinger_bands(cc, period=5, num_std=2.0)
    cases.append(
        {
            "case_id": "const_bollinger",
            "function": "compute_bollinger_bands",
            "dataset": "const_ohlcv",
            "params": {"period": 5, "num_std": 2.0},
            "outputs": [serialize_series(cbu), serialize_series(cbm), serialize_series(cblo)],
        }
    )
    cml, csl, chist = compute_macd(cc, fast_period=4, slow_period=8, signal_period=3)
    cases.append(
        {
            "case_id": "const_macd",
            "function": "compute_macd",
            "dataset": "const_ohlcv",
            "params": {"fast_period": 4, "slow_period": 8, "signal_period": 3},
            "outputs": [serialize_series(cml), serialize_series(csl), serialize_series(chist)],
        }
    )
    cdu, cdlo = compute_donchian_channels(ch, cl, period=5)
    cases.append(
        {
            "case_id": "const_donchian",
            "function": "compute_donchian_channels",
            "dataset": "const_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(cdu), serialize_series(cdlo)],
        }
    )
    cases.append(
        {
            "case_id": "const_atr",
            "function": "compute_atr",
            "dataset": "const_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(compute_atr(ch, cl, cc, period=5))],
        }
    )
    for ma_type in sorted(VALID_MA_TYPES):
        cases.append(
            {
                "case_id": f"const_ma_{ma_type}",
                "function": "compute_ma",
                "dataset": "const_ohlcv",
                "params": {"period": 5, "ma_type": ma_type},
                "outputs": [serialize_series(compute_ma(cc, period=5, ma_type=ma_type))],
            }
        )
    cases.append(
        {
            "case_id": "const_rolling_zscore",
            "function": "compute_rolling_zscore",
            "dataset": "const_ohlcv",
            "params": {"window": 5},
            "outputs": [serialize_series(compute_rolling_zscore(cc, window=5))],
        }
    )
    cases.append(
        {
            "case_id": "const_rolling_rank",
            "function": "compute_rolling_rank",
            "dataset": "const_ohlcv",
            "params": {"window": 5},
            "outputs": [serialize_series(compute_rolling_rank(cc, window=5))],
        }
    )
    cases.append(
        {
            "case_id": "const_pct_change",
            "function": "compute_pct_change",
            "dataset": "const_ohlcv",
            "params": {"change_bars": 1},
            "outputs": [serialize_series(compute_pct_change(cc, change_bars=1))],
        }
    )
    cases.append(
        {
            "case_id": "const_clip",
            "function": "compute_clip",
            "dataset": "const_ohlcv",
            "params": {"clip_low": 90.0, "clip_high": 110.0},
            "outputs": [serialize_series(compute_clip(cc, clip_low=90.0, clip_high=110.0))],
        }
    )

    # 5. Strictly increasing series (RSI loss 0)
    vals_inc = 100.0 + np.arange(50, dtype=float)
    df_inc = pd.DataFrame(
        {
            "open": vals_inc - 0.2,
            "high": vals_inc + 0.5,
            "low": vals_inc - 0.5,
            "close": vals_inc,
        },
        index=idx_const,
    )
    ic, io, ih, il = df_inc["close"], df_inc["open"], df_inc["high"], df_inc["low"]
    cases.append(
        {
            "case_id": "inc_rsi",
            "function": "compute_rsi",
            "dataset": "increasing_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(compute_rsi(ic, period=5))],
        }
    )
    cases.append(
        {
            "case_id": "inc_realized_vol",
            "function": "compute_realized_vol",
            "dataset": "increasing_ohlcv",
            "params": {"window": 5, "periods_per_year": 252},
            "outputs": [serialize_series(compute_realized_vol(ic, window=5, periods_per_year=252))],
        }
    )
    cases.append(
        {
            "case_id": "inc_yang_zhang",
            "function": "compute_yang_zhang",
            "dataset": "increasing_ohlcv",
            "params": {"window": 5, "periods_per_year": 252},
            "outputs": [serialize_series(compute_yang_zhang(io, ih, il, ic, window=5, periods_per_year=252))],
        }
    )
    ibu, ibm, iblo = compute_bollinger_bands(ic, period=5, num_std=2.0)
    cases.append(
        {
            "case_id": "inc_bollinger",
            "function": "compute_bollinger_bands",
            "dataset": "increasing_ohlcv",
            "params": {"period": 5, "num_std": 2.0},
            "outputs": [serialize_series(ibu), serialize_series(ibm), serialize_series(iblo)],
        }
    )
    cases.append(
        {
            "case_id": "inc_atr",
            "function": "compute_atr",
            "dataset": "increasing_ohlcv",
            "params": {"period": 5},
            "outputs": [serialize_series(compute_atr(ih, il, ic, period=5))],
        }
    )
    for ma_type in sorted(VALID_MA_TYPES):
        cases.append(
            {
                "case_id": f"inc_ma_{ma_type}",
                "function": "compute_ma",
                "dataset": "increasing_ohlcv",
                "params": {"period": 5, "ma_type": ma_type},
                "outputs": [serialize_series(compute_ma(ic, period=5, ma_type=ma_type))],
            }
        )
    cases.append(
        {
            "case_id": "inc_rolling_zscore",
            "function": "compute_rolling_zscore",
            "dataset": "increasing_ohlcv",
            "params": {"window": 5},
            "outputs": [serialize_series(compute_rolling_zscore(ic, window=5))],
        }
    )
    cases.append(
        {
            "case_id": "inc_rolling_rank",
            "function": "compute_rolling_rank",
            "dataset": "increasing_ohlcv",
            "params": {"window": 5},
            "outputs": [serialize_series(compute_rolling_rank(ic, window=5))],
        }
    )

    # 6. Series containing 0.0 (percent change produces inf/-inf/nan)
    s_zeros = pd.Series([10.0, 0.0, 5.0, -2.0, 0.0, 0.0, 3.0, 10.0], name="zeros_data")
    cases.append(
        {
            "case_id": "zeros_pct_change_lag1",
            "function": "compute_pct_change",
            "dataset": "zeros_series",
            "params": {"change_bars": 1},
            "outputs": [serialize_series(compute_pct_change(s_zeros, change_bars=1))],
        }
    )
    cases.append(
        {
            "case_id": "zeros_pct_change_lag2",
            "function": "compute_pct_change",
            "dataset": "zeros_series",
            "params": {"change_bars": 2},
            "outputs": [serialize_series(compute_pct_change(s_zeros, change_bars=2))],
        }
    )

    # 7. int64 series
    s_int = pd.Series([10, 15, 12, 18, 22, 19, 25, 30, 28, 35], dtype="int64", name="int_data")
    # Whole-number bounds -> retains int64
    cases.append(
        {
            "case_id": "int64_clip_whole",
            "function": "compute_clip",
            "dataset": "int_series",
            "params": {"clip_low": 12.0, "clip_high": 25.0},
            "outputs": [serialize_series(compute_clip(s_int, clip_low=12.0, clip_high=25.0))],
        }
    )
    # Fractional bounds -> casts to float64
    cases.append(
        {
            "case_id": "int64_clip_float",
            "function": "compute_clip",
            "dataset": "int_series",
            "params": {"clip_low": 12.5, "clip_high": 25.5},
            "outputs": [serialize_series(compute_clip(s_int, clip_low=12.5, clip_high=25.5))],
        }
    )
    cases.append(
        {
            "case_id": "int64_rolling_zscore",
            "function": "compute_rolling_zscore",
            "dataset": "int_series",
            "params": {"window": 3},
            "outputs": [serialize_series(compute_rolling_zscore(s_int, window=3))],
        }
    )
    cases.append(
        {
            "case_id": "int64_rolling_rank",
            "function": "compute_rolling_rank",
            "dataset": "int_series",
            "params": {"window": 3},
            "outputs": [serialize_series(compute_rolling_rank(s_int, window=3))],
        }
    )
    cases.append(
        {
            "case_id": "int64_pct_change",
            "function": "compute_pct_change",
            "dataset": "int_series",
            "params": {"change_bars": 1},
            "outputs": [serialize_series(compute_pct_change(s_int, change_bars=1))],
        }
    )
    for ma_type in sorted(VALID_MA_TYPES):
        cases.append(
            {
                "case_id": f"int64_ma_{ma_type}",
                "function": "compute_ma",
                "dataset": "int_series",
                "params": {"period": 3, "ma_type": ma_type},
                "outputs": [serialize_series(compute_ma(s_int, period=3, ma_type=ma_type))],
            }
        )

    # 8. Float64 series with pd.NA
    s_nullable = pd.Series([10.0, pd.NA, 12.0, 15.0, pd.NA, 20.0, 22.0, 18.0], dtype="Float64", name="nullable_data")
    cases.append(
        {
            "case_id": "nullable_clip",
            "function": "compute_clip",
            "dataset": "nullable_series",
            "params": {"clip_low": 11.0, "clip_high": 19.0},
            "outputs": [serialize_series(compute_clip(s_nullable, clip_low=11.0, clip_high=19.0))],
        }
    )
    cases.append(
        {
            "case_id": "nullable_rolling_zscore",
            "function": "compute_rolling_zscore",
            "dataset": "nullable_series",
            "params": {"window": 3},
            "outputs": [serialize_series(compute_rolling_zscore(s_nullable, window=3))],
        }
    )
    cases.append(
        {
            "case_id": "nullable_rolling_rank",
            "function": "compute_rolling_rank",
            "dataset": "nullable_series",
            "params": {"window": 3},
            "outputs": [serialize_series(compute_rolling_rank(s_nullable, window=3))],
        }
    )
    cases.append(
        {
            "case_id": "nullable_pct_change",
            "function": "compute_pct_change",
            "dataset": "nullable_series",
            "params": {"change_bars": 1},
            "outputs": [serialize_series(compute_pct_change(s_nullable, change_bars=1))],
        }
    )
    for ma_type in sorted(VALID_MA_TYPES):
        cases.append(
            {
                "case_id": f"nullable_ma_{ma_type}",
                "function": "compute_ma",
                "dataset": "nullable_series",
                "params": {"period": 3, "ma_type": ma_type},
                "outputs": [serialize_series(compute_ma(s_nullable, period=3, ma_type=ma_type))],
            }
        )

    return cases


def get_dataset(name: str) -> pd.DataFrame | pd.Series:
    if name == "synthetic_ohlcv":
        return synthetic_ohlcv(400, seed=20240609)
    if name == "short_ohlcv":
        return synthetic_ohlcv(10, seed=20240609)
    if name == "nan_ohlcv":
        df = synthetic_ohlcv(50, seed=20240609).copy()
        df.iloc[15:20, :] = np.nan
        return df
    if name == "const_ohlcv":
        idx = pd.date_range("2023-01-02", periods=50, freq="h", tz="UTC")
        return pd.DataFrame(
            {
                "open": np.full(50, 100.0),
                "high": np.full(50, 100.0),
                "low": np.full(50, 100.0),
                "close": np.full(50, 100.0),
            },
            index=idx,
        )
    if name == "increasing_ohlcv":
        idx = pd.date_range("2023-01-02", periods=50, freq="h", tz="UTC")
        vals = 100.0 + np.arange(50, dtype=float)
        return pd.DataFrame(
            {
                "open": vals - 0.2,
                "high": vals + 0.5,
                "low": vals - 0.5,
                "close": vals,
            },
            index=idx,
        )
    if name == "zeros_series":
        return pd.Series([10.0, 0.0, 5.0, -2.0, 0.0, 0.0, 3.0, 10.0], name="zeros_data")
    if name == "int_series":
        return pd.Series([10, 15, 12, 18, 22, 19, 25, 30, 28, 35], dtype="int64", name="int_data")
    if name == "nullable_series":
        return pd.Series([10.0, pd.NA, 12.0, 15.0, pd.NA, 20.0, 22.0, 18.0], dtype="Float64", name="nullable_data")
    raise ValueError(f"Unknown dataset: {name}")


def main() -> None:
    cases = build_cases()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    data = {
        "metadata": {
            "q_backend_commit": commit,
            "pandas_version": pd.__version__,
            "numpy_version": np.__version__,
            "seed": 20240609,
            "case_count": len(cases),
            "policy": {
                "rule": "AbsRelTol",
                "abs_tol": 1e-10,
                "rel_tol": 1e-12,
            },
        },
        "cases": cases,
    }
    out_path = Path(__file__).parent / "pandas_baseline.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Exported {len(cases)} cases to {out_path} at commit {commit}")


if __name__ == "__main__":
    main()
