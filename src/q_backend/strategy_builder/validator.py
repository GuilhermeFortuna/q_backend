"""Semantic validation for StrategySpec v1 against the WO90 capability registry."""

from __future__ import annotations

from typing import Any, Iterable

from pydantic import ValidationError as PydanticValidationError

from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.market_data.api_service import DEFAULT_B3_INSTRUMENTS, stored_instruments
from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.spec_models import (
    SCHEMA_VERSION,
    ComparisonCondition,
    ConditionGroup,
    ConditionLeaf,
    ExitStopLossCondition,
    ExitTakeProfitCondition,
    FORBIDDEN_SPEC_KEYS,
    IndicatorSpec,
    StrategySpec,
    ValidationErrorDetail,
    ValidationResult,
)

MVP_INDICATOR_TYPES: tuple[str, ...] = (
    "sma",
    "ema",
    "rsi",
    "donchian",
    "bollinger_bands",
    "atr",
)

MVP_OPERATORS: tuple[str, ...] = (
    ">",
    "<",
    ">=",
    "<=",
    "crosses_above",
    "crosses_below",
)

MVP_RISK_SIZING: tuple[str, ...] = ("fixed_quantity", "fixed_safety_margin")

INDICATOR_OUTPUT_PORTS: dict[str, tuple[str, ...]] = {
    "sma": ("value",),
    "ema": ("value",),
    "rsi": ("value",),
    "atr": ("value",),
    "donchian": ("upper", "lower"),
    "bollinger_bands": ("upper", "middle", "lower"),
}

PRICE_COLUMNS = frozenset({"open", "high", "low", "close", "volume", "tick_volume"})


