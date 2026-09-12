"""Compiled strategy models for the deterministic StrategySpec compiler (WO92)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from q_backend.strategy_builder.spec_models import ValidationErrorDetail


class CompiledStrategySummary(BaseModel):
    name: str
    strategy_label: str
    mapping: str
    indicators: list[str] = Field(default_factory=list)
    entry_summary: str
    exit_summary: str
    universe: list[str] = Field(default_factory=list)
    timeframe: str
    market: str


class BacktestConfigPayload(BaseModel):
    """Runnable backtest fields derived from a compiled strategy."""

    symbol: str
    timeframe: str
    strategy: str
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    position_sizing: dict[str, Any] = Field(default_factory=dict)
    execution_assumptions: dict[str, Any] = Field(default_factory=dict)


class CompiledStrategy(BaseModel):
    compiled_id: str
    schema_version: str
    strategy_name: str
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    genome: dict[str, Any] | None = None
    summary: CompiledStrategySummary
    backtest_config: BacktestConfigPayload


class CompileStrategySpecResponse(BaseModel):
    compiled_strategy_id: str
    status: Literal["compiled"] = "compiled"
    compiled_strategy: CompiledStrategy


class CompileStrategySpecErrorResponse(BaseModel):
    status: Literal["validation_failed", "compile_failed"] = "validation_failed"
    errors: list[ValidationErrorDetail] = Field(default_factory=list)


class StrategyCompileError(Exception):
    """Raised when a StrategySpec cannot be compiled into a runnable strategy."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[ValidationErrorDetail] | None = None,
        code: str = "compile_failed",
        status: Literal["validation_failed", "compile_failed"] = "compile_failed",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors = errors or [ValidationErrorDetail(path="", code=code, message=message)]
