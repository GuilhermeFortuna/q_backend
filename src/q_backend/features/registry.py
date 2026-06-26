"""FeatureSpec catalog and resolution (WO127).

Features are causal bar→series recipes. Non-causal recipes with forward_window != 0
belong in the target/label module (WO132), never here — see design §1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from q_backend.backtesting.genome.node_specs import NODE_SPECS, OutputType
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS

FeatureCategory = str
LeakageStatus = str


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    category: FeatureCategory
    node_kind: str
    param_keys: frozenset[str]
    default_params: dict[str, Any]
    lookback_param: str | None
    forward_window: int
    output_type: OutputType
    leakage_status: LeakageStatus
    description: str


def _default_param_value(key: str) -> Any:
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
    ]
    return {spec.name: spec for spec in specs}


FEATURE_SPECS: dict[str, FeatureSpec] = _build_feature_specs()


def assert_catalog_consistent() -> None:
    """Validate param keys and the PIT forward-window contract for every catalog entry."""
    for spec in FEATURE_SPECS.values():
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
    payload = json.dumps(sorted(params.items()), separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{spec.name}.v{spec.version}.{digest}"


assert_catalog_consistent()

# Design §1: features never peek forward — non-zero forward_window is a target, not a feature.
assert all(
    spec.forward_window == 0 for spec in FEATURE_SPECS.values()
), "All FeatureSpec entries must have forward_window == 0."
