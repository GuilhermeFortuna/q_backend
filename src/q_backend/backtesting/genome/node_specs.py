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
class NodeSpec:
    kind: str
    min_inputs: int
    max_inputs: int
    input_series_types: tuple[SeriesType, ...] | None
    output_ports: tuple[str, ...]
    port_types: dict[str, OutputType]
    allowed_param_keys: frozenset[str]


def _series_ports(*names: str, series_type: SeriesType = "price_series") -> dict[str, OutputType]:
    return {name: series_type for name in names}


NODE_SPECS: dict[str, NodeSpec] = {
    "source.close": NodeSpec("source.close", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.high": NodeSpec("source.high", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.low": NodeSpec("source.low", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.open": NodeSpec("source.open", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
    "source.volume": NodeSpec("source.volume", 0, 0, None, ("out",), {"out": "price_series"}, frozenset()),
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
