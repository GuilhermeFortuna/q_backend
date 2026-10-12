"""Chartable indicator catalog — single source for routes and exclusion tests (Q-112)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from q_backend.backtesting.moving_averages import compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_atr_fraction,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_ewma_trend_spread,
    compute_ewma_vol_per_bar,
    compute_log_return,
    compute_macd,
    compute_normalized_ewma_trend_spread,
    compute_normalized_momentum,
    compute_per_bar_volatility,
    compute_price_zscore_log,
    compute_realized_vol,
    compute_realized_vol_per_bar,
    compute_rogers_satchell,
    compute_rsi,
    compute_true_range,
    compute_volatility_log_ratio,
    compute_yang_zhang,
)
from q_backend.backtesting.transforms import compute_pct_change, compute_rolling_rank, compute_rolling_zscore

ParamType = Literal["int", "float", "string"]
Pane = Literal["price", "oscillator"]
RequiredData = Literal["bars", "quotes"]

CATALOG_EXCLUDED_KERNELS: frozenset[str] = frozenset({"clip"})

VOL_ESTIMATOR_CHOICES = ("realized", "ewma", "rogers_satchell", "yang_zhang")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class OutputSpec:
    key: str
    label: str
    pane: Pane


@dataclass(frozen=True)
class IndicatorEntry:
    id: str
    label: str
    category: str
    params: tuple[ParamSpec, ...]
    outputs: tuple[OutputSpec, ...]
    pane: Pane
    unit: str | None
    required_data: RequiredData
    uses_kernels: frozenset[str]
    warmup_bars: Callable[[Mapping[str, Any]], int]
    compute: Callable[[pd.DataFrame, Mapping[str, Any]], dict[str, pd.Series]]


def ewma_warmup_bars(half_life: float) -> int:
    return int(1 + math.ceil(float(half_life) * math.log2(100.0)))


def _param_defaults(entry: IndicatorEntry) -> dict[str, Any]:
    return {spec.name: spec.default for spec in entry.params}


def _int_param(params: Mapping[str, Any], name: str, *, minimum: int | None = None) -> int:
    value = params.get(name)
    if value is None:
        raise ValueError(f"Missing required parameter '{name}'.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parameter '{name}' must be an integer.") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"Parameter '{name}' must be >= {minimum} (got {parsed}).")
    return parsed


def _float_param(params: Mapping[str, Any], name: str, *, minimum: float | None = None) -> float:
    value = params.get(name)
    if value is None:
        raise ValueError(f"Missing required parameter '{name}'.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parameter '{name}' must be a number.") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"Parameter '{name}' must be >= {minimum} (got {parsed}).")
    return parsed


def _choice_param(params: Mapping[str, Any], name: str, choices: tuple[str, ...]) -> str:
    value = str(params.get(name, "")).lower().strip()
    if value not in choices:
        allowed = ", ".join(choices)
        raise ValueError(f"Parameter '{name}' must be one of: {allowed} (got {params.get(name)!r}).")
    return value


def _ohlc(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    return frame["open"], frame["high"], frame["low"], frame["close"]


def _compute_sma(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    close = frame["close"]
    return {"line": compute_ma(close, period, "sma")}


def _compute_ema(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    close = frame["close"]
    return {"line": compute_ma(close, period, "ema")}


def _compute_wma(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    return {"line": compute_ma(frame["close"], period, "wma")}


def _compute_hma(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    return {"line": compute_ma(frame["close"], period, "hma")}


def _compute_smma(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    return {"line": compute_ma(frame["close"], period, "smma")}


def _compute_volume_ma(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    volume = frame["volume"] if "volume" in frame.columns else pd.Series(np.nan, index=frame.index)
    return {"line": compute_ma(volume, period, "sma")}


def _compute_bollinger(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    num_std = _float_param(params, "num_std", minimum=0.0)
    upper, middle, lower = compute_bollinger_bands(frame["close"], period, num_std)
    return {"upper": upper, "middle": middle, "lower": lower}


def _compute_donchian(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    upper, lower = compute_donchian_channels(frame["high"], frame["low"], period)
    return {"upper": upper, "lower": lower}


def _compute_rsi(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    return {"line": compute_rsi(frame["close"], period)}


def _compute_macd(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    fast = _int_param(params, "fast_period", minimum=1)
    slow = _int_param(params, "slow_period", minimum=1)
    signal = _int_param(params, "signal_period", minimum=1)
    if fast >= slow:
        raise ValueError(f"fast_period must be < slow_period (got {fast} >= {slow}).")
    line, sig, hist = compute_macd(frame["close"], fast, slow, signal)
    return {"line": line, "signal": sig, "histogram": hist}


def _compute_rolling_zscore(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=1)
    return {"line": compute_rolling_zscore(frame["close"], window)}


def _compute_rolling_rank(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=1)
    return {"line": compute_rolling_rank(frame["close"], window)}


def _compute_pct_change(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    lag = _int_param(params, "change_bars", minimum=1)
    return {"line": compute_pct_change(frame["close"], lag)}


def _compute_log_return(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    horizon = _int_param(params, "horizon", minimum=1)
    return {"line": compute_log_return(frame["close"], horizon)}


def _vol_params(params: Mapping[str, Any]) -> tuple[str, int | None, float | None]:
    estimator = _choice_param(params, "vol_estimator", VOL_ESTIMATOR_CHOICES)
    vol_window = _int_param(params, "vol_window", minimum=1) if estimator != "ewma" else None
    vol_half_life = _float_param(params, "vol_half_life", minimum=1e-12) if estimator == "ewma" else None
    return estimator, vol_window, vol_half_life


def _compute_normalized_momentum(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    horizon = _int_param(params, "horizon", minimum=1)
    vol_lag = _int_param(params, "vol_lag", minimum=0)
    if vol_lag not in (0, 1):
        raise ValueError(f"vol_lag must be 0 or 1 (got {vol_lag}).")
    estimator, vol_window, vol_half_life = _vol_params(params)
    o, h, l, c = _ohlc(frame)
    return {
        "line": compute_normalized_momentum(
            c,
            o,
            h,
            l,
            horizon=horizon,
            vol_estimator=estimator,
            vol_window=vol_window,
            vol_half_life=vol_half_life,
            vol_lag=vol_lag,
        )
    }


def _compute_normalized_ewma_trend(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    fast = _float_param(params, "fast_half_life", minimum=1e-12)
    slow = _float_param(params, "slow_half_life", minimum=1e-12)
    if fast >= slow:
        raise ValueError(f"fast_half_life must be < slow_half_life (got {fast} >= {slow}).")
    vol_lag = _int_param(params, "vol_lag", minimum=0)
    if vol_lag not in (0, 1):
        raise ValueError(f"vol_lag must be 0 or 1 (got {vol_lag}).")
    estimator, vol_window, vol_half_life = _vol_params(params)
    o, h, l, c = _ohlc(frame)
    return {
        "line": compute_normalized_ewma_trend_spread(
            c,
            o,
            h,
            l,
            fast_half_life=fast,
            slow_half_life=slow,
            vol_estimator=estimator,
            vol_window=vol_window,
            vol_half_life=vol_half_life,
            vol_lag=vol_lag,
        )
    }


def _compute_price_zscore_log(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=2)
    return {"line": compute_price_zscore_log(frame["close"], window)}


def _compute_atr(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    o, h, l, c = _ohlc(frame)
    return {"line": compute_atr(h, l, c, period)}


def _compute_atr_fraction(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    period = _int_param(params, "period", minimum=1)
    o, h, l, c = _ohlc(frame)
    return {"line": compute_atr_fraction(h, l, c, period)}


def _compute_true_range(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    o, h, l, c = _ohlc(frame)
    return {"line": compute_true_range(h, l, c)}


def _compute_realized_vol_ann(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=1)
    ppy = _int_param(params, "periods_per_year", minimum=1)
    return {"line": compute_realized_vol(frame["close"], window, ppy)}


def _compute_per_bar_realized(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=1)
    return {"line": compute_realized_vol_per_bar(frame["close"], window)}


def _compute_per_bar_ewma(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    half_life = _float_param(params, "half_life", minimum=1e-12)
    return {"line": compute_ewma_vol_per_bar(frame["close"], half_life)}


def _compute_per_bar_rogers(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=1)
    o, h, l, c = _ohlc(frame)
    return {"line": compute_rogers_satchell(o, h, l, c, window)}


def _compute_per_bar_yang_zhang(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    window = _int_param(params, "window", minimum=2)
    o, h, l, c = _ohlc(frame)
    return {"line": compute_yang_zhang(o, h, l, c, window, periods_per_year=1)}


def _compute_vol_log_ratio(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    short_w = _int_param(params, "short_window", minimum=1)
    long_w = _int_param(params, "long_window", minimum=1)
    if short_w >= long_w:
        raise ValueError(f"short_window must be < long_window (got {short_w} >= {long_w}).")
    estimator = _choice_param(params, "vol_estimator", VOL_ESTIMATOR_CHOICES)
    o, h, l, c = _ohlc(frame)
    if estimator == "ewma":
        short_hl = _float_param(params, "short_half_life", minimum=1e-12)
        long_hl = _float_param(params, "long_half_life", minimum=1e-12)
        if short_hl >= long_hl:
            raise ValueError(f"short_half_life must be < long_half_life (got {short_hl} >= {long_hl}).")
        sigma_short = compute_ewma_vol_per_bar(c, short_hl)
        sigma_long = compute_ewma_vol_per_bar(c, long_hl)
    else:
        sigma_short = compute_per_bar_volatility(o, h, l, c, estimator=estimator, window=short_w, half_life=None)
        sigma_long = compute_per_bar_volatility(o, h, l, c, estimator=estimator, window=long_w, half_life=None)
    return {"line": compute_volatility_log_ratio(sigma_short, sigma_long)}


def _compute_ewma_trend_spread_raw(frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    fast = _float_param(params, "fast_half_life", minimum=1e-12)
    slow = _float_param(params, "slow_half_life", minimum=1e-12)
    if fast >= slow:
        raise ValueError(f"fast_half_life must be < slow_half_life (got {fast} >= {slow}).")
    return {"line": compute_ewma_trend_spread(frame["close"], fast, slow)}


def _period_param(name: str = "period", default: int = 20) -> ParamSpec:
    return ParamSpec(name, "int", default, minimum=1.0)


def _window_param(default: int = 20) -> ParamSpec:
    return ParamSpec("window", "int", default, minimum=1.0)


_VOL_FLAT_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("vol_estimator", "string", "realized", choices=VOL_ESTIMATOR_CHOICES),
    ParamSpec("vol_window", "int", 20, minimum=1.0),
    ParamSpec("vol_half_life", "float", 32.0, minimum=1e-12),
    ParamSpec("vol_lag", "int", 1, minimum=0.0, maximum=1.0),
)


INDICATOR_CATALOG: tuple[IndicatorEntry, ...] = (
    IndicatorEntry(
        "sma",
        "SMA",
        "Moving averages",
        (_period_param(),),
        (OutputSpec("line", "SMA", "price"),),
        "price",
        None,
        "bars",
        frozenset({"sma"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_sma,
    ),
    IndicatorEntry(
        "ema",
        "EMA",
        "Moving averages",
        (_period_param(),),
        (OutputSpec("line", "EMA", "price"),),
        "price",
        None,
        "bars",
        frozenset({"ema"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_ema,
    ),
    IndicatorEntry(
        "wma",
        "WMA",
        "Moving averages",
        (_period_param(),),
        (OutputSpec("line", "WMA", "price"),),
        "price",
        None,
        "bars",
        frozenset({"wma"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_wma,
    ),
    IndicatorEntry(
        "hma",
        "HMA",
        "Moving averages",
        (_period_param(),),
        (OutputSpec("line", "HMA", "price"),),
        "price",
        None,
        "bars",
        frozenset({"hma"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_hma,
    ),
    IndicatorEntry(
        "smma",
        "SMMA",
        "Moving averages",
        (_period_param(),),
        (OutputSpec("line", "SMMA", "price"),),
        "price",
        None,
        "bars",
        frozenset({"smma"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_smma,
    ),
    IndicatorEntry(
        "volume_ma",
        "Volume MA",
        "Moving averages",
        (_period_param(),),
        (OutputSpec("line", "Volume MA", "price"),),
        "price",
        None,
        "bars",
        frozenset({"sma"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_volume_ma,
    ),
    IndicatorEntry(
        "bollinger",
        "Bollinger bands",
        "Bands and channels",
        (
            _period_param(20),
            ParamSpec("num_std", "float", 2.0, minimum=0.0),
        ),
        (
            OutputSpec("upper", "Upper", "price"),
            OutputSpec("middle", "Middle", "price"),
            OutputSpec("lower", "Lower", "price"),
        ),
        "price",
        None,
        "bars",
        frozenset({"bollinger_bands"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_bollinger,
    ),
    IndicatorEntry(
        "donchian",
        "Donchian channels",
        "Bands and channels",
        (_period_param(),),
        (
            OutputSpec("upper", "Upper", "price"),
            OutputSpec("lower", "Lower", "price"),
        ),
        "price",
        None,
        "bars",
        frozenset({"donchian_channels"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_donchian,
    ),
    IndicatorEntry(
        "rsi",
        "RSI",
        "Oscillators",
        (_period_param(14),),
        (OutputSpec("line", "RSI", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"rsi"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_rsi,
    ),
    IndicatorEntry(
        "macd",
        "MACD",
        "Oscillators",
        (
            ParamSpec("fast_period", "int", 12, minimum=1.0),
            ParamSpec("slow_period", "int", 26, minimum=1.0),
            ParamSpec("signal_period", "int", 9, minimum=1.0),
        ),
        (
            OutputSpec("line", "MACD", "oscillator"),
            OutputSpec("signal", "Signal", "oscillator"),
            OutputSpec("histogram", "Histogram", "oscillator"),
        ),
        "oscillator",
        None,
        "bars",
        frozenset({"macd"}),
        lambda p: max(
            _int_param(p, "slow_period", minimum=1),
            _int_param(p, "signal_period", minimum=1),
        ),
        _compute_macd,
    ),
    IndicatorEntry(
        "rolling_zscore",
        "Rolling z-score",
        "Oscillators",
        (_window_param(),),
        (OutputSpec("line", "Z-score", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"rolling_zscore"}),
        lambda p: _int_param(p, "window", minimum=1),
        _compute_rolling_zscore,
    ),
    IndicatorEntry(
        "rolling_rank",
        "Rolling rank",
        "Oscillators",
        (_window_param(),),
        (OutputSpec("line", "Rank", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"rolling_rank"}),
        lambda p: _int_param(p, "window", minimum=1),
        _compute_rolling_rank,
    ),
    IndicatorEntry(
        "pct_change",
        "Percent change",
        "Oscillators",
        (ParamSpec("change_bars", "int", 1, minimum=1.0),),
        (OutputSpec("line", "% change", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"pct_change"}),
        lambda p: _int_param(p, "change_bars", minimum=1),
        _compute_pct_change,
    ),
    IndicatorEntry(
        "log_return",
        "Log return",
        "Returns and trend",
        (ParamSpec("horizon", "int", 1, minimum=1.0),),
        (OutputSpec("line", "Log return", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"log_return"}),
        lambda p: _int_param(p, "horizon", minimum=1),
        _compute_log_return,
    ),
    IndicatorEntry(
        "normalized_momentum",
        "Volatility-normalized momentum",
        "Returns and trend",
        (ParamSpec("horizon", "int", 20, minimum=1.0),) + _VOL_FLAT_PARAMS,
        (OutputSpec("line", "Norm. momentum", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset(
            {"log_return", "vol_normalize", "realized_vol_per_bar", "ewma_vol_per_bar", "rogers_satchell", "yang_zhang"}
        ),
        lambda p: _int_param(p, "horizon", minimum=1)
        + max(
            (
                _int_param(p, "vol_window", minimum=1)
                if _choice_param(p, "vol_estimator", VOL_ESTIMATOR_CHOICES) != "ewma"
                else 0
            ),
            (
                ewma_warmup_bars(_float_param(p, "vol_half_life", minimum=1e-12))
                if _choice_param(p, "vol_estimator", VOL_ESTIMATOR_CHOICES) == "ewma"
                else 0
            ),
        ),
        _compute_normalized_momentum,
    ),
    IndicatorEntry(
        "normalized_ewma_trend_spread",
        "Normalized EWMA trend spread",
        "Returns and trend",
        (
            ParamSpec("fast_half_life", "float", 8.0, minimum=1e-12),
            ParamSpec("slow_half_life", "float", 32.0, minimum=1e-12),
        )
        + _VOL_FLAT_PARAMS,
        (OutputSpec("line", "Norm. trend spread", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset(
            {
                "ewma_trend_spread",
                "vol_normalize",
                "realized_vol_per_bar",
                "ewma_vol_per_bar",
                "rogers_satchell",
                "yang_zhang",
            }
        ),
        lambda p: ewma_warmup_bars(_float_param(p, "slow_half_life", minimum=1e-12)),
        _compute_normalized_ewma_trend,
    ),
    IndicatorEntry(
        "price_zscore_log",
        "Log-price z-score",
        "Returns and trend",
        (_window_param(20),),
        (OutputSpec("line", "Log-price z", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"price_zscore_log"}),
        lambda p: _int_param(p, "window", minimum=2),
        _compute_price_zscore_log,
    ),
    IndicatorEntry(
        "ewma_trend_spread",
        "EWMA trend spread",
        "Returns and trend",
        (
            ParamSpec("fast_half_life", "float", 8.0, minimum=1e-12),
            ParamSpec("slow_half_life", "float", 32.0, minimum=1e-12),
        ),
        (OutputSpec("line", "Trend spread", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"ewma_trend_spread"}),
        lambda p: ewma_warmup_bars(_float_param(p, "slow_half_life", minimum=1e-12)),
        _compute_ewma_trend_spread_raw,
    ),
    IndicatorEntry(
        "atr",
        "ATR",
        "Volatility and range",
        (_period_param(14),),
        (OutputSpec("line", "ATR", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"atr"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_atr,
    ),
    IndicatorEntry(
        "atr_fraction",
        "ATR fraction",
        "Volatility and range",
        (_period_param(14),),
        (OutputSpec("line", "ATR / close", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"atr_fraction", "atr"}),
        lambda p: _int_param(p, "period", minimum=1),
        _compute_atr_fraction,
    ),
    IndicatorEntry(
        "true_range",
        "True range",
        "Volatility and range",
        (),
        (OutputSpec("line", "True range", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"true_range"}),
        lambda _p: 1,
        _compute_true_range,
    ),
    IndicatorEntry(
        "realized_vol",
        "Realized volatility (annualized)",
        "Volatility and range",
        (
            _window_param(20),
            ParamSpec("periods_per_year", "int", 252, minimum=1.0),
        ),
        (OutputSpec("line", "Realized vol", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"realized_vol"}),
        lambda p: _int_param(p, "window", minimum=1),
        _compute_realized_vol_ann,
    ),
    IndicatorEntry(
        "per_bar_realized_vol",
        "Per-bar realized volatility",
        "Volatility and range",
        (_window_param(20),),
        (OutputSpec("line", "Per-bar vol", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"realized_vol_per_bar"}),
        lambda p: _int_param(p, "window", minimum=1),
        _compute_per_bar_realized,
    ),
    IndicatorEntry(
        "per_bar_ewma_vol",
        "Per-bar EWMA volatility",
        "Volatility and range",
        (ParamSpec("half_life", "float", 32.0, minimum=1e-12),),
        (OutputSpec("line", "EWMA vol", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"ewma_vol_per_bar"}),
        lambda p: ewma_warmup_bars(_float_param(p, "half_life", minimum=1e-12)),
        _compute_per_bar_ewma,
    ),
    IndicatorEntry(
        "rogers_satchell",
        "Rogers–Satchell",
        "Volatility and range",
        (_window_param(20),),
        (OutputSpec("line", "Rogers–Satchell", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"rogers_satchell"}),
        lambda p: _int_param(p, "window", minimum=1),
        _compute_per_bar_rogers,
    ),
    IndicatorEntry(
        "per_bar_yang_zhang",
        "Per-bar Yang–Zhang",
        "Volatility and range",
        (_window_param(20),),
        (OutputSpec("line", "YZ vol", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset({"yang_zhang"}),
        lambda p: _int_param(p, "window", minimum=2),
        _compute_per_bar_yang_zhang,
    ),
    IndicatorEntry(
        "volatility_log_ratio",
        "Volatility log ratio",
        "Volatility and range",
        (
            ParamSpec("vol_estimator", "string", "realized", choices=VOL_ESTIMATOR_CHOICES),
            ParamSpec("short_window", "int", 10, minimum=1.0),
            ParamSpec("long_window", "int", 40, minimum=1.0),
            ParamSpec("short_half_life", "float", 8.0, minimum=1e-12),
            ParamSpec("long_half_life", "float", 32.0, minimum=1e-12),
        ),
        (OutputSpec("line", "Vol log ratio", "oscillator"),),
        "oscillator",
        None,
        "bars",
        frozenset(
            {"volatility_log_ratio", "realized_vol_per_bar", "ewma_vol_per_bar", "rogers_satchell", "yang_zhang"}
        ),
        lambda p: (
            _int_param(p, "long_window", minimum=1)
            if _choice_param(p, "vol_estimator", VOL_ESTIMATOR_CHOICES) != "ewma"
            else ewma_warmup_bars(_float_param(p, "long_half_life", minimum=1e-12))
        ),
        _compute_vol_log_ratio,
    ),
    IndicatorEntry(
        "quote_spread",
        "Quoted spread",
        "Liquidity",
        (
            ParamSpec("output", "string", "time_weighted_mean", choices=("last", "time_weighted_mean", "max")),
            ParamSpec("max_quote_age_ms", "int", 60_000, minimum=0.0),
            ParamSpec("min_coverage", "float", 0.0, minimum=0.0, maximum=1.0),
        ),
        (OutputSpec("line", "Spread", "oscillator"),),
        "oscillator",
        "bps",
        "quotes",
        frozenset({"quote_spread_bars"}),
        lambda _p: 0,
        lambda _frame, _params: {},  # quote indicators are computed in the series service
    ),
    IndicatorEntry(
        "spread_vs_volatility",
        "Spread vs volatility",
        "Liquidity",
        (
            ParamSpec("spread_output", "string", "time_weighted_mean", choices=("last", "time_weighted_mean", "max")),
            ParamSpec("max_quote_age_ms", "int", 60_000, minimum=0.0),
            ParamSpec("min_coverage", "float", 0.0, minimum=0.0, maximum=1.0),
        )
        + _VOL_FLAT_PARAMS,
        (OutputSpec("line", "Spread / vol", "oscillator"),),
        "oscillator",
        None,
        "quotes",
        frozenset(
            {
                "quote_spread_bars",
                "vol_normalize",
                "realized_vol_per_bar",
                "ewma_vol_per_bar",
                "rogers_satchell",
                "yang_zhang",
            }
        ),
        lambda _p: 0,
        lambda _frame, _params: {},
    ),
)


CATALOG_BY_ID: dict[str, IndicatorEntry] = {entry.id: entry for entry in INDICATOR_CATALOG}


def catalog_entry(identifier: str) -> IndicatorEntry:
    try:
        return CATALOG_BY_ID[identifier]
    except KeyError as exc:
        raise KeyError(f"Unknown indicator identifier '{identifier}'.") from exc


def merge_params(entry: IndicatorEntry, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = _param_defaults(entry)
    if overrides:
        merged.update(overrides)
    return merged


def validate_params(entry: IndicatorEntry, params: Mapping[str, Any]) -> dict[str, Any]:
    merged = merge_params(entry, params)
    for spec in entry.params:
        if spec.name not in merged:
            continue
        value = merged[spec.name]
        if spec.type == "int":
            parsed = _int_param(merged, spec.name, minimum=int(spec.minimum) if spec.minimum is not None else None)
            if spec.maximum is not None and parsed > int(spec.maximum):
                raise ValueError(f"Parameter '{spec.name}' must be <= {int(spec.maximum)} (got {parsed}).")
            merged[spec.name] = parsed
        elif spec.type == "float":
            parsed = _float_param(merged, spec.name, minimum=spec.minimum)
            if spec.maximum is not None and parsed > float(spec.maximum):
                raise ValueError(f"Parameter '{spec.name}' must be <= {spec.maximum} (got {parsed}).")
            merged[spec.name] = parsed
        elif spec.type == "string":
            if spec.choices is not None:
                merged[spec.name] = _choice_param(merged, spec.name, spec.choices)
            else:
                merged[spec.name] = str(value)
    if entry.id == "macd":
        if merged["fast_period"] >= merged["slow_period"]:
            raise ValueError(
                f"fast_period must be < slow_period (got {merged['fast_period']} >= {merged['slow_period']})."
            )
    if entry.id in ("normalized_ewma_trend_spread", "ewma_trend_spread"):
        if merged["fast_half_life"] >= merged["slow_half_life"]:
            raise ValueError(
                "fast_half_life must be < slow_half_life "
                f"(got {merged['fast_half_life']} >= {merged['slow_half_life']})."
            )
    if entry.id == "volatility_log_ratio":
        if merged["vol_estimator"] == "ewma":
            if merged["short_half_life"] >= merged["long_half_life"]:
                raise ValueError(
                    "short_half_life must be < long_half_life "
                    f"(got {merged['short_half_life']} >= {merged['long_half_life']})."
                )
        elif merged["short_window"] >= merged["long_window"]:
            raise ValueError(
                f"short_window must be < long_window (got {merged['short_window']} >= {merged['long_window']})."
            )
    return merged


def compute_indicator(entry: IndicatorEntry, frame: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.Series]:
    resolved = validate_params(entry, params)
    if entry.required_data == "quotes":
        raise ValueError(f"Indicator '{entry.id}' is computed from quotes in the series service.")
    return entry.compute(frame, resolved)


def charted_kernels() -> frozenset[str]:
    covered: set[str] = set()
    for entry in INDICATOR_CATALOG:
        covered.update(entry.uses_kernels)
    return frozenset(covered)


def published_q_core_kernels() -> frozenset[str]:
    from q_backend.backtesting import indicator_kernels

    target = indicator_kernels.kernels
    return frozenset(name for name in dir(target) if not name.startswith("_") and callable(getattr(target, name)))


def catalog_response_entries() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for entry in INDICATOR_CATALOG:
        payload.append(
            {
                "id": entry.id,
                "label": entry.label,
                "category": entry.category,
                "pane": entry.pane,
                "unit": entry.unit,
                "requiredData": entry.required_data,
                "parameters": [
                    {
                        "name": spec.name,
                        "type": spec.type,
                        "default": spec.default,
                        "minimum": spec.minimum,
                        "maximum": spec.maximum,
                        "choices": list(spec.choices) if spec.choices else None,
                    }
                    for spec in entry.params
                ],
                "outputs": [{"key": out.key, "label": out.label, "pane": out.pane} for out in entry.outputs],
            }
        )
    return payload
