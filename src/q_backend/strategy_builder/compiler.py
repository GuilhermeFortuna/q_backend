"""Deterministic StrategySpec → runnable strategy compiler (WO92)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from q_backend.backtesting.genome.exit_rule_policy import (
    EXIT_RULE_POLICY_METADATA_KEY,
    build_exit_rule_policy,
    build_single_stop_exit_policy,
    genome_exit_param_ref,
)
from q_backend.backtesting.genome.registry_fixtures import (
    MA_CROSSOVER_GENOME,
    RSI_MEAN_REVERSION_GENOME,
)
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import GenomeValidationError, validate_genome
from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.strategy_registry import ExitPreset
from q_backend.strategy_builder.compiler_models import (
    BacktestConfigPayload,
    CompiledStrategy,
    CompiledStrategySummary,
    StrategyCompileError,
)
from q_backend.strategy_builder.spec_models import (
    SCHEMA_VERSION,
    ComparisonCondition,
    ConditionGroup,
    ConditionLeaf,
    ExitStopLossCondition,
    ExitTakeProfitCondition,
    IndicatorSpec,
    StrategySpec,
    ValidationErrorDetail,
)
from q_backend.strategy_builder.validator import validate_strategy_spec

_OPERATOR_TO_CMP_KIND: dict[str, str] = {
    ">": "cmp.gt",
    "<": "cmp.lt",
    ">=": "cmp.gte",
    "<=": "cmp.lte",
    "crosses_above": "cmp.cross_above",
    "crosses_below": "cmp.cross_below",
}

_SOURCE_KIND_BY_COLUMN: dict[str, str] = {
    "open": "source.open",
    "high": "source.high",
    "low": "source.low",
    "close": "source.close",
    "volume": "source.volume",
}

_INDICATOR_OUTPUT_PORTS: dict[str, dict[str, str]] = {
    "sma": {"": "out"},
    "ema": {"": "out"},
    "rsi": {"": "out"},
    "atr": {"": "out"},
    "donchian": {"upper": "donchian_upper", "lower": "donchian_lower"},
    "bollinger_bands": {
        "upper": "bb_upper",
        "middle": "bb_middle",
        "lower": "bb_lower",
    },
}


def compile_strategy_spec(spec: StrategySpec) -> CompiledStrategy:
    validation = validate_strategy_spec(spec)
    if not validation.valid:
        raise StrategyCompileError(
            "StrategySpec failed validation.",
            errors=validation.errors,
            code="validation_failed",
            status="validation_failed",
        )

    registry_compiled = _try_compile_registry_strategy(spec)
    if registry_compiled is not None:
        return registry_compiled

    return _compile_composite_strategy(spec)


def compile_strategy_spec_payload(payload: dict[str, Any]) -> CompiledStrategy:
    from q_backend.strategy_builder.validator import validate_strategy_spec_payload

    validation = validate_strategy_spec_payload(payload)
    if not validation.valid:
        raise StrategyCompileError(
            "StrategySpec failed validation.",
            errors=validation.errors,
            code="validation_failed",
            status="validation_failed",
        )
    spec = StrategySpec.model_validate(payload)
    return compile_strategy_spec(spec)


def _try_compile_registry_strategy(spec: StrategySpec) -> CompiledStrategy | None:
    exit_rules = _exit_rule_conditions(spec.exit)
    if exit_rules:
        return None

    ma = _match_ma_crossover(spec)
    if ma is not None:
        return ma

    rsi = _match_rsi_mean_reversion(spec)
    if rsi is not None:
        return rsi

    return None


def _match_ma_crossover(spec: StrategySpec) -> CompiledStrategy | None:
    if len(spec.indicators) != 2:
        return None
    if spec.indicators[0].type not in {"sma", "ema"} or spec.indicators[1].type not in {
        "sma",
        "ema",
    }:
        return None
    if spec.indicators[0].source != "close" or spec.indicators[1].source != "close":
        return None

    entry_conditions = _comparison_conditions(spec.entry)
    exit_conditions = _comparison_conditions(spec.exit)
    if len(entry_conditions) != 1 or len(exit_conditions) != 1:
        return None

    entry = entry_conditions[0]
    exit_cond = exit_conditions[0]
    if entry.op != "crosses_above" or exit_cond.op != "crosses_below":
        return None
    if not _refs_equal(entry.left, spec.indicators[0].id):
        return None
    if not _refs_equal(entry.right, spec.indicators[1].id):
        return None
    if not _refs_equal(exit_cond.left, spec.indicators[0].id):
        return None
    if not _refs_equal(exit_cond.right, spec.indicators[1].id):
        return None

    fast, slow = spec.indicators
    if fast.period >= slow.period:
        fast, slow = slow, fast

    strategy_params = {
        "short_period": fast.period,
        "long_period": slow.period,
        "short_ma_type": fast.type,
        "long_ma_type": slow.type,
        "threshold": 0.0,
    }
    genome = _clone_genome_with_id(MA_CROSSOVER_GENOME, spec.name)
    return _build_compiled(
        spec=spec,
        strategy_name="MACrossover",
        strategy_params=strategy_params,
        genome=genome,
        mapping="MACrossover",
        strategy_label="MA Crossover",
        entry_summary=f"{fast.id} crosses above {slow.id}",
        exit_summary=f"{fast.id} crosses below {slow.id}",
    )


def _match_rsi_mean_reversion(spec: StrategySpec) -> CompiledStrategy | None:
    if len(spec.indicators) != 1 or spec.indicators[0].type != "rsi":
        return None

    entry_conditions = _comparison_conditions(spec.entry)
    exit_conditions = _comparison_conditions(spec.exit)
    if len(entry_conditions) != 1 or len(exit_conditions) != 1:
        return None

    entry = entry_conditions[0]
    exit_cond = exit_conditions[0]
    if entry.op != "crosses_above" or exit_cond.op != "crosses_above":
        return None
    if not _refs_equal(entry.left, spec.indicators[0].id):
        return None
    if not _refs_equal(exit_cond.left, spec.indicators[0].id):
        return None
    if not isinstance(entry.right, (int, float)) or not isinstance(
        exit_cond.right, (int, float)
    ):
        return None

    strategy_params = {
        "period": spec.indicators[0].period,
        "oversold": float(entry.right),
        "overbought": float(exit_cond.right),
    }
    genome = _clone_genome_with_id(RSI_MEAN_REVERSION_GENOME, spec.name)
    return _build_compiled(
        spec=spec,
        strategy_name="RSIMeanReversion",
        strategy_params=strategy_params,
        genome=genome,
        mapping="RSIMeanReversion",
        strategy_label="RSI Mean Reversion",
        entry_summary=f"RSI crosses above {entry.right}",
        exit_summary=f"RSI crosses above {exit_cond.right}",
    )


def _compile_composite_strategy(spec: StrategySpec) -> CompiledStrategy:
    builder = _GenomeBuilder(spec)
    genome_dict = builder.build()
    genome = Genome.model_validate(genome_dict)
    try:
        validate_genome(genome)
    except GenomeValidationError as exc:
        raise StrategyCompileError(str(exc)) from exc

    strategy_params = {"genome": genome_dict, **builder.strategy_params}
    return _build_compiled(
        spec=spec,
        strategy_name="CompositeStrategy",
        strategy_params=strategy_params,
        genome=genome_dict,
        mapping="CompositeStrategy",
        strategy_label="Composite strategy",
        entry_summary=builder.entry_summary,
        exit_summary=builder.exit_summary,
    )


def _build_compiled(
    *,
    spec: StrategySpec,
    strategy_name: str,
    strategy_params: dict[str, Any],
    genome: dict[str, Any] | None,
    mapping: str,
    strategy_label: str,
    entry_summary: str,
    exit_summary: str,
) -> CompiledStrategy:
    identity_payload = {
        "schema_version": spec.schema_version,
        "strategy_name": strategy_name,
        "strategy_params": strategy_params,
        "genome": genome,
    }
    compiled_id = _compiled_id(identity_payload)
    summary = CompiledStrategySummary(
        name=spec.name,
        strategy_label=strategy_label,
        mapping=mapping,
        indicators=[f"{item.id} ({item.type})" for item in spec.indicators],
        entry_summary=entry_summary,
        exit_summary=exit_summary,
        universe=list(spec.universe),
        timeframe=spec.timeframe,
        market=spec.market,
    )
    backtest_config = BacktestConfigPayload(
        symbol=spec.universe[0],
        timeframe=spec.timeframe,
        strategy=strategy_name,
        strategy_params=strategy_params,
        position_sizing=_position_sizing_payload(spec),
        execution_assumptions=spec.execution_assumptions.model_dump(mode="json"),
    )
    return CompiledStrategy(
        compiled_id=compiled_id,
        schema_version=spec.schema_version,
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        genome=genome,
        summary=summary,
        backtest_config=backtest_config,
    )


def _compiled_id(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"compiled_{digest}"


def _position_sizing_payload(spec: StrategySpec) -> dict[str, Any]:
    risk = spec.risk
    if risk.position_sizing == "fixed_quantity":
        return {"type": "fixed_quantity", "quantity": risk.quantity}
    return {
        "type": "fixed_safety_margin",
        "safety_margin_per_contract": risk.safety_margin_per_contract,
    }


def _clone_genome_with_id(template: dict[str, Any], spec_name: str) -> dict[str, Any]:
    genome = json.loads(json.dumps(template))
    genome["genome_id"] = _slugify(spec_name)
    return genome


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "strategy"


class _GenomeBuilder:
    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec
        self.nodes: list[dict[str, Any]] = []
        self.strategy_params: dict[str, Any] = {}
        self._node_counter = 0
        self._indicator_nodes: dict[str, str] = {}
        self._source_nodes: dict[str, str] = {}
        self._indicators_by_id = {item.id: item for item in spec.indicators}
        self.entry_summary = ""
        self.exit_summary = ""

    def build(self) -> dict[str, Any]:
        self._build_indicators()
        entry_ref = self._compile_condition_group(self.spec.entry, path_label="entry")
        exit_ref = self._compile_condition_group(self.spec.exit, path_label="exit")
        false_ref = self._always_false_node(entry_ref)

        metadata: dict[str, Any] = {"compiled_from": "strategy_spec.v1"}
        exit_policy = self._build_exit_policy()
        if exit_policy is not None:
            metadata[EXIT_RULE_POLICY_METADATA_KEY] = exit_policy

        genome = {
            "version": 1,
            "genome_id": _slugify(self.spec.name),
            "nodes": self.nodes,
            "entry_long": {"ref": entry_ref},
            "entry_short": {"ref": false_ref},
            "exit_long": {"ref": exit_ref},
            "exit_short": {"ref": false_ref},
            "metadata": metadata,
        }
        return genome

    def _always_false_node(self, signal_ref: str) -> str:
        inverted = self._add_node(kind="logic.not", params={}, inputs=[signal_ref])
        return self._add_node(
            kind="logic.and",
            params={},
            inputs=[signal_ref, inverted],
        )

    def _always_false_ohlc(self) -> str:
        close_ref = self._source_node("close")
        high_ref = self._source_node("high")
        return self._add_node(
            kind="cmp.gt",
            params={},
            inputs=[close_ref, high_ref],
        )

    def _next_id(self) -> str:
        self._node_counter += 1
        return f"n{self._node_counter}"

    def _add_node(self, *, kind: str, params: dict[str, Any], inputs: list[str]) -> str:
        node_id = self._next_id()
        self.nodes.append(
            {"id": node_id, "kind": kind, "params": params, "inputs": inputs}
        )
        return node_id

    def _build_indicators(self) -> None:
        for indicator in self.spec.indicators:
            self._indicator_nodes[indicator.id] = self._build_indicator_node(indicator)

    def _build_indicator_node(self, indicator: IndicatorSpec) -> str:
        if indicator.type in {"sma", "ema"}:
            source_ref = self._source_node(indicator.source)
            return self._add_node(
                kind="ind.ma",
                params={
                    "period": indicator.period,
                    "ma_type": indicator.type,
                },
                inputs=[source_ref],
            )
        if indicator.type == "rsi":
            source_ref = self._source_node(indicator.source)
            return self._add_node(
                kind="ind.rsi",
                params={"period": indicator.period},
                inputs=[source_ref],
            )
        if indicator.type == "donchian":
            return self._add_node(
                kind="ind.donchian",
                params={"period": indicator.period},
                inputs=[],
            )
        if indicator.type == "bollinger_bands":
            source_ref = self._source_node(indicator.source)
            return self._add_node(
                kind="ind.bollinger",
                params={"period": indicator.period, "num_std": 2.0},
                inputs=[source_ref],
            )

        raise StrategyCompileError(
            f"Indicator type '{indicator.type}' cannot be compiled.",
            errors=[
                ValidationErrorDetail(
                    path="indicators",
                    code="unsupported_indicator",
                    message=f"Indicator type '{indicator.type}' cannot be compiled.",
                )
            ],
        )

    def _source_node(self, column: str) -> str:
        if column in self._source_nodes:
            return self._source_nodes[column]
        kind = _SOURCE_KIND_BY_COLUMN.get(column)
        if kind is None:
            raise StrategyCompileError(f"Unsupported price source '{column}'.")
        node_id = self._add_node(kind=kind, params={}, inputs=[])
        self._source_nodes[column] = node_id
        return node_id

    def _resolve_operand(self, operand: str | float | int) -> str:
        if isinstance(operand, (int, float)):
            raise StrategyCompileError(
                "Literal thresholds in level comparisons are not supported in composite "
                "compilation; use crosses_above/crosses_below or a built-in registry mapping.",
                errors=[
                    ValidationErrorDetail(
                        path="entry",
                        code="unsupported_compile_operand",
                        message=(
                            "Literal numeric operands for level comparisons must use "
                            "crosses_above/crosses_below or map to a registry strategy."
                        ),
                    )
                ],
            )

        ref = str(operand)
        if ref in _SOURCE_KIND_BY_COLUMN:
            return self._source_node(ref)

        base, _, port_name = ref.partition(".")
        if base in self._indicator_nodes:
            indicator = self._indicators_by_id[base]
            port_map = _INDICATOR_OUTPUT_PORTS[indicator.type]
            if port_name in port_map:
                genome_port = port_map[port_name]
            elif "" in port_map:
                genome_port = port_map[""]
            else:
                raise StrategyCompileError(
                    f"Indicator '{base}' has no output port '{port_name}'.",
                    errors=[
                        ValidationErrorDetail(
                            path="conditions",
                            code="unknown_indicator_reference",
                            message=f"Indicator '{base}' has no output port '{port_name}'.",
                            suggestions=[f"{base}.{name}" for name in port_map],
                        )
                    ],
                )
            node_id = self._indicator_nodes[base]
            if genome_port == "out":
                return node_id
            return f"{node_id}:{genome_port}"

        raise StrategyCompileError(
            f"Unknown reference '{ref}'.",
            errors=[
                ValidationErrorDetail(
                    path="conditions",
                    code="unknown_indicator_reference",
                    message=f"Unknown reference '{ref}'.",
                )
            ],
        )

    def _compile_comparison(self, condition: ComparisonCondition) -> str:
        kind = _OPERATOR_TO_CMP_KIND[condition.op]
        left_ref = self._resolve_operand(condition.left)

        if condition.op in {"crosses_above", "crosses_below"} and isinstance(
            condition.right, (int, float)
        ):
            left_node, left_port = _split_ref(left_ref)
            inputs = [left_node if left_port == "out" else left_ref]
            return self._add_node(
                kind=kind,
                params={"threshold": float(condition.right)},
                inputs=inputs,
            )

        right_ref = self._resolve_operand(condition.right)
        left_node, left_port = _split_ref(left_ref)
        right_node, right_port = _split_ref(right_ref)

        if (
            condition.op == "crosses_above"
            and left_port == "out"
            and right_port == "out"
            and self._is_ma_pair(condition.left, condition.right)
        ):
            diff_id = self._add_node(
                kind="ind.diff",
                params={},
                inputs=[left_node, right_node],
            )
            return self._add_node(
                kind="cmp.cross_above",
                params={"threshold": 0.0},
                inputs=[diff_id],
            )
        if (
            condition.op == "crosses_below"
            and left_port == "out"
            and right_port == "out"
            and self._is_ma_pair(condition.left, condition.right)
        ):
            diff_id = self._add_node(
                kind="ind.diff",
                params={},
                inputs=[left_node, right_node],
            )
            return self._add_node(
                kind="cmp.cross_below",
                params={"threshold": 0.0},
                inputs=[diff_id],
            )

        return self._add_node(
            kind=kind,
            params={},
            inputs=[
                left_node if left_port == "out" else left_ref,
                right_node if right_port == "out" else right_ref,
            ],
        )

    def _is_ma_pair(self, left: str, right: str | float | int) -> bool:
        if not isinstance(right, str):
            return False
        left_id, _, _ = str(left).partition(".")
        right_id, _, _ = right.partition(".")
        left_ind = self._indicators_by_id.get(left_id)
        right_ind = self._indicators_by_id.get(right_id)
        return (
            left_ind is not None
            and right_ind is not None
            and left_ind.type in {"sma", "ema"}
            and right_ind.type in {"sma", "ema"}
        )

    def _compile_condition_group(self, group: ConditionGroup, *, path_label: str) -> str:
        comparisons = _comparison_conditions(group)
        if not comparisons:
            if path_label == "exit" and _exit_rule_conditions(group):
                false_ref = self._always_false_ohlc()
                self.exit_summary = "Exit via stop/take-profit rules"
                return false_ref
            raise StrategyCompileError(
                f"{path_label} must include at least one comparison condition.",
            )

        compiled_refs = [self._compile_comparison(item) for item in comparisons]
        mode = "all" if group.all is not None else "any"
        combined = self._combine_bool_nodes(compiled_refs, mode=mode)
        summary_joiner = " AND " if mode == "all" else " OR "
        summary = summary_joiner.join(
            f"{item.left} {item.op} {item.right}" for item in comparisons
        )
        if path_label == "entry":
            self.entry_summary = summary
        else:
            self.exit_summary = summary
        return combined

    def _combine_bool_nodes(self, refs: list[str], *, mode: str) -> str:
        if len(refs) == 1:
            return refs[0]
        join_kind = "logic.and" if mode == "all" else "logic.or"
        current = refs[0]
        for ref in refs[1:]:
            current = self._add_node(
                kind=join_kind,
                params={},
                inputs=[current, ref],
            )
        return current

    def _build_exit_policy(self) -> dict[str, Any] | None:
        rules = _exit_rule_conditions(self.spec.exit)
        if not rules:
            return None

        stop_loss: ExitStopLossCondition | None = None
        take_profit: ExitTakeProfitCondition | None = None
        for rule in rules:
            if isinstance(rule, ExitStopLossCondition):
                stop_loss = rule
            elif isinstance(rule, ExitTakeProfitCondition):
                take_profit = rule

        if stop_loss is None and take_profit is None:
            return None

        if stop_loss is not None and take_profit is not None:
            if stop_loss.mode == "percent" and take_profit.mode == "percent":
                preset = _preset_by_id("fixed_pct_bracket")
                policy = build_exit_rule_policy(preset)
                self.strategy_params[genome_exit_param_ref("stop_loss_pct")] = (
                    stop_loss.value
                )
                self.strategy_params[genome_exit_param_ref("take_profit_pct")] = (
                    take_profit.value
                )
                return policy

            raise StrategyCompileError(
                "Mixed ATR/percent stop and take-profit combinations are not supported.",
            )

        if take_profit is not None:
            if take_profit.mode != "percent":
                raise StrategyCompileError("ATR-only take-profit rules are not supported.")
            preset = _preset_by_id("fixed_pct_bracket")
            policy = build_exit_rule_policy(preset)
            self.strategy_params[genome_exit_param_ref("stop_loss_pct")] = 0.0
            self.strategy_params[genome_exit_param_ref("take_profit_pct")] = (
                take_profit.value
            )
            return policy

        assert stop_loss is not None
        if stop_loss.mode == "percent":
            policy = build_single_stop_exit_policy(atr=False)
            self.strategy_params[genome_exit_param_ref("stop_loss_pct")] = stop_loss.value
            return policy

        if stop_loss.mode == "atr":
            policy = build_single_stop_exit_policy(atr=True)
            self.strategy_params[genome_exit_param_ref("stop_loss_atr")] = stop_loss.value
            atr_period = stop_loss.atr_period or 14
            self.strategy_params[genome_exit_param_ref("atr_period")] = atr_period
            return policy

        raise StrategyCompileError(f"Unsupported stop-loss mode '{stop_loss.mode}'.")


def _preset_by_id(preset_id: str) -> ExitPreset:
    for preset in EXIT_PRESETS:
        if preset.id == preset_id:
            return preset
    raise StrategyCompileError(f"Unknown exit preset '{preset_id}'.")


def _comparison_conditions(group: ConditionGroup) -> list[ComparisonCondition]:
    raw = group.all if group.all is not None else group.any or []
    return [item for item in raw if isinstance(item, ComparisonCondition)]


def _exit_rule_conditions(group: ConditionGroup) -> list[ExitStopLossCondition | ExitTakeProfitCondition]:
    raw = group.all if group.all is not None else group.any or []
    return [
        item
        for item in raw
        if isinstance(item, (ExitStopLossCondition, ExitTakeProfitCondition))
    ]


def _refs_equal(left: str, right: str | float | int) -> bool:
    if not isinstance(right, str):
        return False
    return left == right or left == right.split(".", 1)[0]


def _split_ref(ref: str) -> tuple[str, str]:
    if ":" in ref:
        node_id, port = ref.split(":", 1)
        return node_id, port
    return ref, "out"
