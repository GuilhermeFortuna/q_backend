"""Request/response models for the AI strategy interpretation endpoint (WO93)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from q_backend.strategy_builder.capability_models import SCHEMA_VERSION as CAPABILITIES_SCHEMA_VERSION
from q_backend.strategy_builder.compiler_models import CompiledStrategy
from q_backend.strategy_builder.spec_models import ValidationErrorDetail, ValidationResult


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)


class StrategyInterpretRequest(BaseModel):
    message: str = Field(min_length=1)
    model: str | None = None
    provider: str | None = None
    # Prior turns for multi-turn refinement. The current user turn may appear as
    # the last entry; the server dedupes it against ``message`` when building the
    # prompt so it is not repeated in the history section.
    conversation: list[ConversationMessage] = Field(default_factory=list)
    current_spec: dict[str, Any] | None = None
    capabilities_version: str = CAPABILITIES_SCHEMA_VERSION
    validation_errors: list[ValidationErrorDetail] = Field(default_factory=list)


class AiStrategyResponse(BaseModel):
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    unsupported_requests: list[str] = Field(default_factory=list)
    change_notes: list[str] = Field(default_factory=list)
    strategy_spec: dict[str, Any] | None = None
    validation: ValidationResult | None = None
    compiled_strategy: CompiledStrategy | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class AiStrategyServiceErrorResponse(BaseModel):
    status: Literal[
        "ai_disabled",
        "ai_misconfigured",
        "provider_error",
        "parse_error",
    ]
    message: str
    detail: str | None = None


class AiModelOption(BaseModel):
    id: str
    label: str
    available: bool = False
    provider: str


class AiProviderOption(BaseModel):
    id: str
    label: str


class AiStrategyModelsResponse(BaseModel):
    provider: str
    default_model: str
    providers: list[AiProviderOption] = Field(default_factory=list)
    models: list[AiModelOption] = Field(default_factory=list)


class ParsedAiInterpreterPayload(BaseModel):
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    unsupported_requests: list[str] = Field(default_factory=list)
    change_notes: list[str] = Field(default_factory=list)
    strategy_spec: dict[str, Any] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
