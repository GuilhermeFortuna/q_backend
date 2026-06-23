"""Provider protocol for AI strategy interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.interpret_models import StrategyInterpretRequest


@dataclass(frozen=True)
class RawAiResponse:
    content: str
    model: str
    provider: str


class StrategyInterpreterProvider(Protocol):
    provider_name: str
    model: str
    base_url: str

    def interpret(
        self,
        request: StrategyInterpretRequest,
        capabilities: CapabilityRegistry,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> RawAiResponse:
        """Return the raw model text (expected to be JSON)."""
