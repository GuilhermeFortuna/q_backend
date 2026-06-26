"""PIT-safe feature computation (WO128).

Dispatch mirrors ``composite_strategy._evaluate_node`` so feature series are
bit-identical to genome ``compute_indicators`` output for the same primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.moving_averages import compute_ma, normalize_ma_type
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)
from q_backend.features.leakage import FORWARD_LOOKING_KINDS
from q_backend.features.registry import FeatureSpec, feature_id, resolve_params

_REQUIRED_BAR_COLUMNS = frozenset({"time", "open", "high", "low", "close", "volume"})

# Feature catalog name → node output port (multi-port primitives).
_FEATURE_OUTPUT_PORT: dict[str, str] = {
    "rsi": "out",
    "atr": "out",
    "ma": "out",
    "ema": "out",
    "macd": "macd",
    "macd_signal": "macd_signal",
    "macd_histogram": "macd_histogram",
    "bollinger_upper": "bb_upper",
    "bollinger_middle": "bb_middle",
    "bollinger_lower": "bb_lower",
    "donchian_upper": "donchian_upper",
    "donchian_lower": "donchian_lower",
    "momentum": "out",
    "realized_vol": "out",
    "tsmom": "momentum",
    "tsmom_volatility": "volatility",
    "trend_blend": "out",
    "trend_blend_volatility": "volatility",
}


@dataclass(frozen=True)
class FeatureSeries:
    feature_id: str
    series: pd.Series
    warmup_bars: int
    leakage_status: str


def _validate_bars(bars: pd.DataFrame) -> None:
    missing = _REQUIRED_BAR_COLUMNS - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    times = bars["time"]
    if not times.is_monotonic_increasing:
        raise ValueError("bars must be time-sorted ascending")


def _warmup_bars(spec: FeatureSpec, params: dict[str, Any]) -> int:
    if spec.lookback_param is None:
        return 0
    return int(params[spec.lookback_param])


def _output_port(feature_name: str) -> str:
    try:
        return _FEATURE_OUTPUT_PORT[feature_name]
    except KeyError as exc:
        raise KeyError(f"No output port mapping for feature '{feature_name}'.") from exc


def _apply_warmup(series: pd.Series, warmup_bars: int) -> pd.Series:
    if warmup_bars <= 0:
        return series
    trimmed = series.copy()
    trimmed.iloc[:warmup_bars] = np.nan
    return trimmed


def _compute_tsmom_outputs(
    df: pd.DataFrame, params: dict[str, Any]
) -> dict[str, pd.Series]:
    """Mirror ``CompositeStrategy._evaluate_tsmom`` (momentum + volatility ports)."""
    lookback = int(params["lookback_bars"])
    vol_window = int(params["vol_window"])
    vol_estimator = str(params["vol_estimator"])

    momentum = df["close"] / df["close"].shift(lookback) - 1.0
    if vol_estimator == "yang_zhang":
        volatility = compute_yang_zhang(
            df["open"], df["high"], df["low"], df["close"], vol_window
        )
    else:
        volatility = compute_realized_vol(df["close"], vol_window)

    return {"momentum": momentum, "volatility": volatility}


def _compute_node_outputs(
    node_kind: str, df: pd.DataFrame, params: dict[str, Any]
) -> dict[str, pd.Series]:
    """Return all output ports for ``node_kind`` — same leaf calls as ``_evaluate_node``."""
    close = df["close"]

    if node_kind == "ind.ma":
        return {
            "out": compute_ma(
                close, int(params["period"]), normalize_ma_type(str(params["ma_type"]))
            )
        }
    if node_kind == "ind.ema":
        return {"out": compute_ma(close, int(params["period"]), "ema")}
    if node_kind == "ind.rsi":
        return {"out": compute_rsi(close, int(params["period"]))}
    if node_kind == "ind.atr":
        return {
            "out": compute_atr(
                df["high"], df["low"], df["close"], int(params["period"])
            )
        }
    if node_kind == "ind.macd":
        macd_line, signal_line, histogram = compute_macd(
            close,
            int(params["fast_period"]),
            int(params["slow_period"]),
            int(params["signal_period"]),
        )
        return {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_histogram": histogram,
        }
    if node_kind == "ind.bollinger":
        upper, middle, lower = compute_bollinger_bands(
            close, int(params["period"]), float(params["num_std"])
        )
        return {"bb_upper": upper, "bb_middle": middle, "bb_lower": lower}
    if node_kind == "ind.donchian":
        upper, lower = compute_donchian_channels(
            df["high"], df["low"], int(params["period"])
        )
        return {"donchian_upper": upper, "donchian_lower": lower}
    if node_kind == "ind.momentum":
        lookback = int(params["lookback_bars"])
        return {"out": close / close.shift(lookback) - 1.0}
    if node_kind == "ind.realized_vol":
        window = int(params["window"])
        estimator = str(params.get("estimator", "close_to_close"))
        if estimator == "yang_zhang":
            vol = compute_yang_zhang(
                df["open"], df["high"], df["low"], df["close"], window
            )
        else:
            vol = compute_realized_vol(close, window)
        return {"out": vol}
    if node_kind == "ind.tsmom":
        return _compute_tsmom_outputs(df, params)
    if node_kind == "ind.trend_blend":
        l1 = int(params["lookback_1"])
        l2 = int(params["lookback_2"])
        l3 = int(params["lookback_3"])
        vol_w = int(params["vol_window"])

        ret1 = close / close.shift(l1) - 1.0
        ret2 = close / close.shift(l2) - 1.0
        ret3 = close / close.shift(l3) - 1.0

        sig1 = np.where(ret1.isna(), np.nan, np.sign(ret1))
        sig2 = np.where(ret2.isna(), np.nan, np.sign(ret2))
        sig3 = np.where(ret3.isna(), np.nan, np.sign(ret3))

        blend = (pd.Series(sig1, index=df.index) + sig2 + sig3) / 3.0
        return {"out": blend, "volatility": compute_realized_vol(close, vol_w)}

    raise ValueError(f"Unsupported feature node kind '{node_kind}'.")


def compute_feature(
    bars: pd.DataFrame,
    spec: FeatureSpec,
    params: dict[str, Any],
) -> FeatureSeries:
    """Compute a named feature as a causal bar→series mapping."""
    _validate_bars(bars)
    resolved = resolve_params(spec, params)
    df = bars.sort_values("time").reset_index(drop=True)

    port = _output_port(spec.name)
    raw = _compute_node_outputs(spec.node_kind, df, resolved)[port]

    warmup = _warmup_bars(spec, resolved)
    values = _apply_warmup(raw.reset_index(drop=True), warmup)
    series = pd.Series(values.to_numpy(), index=df["time"], name=spec.name)

    leakage_status = spec.leakage_status
    if spec.node_kind in FORWARD_LOOKING_KINDS:
        leakage_status = "suspect"

    return FeatureSeries(
        feature_id=feature_id(spec, resolved),
        series=series,
        warmup_bars=warmup,
        leakage_status=leakage_status,
    )
