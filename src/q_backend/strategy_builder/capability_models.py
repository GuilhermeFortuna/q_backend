"""Typed models for the machine-readable Q capability registry (q_capabilities.v1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from q_backend.backtesting.strategy_registry import (
    ExitPreset,
    ExitRuleInfo,
    StrategyEngine,
    StrategyInfo,
    StrategyParamSpec,
)

SCHEMA_VERSION = "q_capabilities.v1"

OutputType = Literal["price_series", "oscillator", "bool_series", "exit_policy"]
SeriesType = Literal["price_series", "oscillator"]


class DataCapabilities(BaseModel):
    markets: list[str]
    engines: list[StrategyEngine]
    timeframes: list[str]
    ohlcv_columns: list[str]
    tick_columns: list[str]
    data_sources: list[str]


class GenomeNodeCapability(BaseModel):
    kind: str
    min_inputs: int
    max_inputs: int
    input_series_types: list[SeriesType] | None
    output_ports: list[str]
    port_types: dict[str, OutputType]
    allowed_param_keys: list[str]


class GenomeLimits(BaseModel):
    max_depth: int
    max_node_count: int


class RiskSizingCapability(BaseModel):
    type: str
    label: str
    description: str


class ExecutionAssumptions(BaseModel):
    supported_signal_timing: list[str]
    supported_entry_timing: list[str]
    allow_short: bool
    ai_builder_mvp_long_only: bool = True


class CapabilityRegistry(BaseModel):
    schema_version: Literal["q_capabilities.v1"] = SCHEMA_VERSION
    data: DataCapabilities
    strategies: list[StrategyInfo]
    genome_nodes: list[GenomeNodeCapability]
    genome_param_bounds: list[StrategyParamSpec]
    genome_limits: GenomeLimits
    operators: list[str]
    condition_groups: list[str]
    exit_rules: list[ExitRuleInfo]
    exit_presets: list[ExitPreset]
    risk_sizing: list[RiskSizingCapability]
    execution_assumptions: ExecutionAssumptions
    unsupported: list[str] = Field(
        description="Product-level capabilities Q does not expose today."
    )
