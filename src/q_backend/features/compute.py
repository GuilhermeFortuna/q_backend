"""PIT-safe feature computation (WO128, WO143).

Dispatch mirrors ``composite_strategy._evaluate_node`` so feature series are
bit-identical to genome ``compute_indicators`` output for the same primitive.
Neural latents dispatch through a model-output cache (one ``transform`` per
model + bar range) and are trimmed to OOS bars only.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
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
from q_backend.features.leakage import (
    FORWARD_LOOKING_KINDS,
    assert_neural_oos_only,
    neural_leakage_status,
    to_utc_series,
)
from q_backend.features.registry import FeatureSpec, feature_id, get_feature_spec, resolve_params
from q_backend.storage.lake.artifacts import read_neural_model

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

# In-process LRU: key ``(model_hash, bars_range_hash)`` → full latent frame.
_MODEL_OUTPUT_CACHE: OrderedDict[tuple[str, str], pd.DataFrame] = OrderedDict()
_MODEL_OUTPUT_CACHE_MAXSIZE = 32


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


def _to_utc_timestamp(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _bars_range_hash(bars: pd.DataFrame) -> str:
    """Content hash of bar count and time endpoints (model-output cache key component)."""
    if bars.empty:
        return "empty"
    times = bars["time"]
    payload = f"{len(bars)}|{times.iloc[0]}|{times.iloc[-1]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cache_get(model_hash: str, bars: pd.DataFrame) -> pd.DataFrame | None:
    key = (model_hash, _bars_range_hash(bars))
    frame = _MODEL_OUTPUT_CACHE.get(key)
    if frame is None:
        return None
    _MODEL_OUTPUT_CACHE.move_to_end(key)
    return frame


def _cache_put(model_hash: str, bars: pd.DataFrame, frame: pd.DataFrame) -> None:
    key = (model_hash, _bars_range_hash(bars))
    _MODEL_OUTPUT_CACHE[key] = frame
    _MODEL_OUTPUT_CACHE.move_to_end(key)
    while len(_MODEL_OUTPUT_CACHE) > _MODEL_OUTPUT_CACHE_MAXSIZE:
        _MODEL_OUTPUT_CACHE.popitem(last=False)


def clear_model_output_cache() -> None:
    """Clear the in-process neural transform cache (test helper)."""
    _MODEL_OUTPUT_CACHE.clear()


def _warmup_bars(spec: FeatureSpec, params: dict[str, Any]) -> int:
    if spec.lookback_param is None:
        return 0
    return int(params[spec.lookback_param])


def _oos_warmup_bars(times: pd.Series, train_end: datetime) -> int:
    train_end_ts = _to_utc_timestamp(train_end)
    return int((to_utc_series(times) <= train_end_ts).sum())


def _apply_warmup(series: pd.Series, warmup_bars: int) -> pd.Series:
    if warmup_bars <= 0:
        return series
    trimmed = series.copy()
    trimmed.iloc[:warmup_bars] = np.nan
    return trimmed


def _build_classical_input_window(
    bars: pd.DataFrame, input_features: list[str]
) -> pd.DataFrame:
    """Standardized classical features consumed by the encoder."""
    df = bars.sort_values("time").reset_index(drop=True)
    columns: dict[str, np.ndarray] = {}
    for feature_name in input_features:
        spec = get_feature_spec(feature_name)
        computed = compute_feature(df, spec, {})
        columns[feature_name] = computed.series.to_numpy()
    return pd.DataFrame(columns, index=df["time"])


def _get_or_compute_latent_frame(model_hash: str, bars: pd.DataFrame) -> pd.DataFrame:
    cached = _cache_get(model_hash, bars)
    if cached is not None:
        return cached

    encoder = read_neural_model(model_hash)
    input_window = _build_classical_input_window(
        bars, list(encoder.config.input_features)
    )
    finite_mask = input_window.notna().all(axis=1)
    latent_frame = pd.DataFrame(
        np.nan,
        index=input_window.index,
        columns=encoder.latent_names,
        dtype=float,
    )
    if finite_mask.any():
        encoded = encoder.transform(input_window.loc[finite_mask])
        latent_frame.loc[finite_mask, encoded.columns] = encoded.to_numpy()
    _cache_put(model_hash, bars, latent_frame)
    return latent_frame


def _compute_neural(bars: pd.DataFrame, spec: FeatureSpec) -> FeatureSeries:
    if spec.model_hash is None:
        raise ValueError(f"Neural feature '{spec.name}' is missing model_hash.")

    df = bars.sort_values("time").reset_index(drop=True)
    encoder = read_neural_model(spec.model_hash)
    train_end = encoder.config.train_end

    latent_frame = _get_or_compute_latent_frame(spec.model_hash, df)
    if spec.name not in latent_frame.columns:
        raise KeyError(
            f"Latent column '{spec.name}' not found for model '{spec.model_hash}'."
        )

    raw = latent_frame[spec.name].reset_index(drop=True)
    warmup = _oos_warmup_bars(df["time"], train_end)
    values = _apply_warmup(raw, warmup)
    series = pd.Series(values.to_numpy(), index=df["time"], name=spec.name)

    assert_neural_oos_only(series.reset_index(drop=True), df["time"], train_end)
    leakage_status = neural_leakage_status(df["time"], series, train_end)

    return FeatureSeries(
        feature_id=feature_id(spec, {}),
        series=series,
        warmup_bars=warmup,
        leakage_status=leakage_status,
    )


def _output_port(feature_name: str) -> str:
    try:
        return _FEATURE_OUTPUT_PORT[feature_name]
    except KeyError as exc:
        raise KeyError(f"No output port mapping for feature '{feature_name}'.") from exc


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
    if spec.source == "neural":
        return _compute_neural(bars, spec)

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
