"""FeatureSpec catalog and resolution (WO127).

Features are causal bar→series recipes. Non-causal recipes with forward_window != 0
belong in the target/label module (WO132), never here — see design §1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from q_backend.backtesting.genome.node_specs import NODE_SPECS, OutputType
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS

if TYPE_CHECKING:
    from q_backend.storage.db.models import NeuralModelVersion

FeatureCategory = str
LeakageStatus = str
FeatureSource = str


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    category: FeatureCategory
    node_kind: str | None
    param_keys: frozenset[str]
    default_params: dict[str, Any]
    lookback_param: str | None
    forward_window: int
    output_type: OutputType
    leakage_status: LeakageStatus
    description: str
    source: FeatureSource = "classical"
    model_hash: str | None = None
    latent_index: int | None = None


def _default_param_value(key: str) -> Any:
    _string_defaults = {
        "session_open": "09:00",
        "session_close": "18:00",
        "window_from": "09:00",
        "window_to": "12:00",
    }
    if key in _string_defaults:
        return _string_defaults[key]
    if key == "symbol":
        return "WDO$"
    if key in GENOME_PARAM_BOUNDS:
        return GENOME_PARAM_BOUNDS[key].default
    if key == "window":
        return 63
    if key == "estimator":
        return "close_to_close"
    raise KeyError(f"No default registered for param key '{key}'")


def _default_params_for(
    node_kind: str, *, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    keys = NODE_SPECS[node_kind].allowed_param_keys
    params = {key: _default_param_value(key) for key in keys}
    if overrides:
        params.update(overrides)
    return params


def _make_spec(
    name: str,
    *,
    category: FeatureCategory,
    node_kind: str,
    lookback_param: str | None,
    output_type: OutputType,
    description: str,
    default_overrides: dict[str, Any] | None = None,
    source: FeatureSource = "classical",
) -> FeatureSpec:
    node = NODE_SPECS[node_kind]
    default_params = _default_params_for(node_kind, overrides=default_overrides)
    return FeatureSpec(
        name=name,
        version=1,
        category=category,
        node_kind=node_kind,
        param_keys=node.allowed_param_keys,
        default_params=default_params,
        lookback_param=lookback_param,
        forward_window=0,
        output_type=output_type,
        leakage_status="clean",
        description=description,
        source=source,
        model_hash=None,
        latent_index=None,
    )


def _build_feature_specs() -> dict[str, FeatureSpec]:
    specs = [
        _make_spec(
            "rsi",
            category="momentum",
            node_kind="ind.rsi",
            lookback_param="period",
            output_type="oscillator",
            description="Relative Strength Index on a price source.",
            default_overrides={"period": 14},
        ),
        _make_spec(
            "atr",
            category="volatility",
            node_kind="ind.atr",
            lookback_param="period",
            output_type="oscillator",
            description="Average True Range (Wilder) on OHLC bars.",
            default_overrides={"period": 14},
        ),
        _make_spec(
            "ma",
            category="trend",
            node_kind="ind.ma",
            lookback_param="period",
            output_type="price_series",
            description="Moving average of a price source.",
        ),
        _make_spec(
            "ema",
            category="trend",
            node_kind="ind.ema",
            lookback_param="period",
            output_type="price_series",
            description="Exponential moving average of a price source.",
        ),
        _make_spec(
            "macd",
            category="momentum",
            node_kind="ind.macd",
            lookback_param="slow_period",
            output_type="oscillator",
            description="MACD line (fast EMA minus slow EMA).",
        ),
        _make_spec(
            "macd_signal",
            category="momentum",
            node_kind="ind.macd",
            lookback_param="slow_period",
            output_type="oscillator",
            description="MACD signal line (EMA of the MACD line).",
        ),
        _make_spec(
            "macd_histogram",
            category="momentum",
            node_kind="ind.macd",
            lookback_param="slow_period",
            output_type="oscillator",
            description="MACD histogram (MACD line minus signal line).",
        ),
        _make_spec(
            "bollinger_upper",
            category="volatility",
            node_kind="ind.bollinger",
            lookback_param="period",
            output_type="price_series",
            description="Bollinger upper band.",
        ),
        _make_spec(
            "bollinger_middle",
            category="volatility",
            node_kind="ind.bollinger",
            lookback_param="period",
            output_type="price_series",
            description="Bollinger middle band (moving average).",
        ),
        _make_spec(
            "bollinger_lower",
            category="volatility",
            node_kind="ind.bollinger",
            lookback_param="period",
            output_type="price_series",
            description="Bollinger lower band.",
        ),
        _make_spec(
            "donchian_upper",
            category="trend",
            node_kind="ind.donchian",
            lookback_param="period",
            output_type="price_series",
            description="Donchian channel upper bound (rolling high).",
        ),
        _make_spec(
            "donchian_lower",
            category="trend",
            node_kind="ind.donchian",
            lookback_param="period",
            output_type="price_series",
            description="Donchian channel lower bound (rolling low).",
        ),
        _make_spec(
            "momentum",
            category="momentum",
            node_kind="ind.momentum",
            lookback_param="lookback_bars",
            output_type="oscillator",
            description="Simple price momentum (return over lookback bars).",
        ),
        _make_spec(
            "realized_vol",
            category="volatility",
            node_kind="ind.realized_vol",
            lookback_param="window",
            output_type="oscillator",
            description="Realized volatility (close-to-close or Yang–Zhang).",
        ),
        _make_spec(
            "tsmom",
            category="momentum",
            node_kind="ind.tsmom",
            lookback_param="lookback_bars",
            output_type="oscillator",
            description="Time-series momentum sign series at rebalance points.",
        ),
        _make_spec(
            "tsmom_volatility",
            category="volatility",
            node_kind="ind.tsmom",
            lookback_param="vol_window",
            output_type="oscillator",
            description="Volatility estimate paired with the TSMOM primitive.",
        ),
        _make_spec(
            "trend_blend",
            category="trend",
            node_kind="ind.trend_blend",
            lookback_param="lookback_3",
            output_type="oscillator",
            description="Blended sign-of-return trend signal across three lookbacks.",
        ),
        _make_spec(
            "trend_blend_volatility",
            category="volatility",
            node_kind="ind.trend_blend",
            lookback_param="vol_window",
            output_type="oscillator",
            description="Realized volatility paired with the trend-blend primitive.",
        ),
        _make_spec(
            "zscore",
            category="transform",
            node_kind="transform.zscore",
            lookback_param="window",
            output_type="oscillator",
            description="Rolling z-score of close.",
        ),
        _make_spec(
            "rank",
            category="transform",
            node_kind="transform.rank",
            lookback_param="window",
            output_type="oscillator",
            description="Rolling percentile rank of close within a trailing window.",
        ),
        _make_spec(
            "pct_change",
            category="transform",
            node_kind="transform.pct_change",
            lookback_param="change_bars",
            output_type="oscillator",
            description="Percent change of close over a positive lag.",
            default_overrides={"change_bars": 1},
        ),
        _make_spec(
            "clip",
            category="transform",
            node_kind="transform.clip",
            lookback_param=None,
            output_type="price_series",
            description="Clamp close to an optimizable numeric band.",
        ),
        _make_spec(
            "minutes_from_open",
            category="session",
            node_kind="feature.minutes_from_open",
            lookback_param=None,
            output_type="oscillator",
            description="Minutes since configured session open.",
            default_overrides={"session_open": "09:00"},
        ),
        _make_spec(
            "time_of_day",
            category="session",
            node_kind="feature.time_of_day",
            lookback_param=None,
            output_type="oscillator",
            description="Normalized intraday position within the session.",
            default_overrides={"session_open": "09:00", "session_close": "18:00"},
        ),
        _make_spec(
            "day_of_week",
            category="session",
            node_kind="feature.day_of_week",
            lookback_param=None,
            output_type="oscillator",
            description="Day-of-week index (Mon=0).",
        ),
        _make_spec(
            "month_of_year",
            category="session",
            node_kind="feature.month_of_year",
            lookback_param=None,
            output_type="oscillator",
            description="Calendar month (1-12).",
        ),
        _make_spec(
            "session_window",
            category="session",
            node_kind="feature.session_window",
            lookback_param=None,
            output_type="bool_series",
            description="True inside a configurable intraday window.",
            default_overrides={"window_from": "10:00", "window_to": "12:00"},
        ),
        _make_spec(
            "vol_regime",
            category="regime",
            node_kind="feature.vol_regime",
            lookback_param="regime_lookback",
            output_type="oscillator",
            description="Rolling percentile of realized volatility.",
        ),
        _make_spec(
            "trend_regime",
            category="regime",
            node_kind="feature.trend_regime",
            lookback_param="ma_period",
            output_type="oscillator",
            description="Distance from MA normalized by recent volatility.",
        ),
        _make_spec(
            "range_compression",
            category="regime",
            node_kind="feature.range_compression",
            lookback_param="regime_lookback",
            output_type="oscillator",
            description="Rolling percentile of intrabar range relative to ATR.",
        ),
        _make_spec(
            "prev_session_high",
            category="session",
            node_kind="feature.prev_session_high",
            lookback_param=None,
            output_type="price_series",
            description="Previous completed session high.",
        ),
        _make_spec(
            "prev_session_low",
            category="session",
            node_kind="feature.prev_session_low",
            lookback_param=None,
            output_type="price_series",
            description="Previous completed session low.",
        ),
        _make_spec(
            "prev_session_close",
            category="session",
            node_kind="feature.prev_session_close",
            lookback_param=None,
            output_type="price_series",
            description="Previous completed session close.",
        ),
        _make_spec(
            "session_gap",
            category="session",
            node_kind="feature.session_gap",
            lookback_param=None,
            output_type="oscillator",
            description="Overnight gap versus previous session close.",
        ),
        _make_spec(
            "dist_prev_session_high_atr",
            category="session",
            node_kind="feature.dist_prev_session_high_atr",
            lookback_param="atr_period",
            output_type="oscillator",
            description="Distance from previous session high in ATR units.",
        ),
        _make_spec(
            "dist_prev_session_low_atr",
            category="session",
            node_kind="feature.dist_prev_session_low_atr",
            lookback_param="atr_period",
            output_type="oscillator",
            description="Distance from previous session low in ATR units.",
        ),
        _make_spec(
            "dist_prev_session_close_atr",
            category="session",
            node_kind="feature.dist_prev_session_close_atr",
            lookback_param="atr_period",
            output_type="oscillator",
            description="Distance from previous session close in ATR units.",
        ),
        _make_spec(
            "opening_range_high",
            category="session",
            node_kind="feature.opening_range_high",
            lookback_param="range_minutes",
            output_type="price_series",
            description="Completed opening-range high.",
            default_overrides={"session_open": "09:00", "range_minutes": 60},
        ),
        _make_spec(
            "opening_range_low",
            category="session",
            node_kind="feature.opening_range_low",
            lookback_param="range_minutes",
            output_type="price_series",
            description="Completed opening-range low.",
            default_overrides={"session_open": "09:00", "range_minutes": 60},
        ),
        _make_spec(
            "d1_prev_high",
            category="htf",
            node_kind="feature.d1_prev_high",
            lookback_param=None,
            output_type="price_series",
            description="Previous completed daily high aligned onto intraday bars.",
        ),
        _make_spec(
            "d1_prev_low",
            category="htf",
            node_kind="feature.d1_prev_low",
            lookback_param=None,
            output_type="price_series",
            description="Previous completed daily low aligned onto intraday bars.",
        ),
        _make_spec(
            "d1_prev_close",
            category="htf",
            node_kind="feature.d1_prev_close",
            lookback_param=None,
            output_type="price_series",
            description="Previous completed daily close aligned onto intraday bars.",
        ),
        _make_spec(
            "d1_trend",
            category="htf",
            node_kind="feature.d1_trend",
            lookback_param=None,
            output_type="oscillator",
            description="Signed momentum of the previous completed daily bar.",
        ),
        _make_spec(
            "d1_volatility",
            category="htf",
            node_kind="feature.d1_volatility",
            lookback_param=None,
            output_type="oscillator",
            description="Realized volatility of the previous completed daily bar.",
        ),
        _make_spec(
            "exog_close",
            category="exogenous",
            node_kind="source.exog.close",
            lookback_param=None,
            output_type="price_series",
            description="Backward-aligned exogenous close (requires attached exog columns).",
            source="exogenous",
        ),
        _make_spec(
            "exog_return",
            category="exogenous",
            node_kind="source.exog.return",
            lookback_param="lookback_bars",
            output_type="oscillator",
            description="Exogenous return over a bounded lookback.",
            default_overrides={"lookback_bars": 8},
            source="exogenous",
        ),
        _make_spec(
            "exog_return_zscore",
            category="exogenous",
            node_kind="source.exog.return_zscore",
            lookback_param="window",
            output_type="oscillator",
            description="Rolling z-score of exogenous returns.",
            default_overrides={"lookback_bars": 8, "window": 20},
            source="exogenous",
        ),
        _make_spec(
            "exog_rolling_corr",
            category="exogenous",
            node_kind="source.exog.rolling_corr",
            lookback_param="window",
            output_type="oscillator",
            description="Rolling correlation between primary and exogenous returns.",
            default_overrides={"window": 20},
            source="exogenous",
        ),
        _make_spec(
            "exog_relative_strength",
            category="exogenous",
            node_kind="source.exog.relative_strength",
            lookback_param="lookback_bars",
            output_type="oscillator",
            description="Primary minus exogenous return spread.",
            default_overrides={"lookback_bars": 8},
            source="exogenous",
        ),
        _make_spec(
            "exog_vol_regime",
            category="exogenous",
            node_kind="source.exog.vol_regime",
            lookback_param="vol_window",
            output_type="bool_series",
            description="Exogenous volatility regime gate.",
            default_overrides={"vol_window": 20},
            source="exogenous",
        ),
        _make_spec(
            "exog_direction_regime",
            category="exogenous",
            node_kind="source.exog.direction_regime",
            lookback_param="lookback_bars",
            output_type="bool_series",
            description="Exogenous direction regime gate.",
            default_overrides={"lookback_bars": 8},
            source="exogenous",
        ),
    ]
    return {spec.name: spec for spec in specs}


FEATURE_SPECS: dict[str, FeatureSpec] = _build_feature_specs()


def neural_catalog_key(latent_name: str, model_hash: str) -> str:
    """Stable catalog key so latents from different models never collide."""
    return f"{latent_name}@{model_hash[:8]}"


def register_neural_model_features(version: NeuralModelVersion) -> list[str]:
    """Register one neural ``FeatureSpec`` per latent on a trained model version."""
    keys: list[str] = []
    for index, latent_name in enumerate(version.latent_names, start=1):
        key = neural_catalog_key(latent_name, version.model_hash)
        FEATURE_SPECS[key] = FeatureSpec(
            name=latent_name,
            version=version.version,
            category="neural",
            node_kind=None,
            param_keys=frozenset(),
            default_params={},
            lookback_param=None,
            forward_window=0,
            output_type="oscillator",
            leakage_status="clean",
            description=(
                f"Neural latent {latent_name} from model {version.model_hash[:8]}."
            ),
            source="neural",
            model_hash=version.model_hash,
            latent_index=index,
        )
        keys.append(key)
    return keys


def unregister_neural_model_features(catalog_keys: list[str]) -> None:
    """Remove neural catalog entries (test helper)."""
    for key in catalog_keys:
        FEATURE_SPECS.pop(key, None)


def assert_catalog_consistent() -> None:
    """Validate param keys and the PIT forward-window contract for every catalog entry."""
    for catalog_key, spec in FEATURE_SPECS.items():
        if spec.source == "neural":
            if not spec.model_hash:
                raise AssertionError(
                    f"Neural feature '{catalog_key}' is missing model_hash."
                )
            if spec.latent_index is None:
                raise AssertionError(
                    f"Neural feature '{catalog_key}' is missing latent_index."
                )
            if spec.forward_window != 0:
                raise AssertionError(
                    f"Neural feature '{catalog_key}' has forward_window="
                    f"{spec.forward_window}; features must be causal."
                )
            if spec.node_kind is not None:
                raise AssertionError(
                    f"Neural feature '{catalog_key}' must have node_kind=None."
                )
            continue

        node = NODE_SPECS.get(spec.node_kind)
        if node is None:
            raise AssertionError(
                f"Feature '{spec.name}' references unknown node kind '{spec.node_kind}'."
            )
        if spec.param_keys != node.allowed_param_keys:
            raise AssertionError(
                f"Feature '{spec.name}' param_keys {sorted(spec.param_keys)} != "
                f"NODE_SPECS[{spec.node_kind!r}].allowed_param_keys {sorted(node.allowed_param_keys)}."
            )
        if set(spec.default_params) - spec.param_keys:
            raise AssertionError(
                f"Feature '{spec.name}' default_params has keys outside param_keys: "
                f"{sorted(set(spec.default_params) - spec.param_keys)}."
            )
        if spec.forward_window != 0:
            raise AssertionError(
                f"Feature '{spec.name}' has forward_window={spec.forward_window}; "
                "features must be causal (forward_window == 0). Targets live in WO132."
            )


def list_feature_specs() -> list[FeatureSpec]:
    return [FEATURE_SPECS[name] for name in sorted(FEATURE_SPECS)]


def list_categories() -> list[str]:
    return sorted({spec.category for spec in FEATURE_SPECS.values()})


def get_feature_spec(name: str, version: int | None = None) -> FeatureSpec:
    try:
        spec = FEATURE_SPECS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown feature spec '{name}'.") from exc
    if version is not None and spec.version != version:
        raise KeyError(f"Feature '{name}' has no version {version}.")
    return spec


def resolve_params(spec: FeatureSpec, overrides: dict[str, Any]) -> dict[str, Any]:
    unknown = set(overrides) - spec.param_keys
    if unknown:
        raise ValueError(f"Unknown params for feature '{spec.name}': {sorted(unknown)}")
    merged = dict(spec.default_params)
    merged.update(overrides)
    return merged


def feature_id(spec: FeatureSpec, params: dict[str, Any]) -> str:
    if spec.source == "neural":
        identity = {
            "model_hash": spec.model_hash,
            "params": sorted(params.items()),
        }
        payload = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    else:
        payload = json.dumps(sorted(params.items()), separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{spec.name}.v{spec.version}.{digest}"


assert_catalog_consistent()

# Design §1: features never peek forward — non-zero forward_window is a target, not a feature.
assert all(
    spec.forward_window == 0 for spec in FEATURE_SPECS.values()
), "All FeatureSpec entries must have forward_window == 0."
