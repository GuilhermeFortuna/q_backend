"""Generate q_capabilities.v1 from existing backend registries (no hand-maintained drift)."""

from __future__ import annotations

from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.exit_rules.registry import EXIT_RULES, list_exit_rules
from q_backend.backtesting.genome.node_specs import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODE_COUNT,
    NODE_SPECS,
)
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.strategy_registry import (
    StrategyEngine,
    StrategyInfo,
    _CUSTOM_STRATEGIES,
    list_registered_strategies,
)
from q_backend.market_data.clients.metatrader import COLUMNAR_TICK_KEYS, TIMEFRAME_NAMES
from q_backend.market_data.local_store import _OHLCV_COLUMNS
from q_backend.storage.runtime_config import _VALID_SOURCES
from q_backend.strategy_builder.capability_models import (
    SCHEMA_VERSION,
    CapabilityRegistry,
    DataCapabilities,
    ExecutionAssumptions,
    GenomeLimits,
    GenomeNodeCapability,
    RiskSizingCapability,
)

# User-facing StrategySpec operators mapped from genome compare node kinds.
_CMP_KIND_TO_OPERATOR: dict[str, str] = {
    "cmp.gt": ">",
    "cmp.lt": "<",
    "cmp.gte": ">=",
    "cmp.lte": "<=",
    "cmp.cross_above": "crosses_above",
    "cmp.cross_below": "crosses_below",
    "cmp.touch_below": "touch_below",
    "cmp.touch_above": "touch_above",
    "cmp.trb_breakout_above": "trb_breakout_above",
    "cmp.trb_breakout_below": "trb_breakout_below",
}

CONDITION_GROUPS = ("all", "any")

UNSUPPORTED_CAPABILITIES: tuple[str, ...] = (
    "arbitrary_python_execution",
    "broker_routing",
    "fundamental_data",
    "intrabar_fill_simulation",
    "live_order_execution",
    "options_greeks",
    "order_book_depth",
)

_RISK_SIZING: tuple[RiskSizingCapability, ...] = (
    RiskSizingCapability(
        type="fixed_quantity",
        label="Fixed quantity",
        description="Trade a fixed number of contracts/shares on each signal.",
    ),
    RiskSizingCapability(
        type="fixed_safety_margin",
        label="Fixed safety margin",
        description="Size positions from available capital and a per-contract safety margin.",
    ),
    RiskSizingCapability(
        type="inverse_volatility",
        label="Inverse volatility",
        description="Scale position size inversely to recent realized volatility.",
    ),
)

_SUPPORTED_MARKETS = ("B3",)


def _builtin_strategies() -> list[StrategyInfo]:
    return sorted(
        (info for info in list_registered_strategies() if info.name not in _CUSTOM_STRATEGIES),
        key=lambda info: info.name,
    )


def _supported_engines(strategies: list[StrategyInfo]) -> list[StrategyEngine]:
    engines = sorted({info.engine for info in strategies})
    return engines  # type: ignore[return-value]


def _genome_nodes() -> list[GenomeNodeCapability]:
    nodes: list[GenomeNodeCapability] = []
    for kind in sorted(NODE_SPECS):
        spec = NODE_SPECS[kind]
        input_types: list[str] | None
        if spec.input_series_types is None:
            input_types = None
        else:
            input_types = list(spec.input_series_types)
        nodes.append(
            GenomeNodeCapability(
                kind=kind,
                min_inputs=spec.min_inputs,
                max_inputs=spec.max_inputs,
                input_series_types=input_types,  # type: ignore[arg-type]
                output_ports=list(spec.output_ports),
                port_types=dict(sorted(spec.port_types.items())),
                allowed_param_keys=sorted(spec.allowed_param_keys),
            )
        )
    return nodes


def _operators() -> list[str]:
    return sorted({operator for kind, operator in _CMP_KIND_TO_OPERATOR.items() if kind in NODE_SPECS})


def _ohlcv_columns() -> list[str]:
    return sorted(col for col in _OHLCV_COLUMNS if col != "time")


def _tick_columns() -> list[str]:
    return sorted(COLUMNAR_TICK_KEYS)


def _allow_short(strategies: list[StrategyInfo]) -> bool:
    short_capable = {
        "FMA",
        "MACD",
        "RSIMeanReversion",
        "TRB",
        "TSMOM",
        "VMA",
        "CompositeStrategy",
        "GatevPairs",
    }
    return any(info.name in short_capable for info in strategies)


def build_capability_registry() -> CapabilityRegistry:
    """Build a deterministic capability document from live backend registries."""
    strategies = _builtin_strategies()
    return CapabilityRegistry(
        schema_version=SCHEMA_VERSION,
        data=DataCapabilities(
            markets=list(_SUPPORTED_MARKETS),
            engines=_supported_engines(strategies),
            timeframes=list(TIMEFRAME_NAMES),
            ohlcv_columns=_ohlcv_columns(),
            tick_columns=_tick_columns(),
            data_sources=sorted(_VALID_SOURCES),
        ),
        strategies=strategies,
        genome_nodes=_genome_nodes(),
        genome_param_bounds=[GENOME_PARAM_BOUNDS[key] for key in sorted(GENOME_PARAM_BOUNDS)],
        genome_limits=GenomeLimits(
            max_depth=DEFAULT_MAX_DEPTH,
            max_node_count=DEFAULT_MAX_NODE_COUNT,
        ),
        operators=_operators(),
        condition_groups=list(CONDITION_GROUPS),
        exit_rules=list_exit_rules(),
        exit_presets=sorted(EXIT_PRESETS, key=lambda preset: preset.id),
        risk_sizing=list(_RISK_SIZING),
        execution_assumptions=ExecutionAssumptions(
            supported_signal_timing=["closed_bar"],
            supported_entry_timing=["next_bar_open"],
            allow_short=_allow_short(strategies),
            ai_builder_mvp_long_only=True,
        ),
        unsupported=list(UNSUPPORTED_CAPABILITIES),
    )


def registered_genome_node_kinds() -> frozenset[str]:
    return frozenset(NODE_SPECS.keys())


def registered_exit_rule_ids() -> frozenset[str]:
    return frozenset(rule.id for rule in EXIT_RULES)