class StrategySpecValidator:
    def __init__(self, capabilities: CapabilityRegistry | None = None) -> None:
        self.capabilities = capabilities or build_capability_registry()
        self._current_indicators: list[IndicatorSpec] = []

    def validate_payload(self, payload: dict[str, Any]) -> ValidationResult:
        errors: list[ValidationErrorDetail] = []
        errors.extend(self._scan_forbidden_keys(payload, path=""))
        if payload.get("schema_version") not in (None, SCHEMA_VERSION):
            errors.append(
                ValidationErrorDetail(
                    path="schema_version",
                    code="invalid_schema_version",
                    message=(
                        f"Unsupported schema version '{payload.get('schema_version')}'. "
                        f"Expected '{SCHEMA_VERSION}'."
                    ),
                    suggestions=[SCHEMA_VERSION],
                )
            )

        try:
            spec = StrategySpec.model_validate(payload)
        except PydanticValidationError as exc:
            errors.extend(self._pydantic_errors(exc))
            return ValidationResult(valid=False, errors=errors)

        errors.extend(self._validate_semantics(spec))
        return ValidationResult(valid=not errors, errors=errors)

    def validate_spec(self, spec: StrategySpec) -> ValidationResult:
        return self.validate_payload(spec.model_dump(mode="json"))

    def _validate_semantics(self, spec: StrategySpec) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        errors.extend(self._validate_market(spec.market))
        errors.extend(self._validate_timeframe(spec.timeframe))
        errors.extend(self._validate_universe(spec.universe))
        errors.extend(self._validate_data_requirements(spec))
        errors.extend(self._validate_indicators(spec.indicators))
        indicator_ids = {indicator.id for indicator in spec.indicators}
        self._current_indicators = spec.indicators
        errors.extend(self._validate_condition_group(spec.entry, "entry", indicator_ids, require_comparison=True))
        errors.extend(self._validate_condition_group(spec.exit, "exit", indicator_ids, require_comparison=False))
        errors.extend(self._validate_risk(spec.risk.model_dump(mode="json")))
        errors.extend(self._validate_execution(spec.execution_assumptions.model_dump(mode="json")))
        return errors

    def _validate_market(self, market: str) -> list[ValidationErrorDetail]:
        supported = set(self.capabilities.data.markets)
        if market in supported:
            return []
        return [
            ValidationErrorDetail(
                path="market",
                code="unsupported_market",
                message=f"Market '{market}' is not currently supported.",
                suggestions=sorted(supported),
            )
        ]

    def _validate_timeframe(self, timeframe: str) -> list[ValidationErrorDetail]:
        normalized = timeframe.strip().upper()
        supported = set(self.capabilities.data.timeframes)
        if normalized in supported:
            return []
        return [
            ValidationErrorDetail(
                path="timeframe",
                code="unsupported_timeframe",
                message=f"Timeframe '{timeframe}' is not currently supported.",
                suggestions=sorted(supported),
            )
        ]

    def _validate_universe(self, universe: list[str]) -> list[ValidationErrorDetail]:
        if not universe:
            return [
                ValidationErrorDetail(
                    path="universe",
                    code="empty_universe",
                    message="Universe must include at least one symbol.",
                )
            ]

        known = _known_symbols()
        errors: list[ValidationErrorDetail] = []
        for index, symbol in enumerate(universe):
            if symbol not in known:
                errors.append(
                    ValidationErrorDetail(
                        path=f"universe[{index}]",
                        code="unknown_symbol",
                        message=f"Symbol '{symbol}' is not in the known instrument catalog.",
                        suggestions=sorted(known)[:5],
                    )
                )
        return errors

    def _validate_data_requirements(self, spec: StrategySpec) -> list[ValidationErrorDetail]:
        if spec.data_requirements is None:
            return []

        supported = set(self.capabilities.data.ohlcv_columns)
        errors: list[ValidationErrorDetail] = []
        for index, column in enumerate(spec.data_requirements.columns):
            if column not in supported:
                errors.append(
                    ValidationErrorDetail(
                        path=f"data_requirements.columns[{index}]",
                        code="unsupported_data_column",
                        message=f"Data column '{column}' is not currently supported.",
                        suggestions=sorted(supported),
                    )
                )
        return errors

    def _validate_indicators(self, indicators: list[IndicatorSpec]) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        seen_ids: set[str] = set()
        for index, indicator in enumerate(indicators):
            path_prefix = f"indicators[{index}]"
            if indicator.id in seen_ids:
                errors.append(
                    ValidationErrorDetail(
                        path=f"{path_prefix}.id",
                        code="duplicate_indicator_id",
                        message=f"Duplicate indicator id '{indicator.id}'.",
                    )
                )
            seen_ids.add(indicator.id)

            if indicator.type not in MVP_INDICATOR_TYPES:
                errors.append(
                    ValidationErrorDetail(
                        path=f"{path_prefix}.type",
                        code="unsupported_indicator",
                        message=f"Indicator '{indicator.type}' is not currently supported.",
                        suggestions=list(MVP_INDICATOR_TYPES),
                    )
                )
                continue

            if indicator.source not in self.capabilities.data.ohlcv_columns and indicator.source != "volume":
                errors.append(
                    ValidationErrorDetail(
                        path=f"{path_prefix}.source",
                        code="unsupported_data_column",
                        message=f"Indicator source '{indicator.source}' is not supported.",
                        suggestions=sorted(self.capabilities.data.ohlcv_columns),
                    )
                )

            period_spec = GENOME_PARAM_BOUNDS.get("period")
            if period_spec is not None and indicator.type in {
                "sma",
                "ema",
                "rsi",
                "donchian",
                "bollinger_bands",
                "atr",
            }:
                if period_spec.min is not None and indicator.period < period_spec.min:
                    errors.append(
                        ValidationErrorDetail(
                            path=f"{path_prefix}.period",
                            code="invalid_parameter",
                            message=(
                                f"Indicator period {indicator.period} is below the minimum " f"{int(period_spec.min)}."
                            ),
                        )
                    )
                if period_spec.max is not None and indicator.period > period_spec.max:
                    errors.append(
                        ValidationErrorDetail(
                            path=f"{path_prefix}.period",
                            code="invalid_parameter",
                            message=(
                                f"Indicator period {indicator.period} exceeds the maximum " f"{int(period_spec.max)}."
                            ),
                        )
                    )
        return errors

    def _validate_condition_group(
        self,
        group: ConditionGroup,
        path: str,
        indicator_ids: set[str],
        *,
        require_comparison: bool,
    ) -> list[ValidationErrorDetail]:
        conditions = group.all if group.all is not None else group.any or []
        group_key = "all" if group.all is not None else "any"

        if not conditions:
            code = "missing_entry_logic" if require_comparison else "missing_exit_logic"
            message = (
                "Entry logic must include at least one condition."
                if require_comparison
                else "Exit logic must include at least one rule."
            )
            return [
                ValidationErrorDetail(
                    path=f"{path}.{group_key}",
                    code=code,
                    message=message,
                )
            ]

        errors: list[ValidationErrorDetail] = []
        has_comparison = False
        for index, condition in enumerate(conditions):
            condition_path = f"{path}.{group_key}[{index}]"
            if isinstance(condition, ComparisonCondition):
                has_comparison = True
                errors.extend(self._validate_comparison(condition, condition_path, indicator_ids))
            elif isinstance(condition, (ExitStopLossCondition, ExitTakeProfitCondition)):
                errors.extend(self._validate_exit_condition(condition, condition_path))
            else:
                errors.append(
                    ValidationErrorDetail(
                        path=condition_path,
                        code="invalid_condition",
                        message="Condition is not a supported comparison or exit rule.",
                    )
                )

        if require_comparison and not has_comparison:
            errors.append(
                ValidationErrorDetail(
                    path=f"{path}.{group_key}",
                    code="missing_entry_logic",
                    message="Entry logic must include at least one comparison condition.",
                )
            )
        return errors

    def _validate_comparison(
        self,
        condition: ComparisonCondition,
        path: str,
        indicator_ids: set[str],
    ) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        if condition.op not in MVP_OPERATORS:
            errors.append(
                ValidationErrorDetail(
                    path=f"{path}.op",
                    code="unsupported_operator",
                    message=f"Operator '{condition.op}' is not currently supported.",
                    suggestions=list(MVP_OPERATORS),
                )
            )

        for side, label in ((condition.left, "left"), (condition.right, "right")):
            if isinstance(side, str):
                errors.extend(self._validate_reference(side, f"{path}.{label}", indicator_ids))
        return errors

    def _validate_exit_condition(
        self,
        condition: ExitStopLossCondition | ExitTakeProfitCondition,
        path: str,
    ) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        if condition.mode == "atr" and condition.atr_period is None:
            errors.append(
                ValidationErrorDetail(
                    path=f"{path}.atr_period",
                    code="invalid_parameter",
                    message="ATR-based exit rules require atr_period.",
                )
            )
        return errors

    def _validate_reference(
        self,
        ref: str,
        path: str,
        indicator_ids: set[str],
    ) -> list[ValidationErrorDetail]:
        if ref in PRICE_COLUMNS:
            return []

        base, _, port = ref.partition(".")
        if base in indicator_ids:
            indicator = next(ind for ind in self._current_indicators if ind.id == base)
            valid_ports = INDICATOR_OUTPUT_PORTS[indicator.type]
            if port and port not in valid_ports:
                return [
                    ValidationErrorDetail(
                        path=path,
                        code="unknown_indicator_reference",
                        message=f"Indicator '{base}' has no output port '{port}'.",
                        suggestions=[f"{base}.{name}" for name in valid_ports],
                    )
                ]
            return []

        return [
            ValidationErrorDetail(
                path=path,
                code="unknown_indicator_reference",
                message=f"Unknown indicator or price reference '{ref}'.",
                suggestions=sorted(indicator_ids),
            )
        ]

    def _validate_risk(self, risk: dict[str, Any]) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        sizing = risk.get("position_sizing")
        if sizing not in MVP_RISK_SIZING:
            errors.append(
                ValidationErrorDetail(
                    path="risk.position_sizing",
                    code="unsupported_risk_model",
                    message=f"Risk model '{sizing}' is not supported in the MVP builder.",
                    suggestions=list(MVP_RISK_SIZING),
                )
            )
            return errors

        if sizing == "fixed_quantity" and risk.get("quantity") is None:
            errors.append(
                ValidationErrorDetail(
                    path="risk.quantity",
                    code="invalid_parameter",
                    message="fixed_quantity sizing requires quantity > 0.",
                )
            )
        if sizing == "fixed_safety_margin" and risk.get("safety_margin_per_contract") is None:
            errors.append(
                ValidationErrorDetail(
                    path="risk.safety_margin_per_contract",
                    code="invalid_parameter",
                    message="fixed_safety_margin sizing requires safety_margin_per_contract > 0.",
                )
            )
        return errors

    def _validate_execution(self, execution: dict[str, Any]) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        caps = self.capabilities.execution_assumptions

        signal_timing = execution.get("signal_timing", "closed_bar")
        if signal_timing not in caps.supported_signal_timing:
            errors.append(
                ValidationErrorDetail(
                    path="execution_assumptions.signal_timing",
                    code="unsupported_execution_assumption",
                    message=f"Signal timing '{signal_timing}' is not supported.",
                    suggestions=list(caps.supported_signal_timing),
                )
            )

        entry_timing = execution.get("entry_timing", "next_bar_open")
        if entry_timing not in caps.supported_entry_timing:
            errors.append(
                ValidationErrorDetail(
                    path="execution_assumptions.entry_timing",
                    code="unsupported_execution_assumption",
                    message=f"Entry timing '{entry_timing}' is not supported.",
                    suggestions=list(caps.supported_entry_timing),
                )
            )

        if execution.get("allow_short"):
            errors.append(
                ValidationErrorDetail(
                    path="execution_assumptions.allow_short",
                    code="short_not_allowed",
                    message="Short selling is not supported in the AI strategy builder MVP.",
                )
            )

        if execution.get("live_trading"):
            errors.append(
                ValidationErrorDetail(
                    path="execution_assumptions.live_trading",
                    code="unsupported_capability",
                    message="Live trading is not currently supported.",
                    suggestions=["backtest_only"],
                )
            )
        return errors

    def _scan_forbidden_keys(self, value: Any, *, path: str) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        if isinstance(value, dict):
            for key, nested in value.items():
                child_path = f"{path}.{key}" if path else key
                if key in FORBIDDEN_SPEC_KEYS:
                    errors.append(
                        ValidationErrorDetail(
                            path=child_path,
                            code="forbidden_field",
                            message=f"Field '{key}' is not allowed in StrategySpec.",
                        )
                    )
                errors.extend(self._scan_forbidden_keys(nested, path=child_path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                child_path = f"{path}[{index}]"
                errors.extend(self._scan_forbidden_keys(item, path=child_path))
        return errors

    def _pydantic_errors(self, exc: PydanticValidationError) -> list[ValidationErrorDetail]:
        errors: list[ValidationErrorDetail] = []
        for error in exc.errors():
            path = _format_error_path(error.get("loc", ()))
            error_type = error.get("type", "")
            if error_type == "extra_forbidden":
                code = "forbidden_field"
            elif error_type == "value_error":
                code = "invalid_condition_group"
            else:
                code = "parse_error"
            errors.append(
                ValidationErrorDetail(
                    path=path,
                    code=code,
                    message=error.get("msg", "Invalid StrategySpec field."),
                )
            )
        return errors


def _format_error_path(location: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in location:
        if item == "body":
            continue
        if isinstance(item, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{item}]"
            else:
                parts.append(f"[{item}]")
        else:
            parts.append(str(item))
    return ".".join(parts) if parts else ""


def _known_symbols() -> set[str]:
    symbols: set[str] = set()
    for item in DEFAULT_B3_INSTRUMENTS:
        symbols.add(item["symbol"])
    for item in stored_instruments():
        symbols.add(item["symbol"])
    return symbols


def validate_strategy_spec_payload(payload: dict[str, Any]) -> ValidationResult:
    return StrategySpecValidator().validate_payload(payload)


def validate_strategy_spec(spec: StrategySpec) -> ValidationResult:
    return StrategySpecValidator().validate_spec(spec)
