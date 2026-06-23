"""User-facing StrategySpec v1 models for the AI strategy builder."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "strategy_spec.v1"

IndicatorType = Literal["sma", "ema", "rsi", "donchian", "bollinger_bands", "atr"]
PriceColumn = Literal["open", "high", "low", "close", "volume"]
ComparisonOperator = Literal[
    ">",
    "<",
    ">=",
    "<=",
    "crosses_above",
    "crosses_below",
]
MvpRiskSizing = Literal["fixed_quantity", "fixed_safety_margin"]


class IndicatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    source: PriceColumn = "close"
    period: int = Field(default=20, ge=1)


class ComparisonCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: str = Field(min_length=1)
    op: str = Field(min_length=1)
    right: str | float | int


class ExitStopLossCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["stop_loss"]
    mode: Literal["percent", "atr"]
    value: float = Field(gt=0)
    atr_period: int | None = Field(default=None, ge=1)


class ExitTakeProfitCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["take_profit"]
    mode: Literal["percent", "atr"]
    value: float = Field(gt=0)
    atr_period: int | None = Field(default=None, ge=1)


ConditionLeaf = Union[ComparisonCondition, ExitStopLossCondition, ExitTakeProfitCondition]


class ConditionGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all: list[ConditionLeaf] | None = None
    any: list[ConditionLeaf] | None = None

    @model_validator(mode="after")
    def exactly_one_group(self) -> ConditionGroup:
        has_all = self.all is not None
        has_any = self.any is not None
        if has_all == has_any:
            raise ValueError("Condition group must define exactly one of 'all' or 'any'.")
        return self


class RiskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_sizing: MvpRiskSizing
    quantity: float | None = Field(default=None, gt=0)
    safety_margin_per_contract: float | None = Field(default=None, gt=0)


class ExecutionAssumptionsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_timing: Literal["closed_bar"] = "closed_bar"
    entry_timing: Literal["next_bar_open"] = "next_bar_open"
    allow_short: bool = False
    live_trading: bool = False


class DataRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["strategy_spec.v1"] = SCHEMA_VERSION
    name: str = Field(min_length=1)
    universe: list[str] = Field(min_length=1)
    market: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    indicators: list[IndicatorSpec] = Field(default_factory=list)
    entry: ConditionGroup
    exit: ConditionGroup
    risk: RiskSpec
    execution_assumptions: ExecutionAssumptionsSpec = Field(
        default_factory=ExecutionAssumptionsSpec
    )
    data_requirements: DataRequirements | None = None


class ValidationErrorDetail(BaseModel):
    path: str
    code: str
    message: str
    suggestions: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationErrorDetail] = Field(default_factory=list)


FORBIDDEN_SPEC_KEYS = frozenset(
    {
        "python",
        "python_code",
        "code",
        "script",
        "exec",
        "eval",
        "lambda",
    }
)
