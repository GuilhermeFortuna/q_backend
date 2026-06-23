"""Optional AI strategy-builder metadata stored alongside custom strategies."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AiStrategyMetadata(BaseModel):
    """Additive metadata for AI-authored custom strategies (WO95)."""

    model_config = ConfigDict(extra="forbid")

    strategy_spec: dict[str, Any]
    strategy_spec_version: str = Field(min_length=1)
    capabilities_version: str = Field(min_length=1)
    original_prompt: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    unsupported_requests_acknowledged: list[str] = Field(default_factory=list)
    compiled_strategy_id: str | None = None
    compiled_strategy: dict[str, Any] | None = None
