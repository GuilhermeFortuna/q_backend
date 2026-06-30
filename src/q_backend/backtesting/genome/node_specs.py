"""v1 primitive allowlist with arity and output-type tags (design §2.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OutputType = Literal[
    "price_series",
    "oscillator",
    "bool_series",
    "exit_policy",
]

SeriesType = Literal["price_series", "oscillator"]


@dataclass(frozen=True)
class NodeGenMetadata:
    """Effective GA generation flags for one node kind (WO158)."""

    category: str
    random_init: bool
    add_node: bool
    swap: bool


@dataclass(frozen=True)
class NodeSpec:
    kind: str
    min_inputs: int
    max_inputs: int
    input_series_types: tuple[SeriesType, ...] | None
    output_ports: tuple[str, ...]
    port_types: dict[str, OutputType]
    allowed_param_keys: frozenset[str]
    gen_category: str | None = None
    gen_random_init: bool | None = None
    gen_add_node: bool | None = None
    gen_swap: bool | None = None


_SWAP_EXCLUDED_INDICATORS = frozenset({"ind.diff", "ind.ratio", "ind.tsmom", "ind.latent"})
_ADD_NODE_INDICATORS = frozenset({"ind.ma", "ind.ema", "ind.rsi"})
_WO158_TRANSFORM_KINDS = frozenset(
    {
        "transform.zscore",
        "transform.rank",
        "transform.pct_change",
        "transform.clip",
    }
)
_WO159_CONTEXT_FEATURE_KINDS = frozenset(
    {
        "feature.minutes_from_open",
        "feature.time_of_day",
        "feature.day_of_week",
        "feature.month_of_year",
        "feature.session_window",
        "feature.vol_regime",
        "feature.trend_regime",
        "feature.range_compression",
        "feature.prev_session_high",
        "feature.prev_session_low",
        "feature.prev_session_close",
        "feature.session_gap",
        "feature.dist_prev_session_high_atr",
        "feature.dist_prev_session_low_atr",
        "feature.dist_prev_session_close_atr",
        "feature.opening_range_high",
        "feature.opening_range_low",
        "feature.d1_prev_high",
        "feature.d1_prev_low",
        "feature.d1_prev_close",
        "feature.d1_trend",
        "feature.d1_volatility",
    }
)


def _derive_gen_category(kind: str) -> str:
    prefix = kind.split(".", 1)[0]
    return {
        "source": "source",
        "ind": "indicator",
        "transform": "transform",
        "feature": "feature",
        "cmp": "cmp",
        "logic": "logic",
        "exit": "exit",
    }.get(prefix, "core")


def resolve_node_gen_metadata(spec: NodeSpec) -> NodeGenMetadata:
    """Return effective generation metadata (explicit fields override derivation)."""
    category = spec.gen_category if spec.gen_category is not None else _derive_gen_category(spec.kind)

    if spec.kind in _WO158_TRANSFORM_KINDS:
        return NodeGenMetadata(
            category=spec.gen_category or "transform",
            random_init=spec.gen_random_init if spec.gen_random_init is not None else True,
            add_node=spec.gen_add_node if spec.gen_add_node is not None else True,
            swap=spec.gen_swap if spec.gen_swap is not None else True,
        )

    if spec.kind in _WO159_CONTEXT_FEATURE_KINDS:
        swap_default = spec.kind != "feature.session_window"
        return NodeGenMetadata(
            category=spec.gen_category or "feature",
            random_init=spec.gen_random_init if spec.gen_random_init is not None else True,
            add_node=spec.gen_add_node if spec.gen_add_node is not None else True,
            swap=spec.gen_swap if spec.gen_swap is not None else swap_default,
        )

    if spec.kind.startswith("feature."):
        return NodeGenMetadata(
            category=category,
            random_init=spec.gen_random_init if spec.gen_random_init is not None else False,
            add_node=spec.gen_add_node if spec.gen_add_node is not None else False,
            swap=spec.gen_swap if spec.gen_swap is not None else False,
        )

    if spec.kind.startswith("ind."):
        swap = spec.gen_swap
        if swap is None:
            swap = spec.kind not in _SWAP_EXCLUDED_INDICATORS
        add_node = spec.gen_add_node
        if add_node is None:
            add_node = spec.kind in _ADD_NODE_INDICATORS
        random_init = spec.gen_random_init if spec.gen_random_init is not None else False
        return NodeGenMetadata(
            category=category,
            random_init=random_init,
            add_node=add_node,
            swap=swap,
        )

    if spec.kind.startswith("transform."):
        random_init = spec.gen_random_init if spec.gen_random_init is not None else False
        add_node = spec.gen_add_node if spec.gen_add_node is not None else False
        swap = spec.gen_swap if spec.gen_swap is not None else False
        return NodeGenMetadata(
            category=category,
            random_init=random_init,
            add_node=add_node,
            swap=swap,
        )

    return NodeGenMetadata(
        category=category,
        random_init=spec.gen_random_init if spec.gen_random_init is not None else False,
        add_node=spec.gen_add_node if spec.gen_add_node is not None else False,
        swap=spec.gen_swap if spec.gen_swap is not None else False,
    )


def base_indicator_kinds() -> tuple[str, ...]:
    """Indicator kinds eligible for swap (excludes latent — added per-run by latent universe)."""
    return tuple(
        sorted(
            kind
            for kind, spec in NODE_SPECS.items()
            if kind.startswith("ind.")
            and resolve_node_gen_metadata(spec).swap
            and kind != "ind.latent"
        )
    )


def add_node_kinds() -> tuple[str, ...]:
    """Unary series producers eligible for ``add_node`` mutation."""
    return tuple(
        sorted(
            kind
            for kind, spec in NODE_SPECS.items()
            if resolve_node_gen_metadata(spec).add_node
        )
    )


def random_init_transform_kinds() -> tuple[str, ...]:
    """Transforms that may be inserted during random genome construction."""
    return tuple(
        sorted(
            kind
            for kind, spec in NODE_SPECS.items()
            if kind.startswith("transform.")
            and resolve_node_gen_metadata(spec).random_init
        )
    )


def swap_kinds(indicator_kinds: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Kinds eligible when swapping a unary series node (indicators + transforms + features)."""
    transform_kinds = tuple(
        sorted(
            kind
            for kind, spec in NODE_SPECS.items()
            if kind.startswith("transform.") and resolve_node_gen_metadata(spec).swap
        )
    )
    feature_kinds = tuple(
        sorted(
            kind
            for kind, spec in NODE_SPECS.items()
            if kind.startswith("feature.") and resolve_node_gen_metadata(spec).swap
        )
    )
    indicator_swap = tuple(
        sorted(kind for kind in indicator_kinds if kind in NODE_SPECS and kind.startswith("ind."))
    )
    return indicator_swap + transform_kinds + feature_kinds


def _series_ports(*names: str, series_type: SeriesType = "price_series") -> dict[str, OutputType]:
    return {name: series_type for name in names}


NODE_SPECS: dict[str, NodeSpec] = {
    "source.close": NodeSpec("source.close", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.high": NodeSpec("source.high", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.low": NodeSpec("source.low", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.open": NodeSpec("source.open", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.volume": NodeSpec("source.volume", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.exog.close": NodeSpec(
        "source.exog.close",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset({"symbol"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "source.exog.return": NodeSpec(
        "source.exog.return",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"symbol", "lookback_bars"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "source.exog.return_zscore": NodeSpec(
        "source.exog.return_zscore",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"symbol", "lookback_bars", "window"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "source.exog.rolling_corr": NodeSpec(
        "source.exog.rolling_corr",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"symbol", "window"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "source.exog.relative_strength": NodeSpec(
        "source.exog.relative_strength",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"symbol", "lookback_bars"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "source.exog.vol_regime": NodeSpec(
        "source.exog.vol_regime",
        0,
        0,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset({"symbol", "vol_window"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "source.exog.direction_regime": NodeSpec(
        "source.exog.direction_regime",
        0,
        0,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset({"symbol", "lookback_bars"}),
        gen_category="exogenous",
        gen_random_init=False,
        gen_add_node=False,
        gen_swap=False,
    ),
    "ind.ma": NodeSpec(
        "ind.ma",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "price_series"},
        frozenset({"period", "ma_type"}),
    ),
    "ind.ema": NodeSpec(
        "ind.ema",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "price_series"},
        frozenset({"period"}),
    ),
    "ind.rsi": NodeSpec(
        "ind.rsi",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "oscillator"},
        frozenset({"period"}),
    ),
    "ind.atr": NodeSpec(
        "ind.atr",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"period"}),
    ),
    "ind.macd": NodeSpec(
        "ind.macd",
        1,
        1,
        ("price_series",),
        ("macd", "macd_signal", "macd_histogram"),
        {
            "macd": "oscillator",
            "macd_signal": "oscillator",
            "macd_histogram": "oscillator",
        },
        frozenset({"fast_period", "slow_period", "signal_period"}),
    ),
    "ind.bollinger": NodeSpec(
        "ind.bollinger",
        1,
        1,
        ("price_series",),
        ("bb_upper", "bb_middle", "bb_lower"),
        {
            "bb_upper": "price_series",
            "bb_middle": "price_series",
            "bb_lower": "price_series",
        },
        frozenset({"period", "num_std"}),
    ),
    "ind.donchian": NodeSpec(
        "ind.donchian",
        0,
        0,
        None,
        ("donchian_upper", "donchian_lower"),
        {"donchian_upper": "price_series", "donchian_lower": "price_series"},
        frozenset({"period"}),
    ),
    "ind.momentum": NodeSpec(
        "ind.momentum",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "oscillator"},
        frozenset({"lookback_bars"}),
    ),
    "ind.realized_vol": NodeSpec(
        "ind.realized_vol",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "oscillator"},
        frozenset({"window", "estimator"}),
    ),
    "ind.diff": NodeSpec(
        "ind.diff",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "price_series"},
        frozenset(),
    ),
    "ind.ratio": NodeSpec(
        "ind.ratio",
        2,
        2,
        ("price_series", "oscillator"),
        ("out",),
        {"out": "oscillator"},
        frozenset(),
    ),
    "ind.trb_channel": NodeSpec(
        "ind.trb_channel",
        1,
        1,
        ("price_series",),
        ("channel_high", "channel_low", "trb_upper", "trb_lower"),
        {
            "channel_high": "price_series",
            "channel_low": "price_series",
            "trb_upper": "price_series",
            "trb_lower": "price_series",
        },
        frozenset({"period", "band_pct"}),
    ),
    "ind.ma_band": NodeSpec(
        "ind.ma_band",
        1,
        1,
        ("price_series",),
        ("ma_band_upper", "ma_band_lower"),
        {"ma_band_upper": "price_series", "ma_band_lower": "price_series"},
        frozenset({"band_pct"}),
    ),
    "ind.tsmom": NodeSpec(
        "ind.tsmom",
        0,
        0,
        None,
        ("buy_signal", "sell_signal", "momentum", "volatility"),
        {
            "buy_signal": "bool_series",
            "sell_signal": "bool_series",
            "momentum": "oscillator",
            "volatility": "oscillator",
        },
        frozenset({"lookback_bars", "rebalance_bars", "vol_window", "vol_estimator"}),
    ),
    "transform.shift": NodeSpec(
        "transform.shift",
        1,
        1,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset({"bars"}),
    ),
    "transform.abs": NodeSpec(
        "transform.abs",
        1,
        1,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
    ),
    "transform.scale": NodeSpec(
        "transform.scale",
        1,
        1,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset({"factor"}),
    ),
    "transform.zscore": NodeSpec(
        "transform.zscore",
        1,
        1,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"window"}),
        gen_category="transform",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "transform.rank": NodeSpec(
        "transform.rank",
        1,
        1,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"window"}),
        gen_category="transform",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "transform.pct_change": NodeSpec(
        "transform.pct_change",
        1,
        1,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"change_bars"}),
        gen_category="transform",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "transform.clip": NodeSpec(
        "transform.clip",
        1,
        1,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset({"clip_low", "clip_high"}),
        gen_category="transform",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.minutes_from_open": NodeSpec(
        "feature.minutes_from_open",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"session_open"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.time_of_day": NodeSpec(
        "feature.time_of_day",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"session_open", "session_close"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.day_of_week": NodeSpec(
        "feature.day_of_week",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.month_of_year": NodeSpec(
        "feature.month_of_year",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.session_window": NodeSpec(
        "feature.session_window",
        0,
        0,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset({"window_from", "window_to"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=False,
    ),
    "feature.vol_regime": NodeSpec(
        "feature.vol_regime",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "oscillator"},
        frozenset({"window", "regime_lookback"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.trend_regime": NodeSpec(
        "feature.trend_regime",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "oscillator"},
        frozenset({"ma_period"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.range_compression": NodeSpec(
        "feature.range_compression",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "oscillator"},
        frozenset({"window", "regime_lookback"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.prev_session_high": NodeSpec(
        "feature.prev_session_high",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.prev_session_low": NodeSpec(
        "feature.prev_session_low",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.prev_session_close": NodeSpec(
        "feature.prev_session_close",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.session_gap": NodeSpec(
        "feature.session_gap",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.dist_prev_session_high_atr": NodeSpec(
        "feature.dist_prev_session_high_atr",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"atr_period"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.dist_prev_session_low_atr": NodeSpec(
        "feature.dist_prev_session_low_atr",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"atr_period"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.dist_prev_session_close_atr": NodeSpec(
        "feature.dist_prev_session_close_atr",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"atr_period"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.opening_range_high": NodeSpec(
        "feature.opening_range_high",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset({"session_open", "range_minutes"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.opening_range_low": NodeSpec(
        "feature.opening_range_low",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset({"session_open", "range_minutes"}),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.d1_prev_high": NodeSpec(
        "feature.d1_prev_high",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.d1_prev_low": NodeSpec(
        "feature.d1_prev_low",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.d1_prev_close": NodeSpec(
        "feature.d1_prev_close",
        0,
        0,
        None,
        ("out",),
        {"out": "price_series"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.d1_trend": NodeSpec(
        "feature.d1_trend",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "feature.d1_volatility": NodeSpec(
        "feature.d1_volatility",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset(),
        gen_category="feature",
        gen_random_init=True,
        gen_add_node=True,
        gen_swap=True,
    ),
    "cmp.gt": NodeSpec(
        "cmp.gt",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.lt": NodeSpec(
        "cmp.lt",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.gte": NodeSpec(
        "cmp.gte",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.lte": NodeSpec(
        "cmp.lte",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.cross_above": NodeSpec(
        "cmp.cross_above",
        1,
        2,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset({"threshold", "negate"}),
    ),
    "cmp.cross_below": NodeSpec(
        "cmp.cross_below",
        1,
        2,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset({"threshold", "negate"}),
    ),
    "cmp.touch_below": NodeSpec(
        "cmp.touch_below",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.touch_above": NodeSpec(
        "cmp.touch_above",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.trb_breakout_above": NodeSpec(
        "cmp.trb_breakout_above",
        3,
        3,
        ("price_series", "price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "cmp.trb_breakout_below": NodeSpec(
        "cmp.trb_breakout_below",
        3,
        3,
        ("price_series", "price_series", "price_series"),
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "logic.and": NodeSpec(
        "logic.and",
        2,
        2,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "logic.or": NodeSpec(
        "logic.or",
        2,
        2,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "logic.not": NodeSpec(
        "logic.not",
        1,
        1,
        None,
        ("out",),
        {"out": "bool_series"},
        frozenset(),
    ),
    "exit.opposite_signal": NodeSpec(
        "exit.opposite_signal",
        0,
        0,
        None,
        ("out",),
        {"out": "exit_policy"},
        frozenset(),
    ),
    "exit.middle_band": NodeSpec(
        "exit.middle_band",
        2,
        2,
        ("price_series", "price_series"),
        ("exit_long", "exit_short"),
        {"exit_long": "bool_series", "exit_short": "bool_series"},
        frozenset(),
    ),
    "exit.fixed_holding": NodeSpec(
        "exit.fixed_holding",
        0,
        0,
        None,
        ("out",),
        {"out": "exit_policy"},
        frozenset({"holding_period"}),
    ),
    "exit.rebalance": NodeSpec(
        "exit.rebalance",
        0,
        0,
        None,
        ("out",),
        {"out": "exit_policy"},
        frozenset(),
    ),
    "ind.trend_blend": NodeSpec(
        "ind.trend_blend",
        1,
        1,
        ("price_series",),
        ("out", "volatility"),
        {"out": "oscillator", "volatility": "oscillator"},
        frozenset({"lookback_1", "lookback_2", "lookback_3", "vol_window"}),
    ),
    "ind.latent": NodeSpec(
        "ind.latent",
        0,
        0,
        None,
        ("out",),
        {"out": "oscillator"},
        frozenset({"latent_index"}),
    ),
}

ALLOWED_NODE_KINDS = frozenset(NODE_SPECS.keys())

DEFAULT_MAX_DEPTH = 12
DEFAULT_MAX_NODE_COUNT = 24


def parse_input_ref(raw: str) -> tuple[str, str]:
    if ":" in raw:
        node_id, port = raw.split(":", 1)
        return node_id, port
    return raw, "out"


def port_output_type(kind: str, port: str) -> OutputType:
    spec = NODE_SPECS[kind]
    if port not in spec.port_types:
        raise KeyError(port)
    return spec.port_types[port]
