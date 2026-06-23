"""Orchestrate AI strategy interpretation with validation and compilation."""

from __future__ import annotations

import logging

from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.compiler import compile_strategy_spec_payload
from q_backend.strategy_builder.compiler_models import StrategyCompileError
from q_backend.strategy_builder.interpret_models import (
    AiStrategyResponse,
    StrategyInterpretRequest,
)
from q_backend.strategy_builder.interpret_parser import AiParseError, parse_ai_interpreter_response
from q_backend.strategy_builder.interpret_prompt import build_system_prompt, build_user_prompt
from q_backend.strategy_builder.providers.base import StrategyInterpreterProvider
from q_backend.strategy_builder.providers.openai_compatible import ProviderRequestError
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.validator import validate_strategy_spec_payload

logger = logging.getLogger(__name__)


def interpret_strategy_request(
    request: StrategyInterpretRequest,
    *,
    provider: StrategyInterpreterProvider,
    capabilities: CapabilityRegistry | None = None,
    model: str | None = None,
) -> AiStrategyResponse:
    registry = capabilities or build_capability_registry()
    if request.capabilities_version != registry.schema_version:
        logger.info(
            "Interpret request used capabilities_version=%s (current=%s)",
            request.capabilities_version,
            registry.schema_version,
        )

    system_prompt = build_system_prompt(registry)
    user_prompt = build_user_prompt(request)
    interpret_kwargs: dict[str, str] = {}
    if model is not None:
        interpret_kwargs["model"] = model
    raw = provider.interpret(
        request,
        registry,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        **interpret_kwargs,
    )
    parsed = parse_ai_interpreter_response(raw.content)

    validation = None
    compiled = None
    strategy_spec = parsed.strategy_spec

    if strategy_spec is not None:
        validation = validate_strategy_spec_payload(strategy_spec)
        if validation.valid:
            try:
                compiled = compile_strategy_spec_payload(strategy_spec)
            except StrategyCompileError as exc:
                logger.warning("Compilation failed after valid StrategySpec from model.")
                validation = validation.model_copy(
                    update={
                        "valid": False,
                        "errors": [
                            *validation.errors,
                            *exc.errors,
                        ],
                    }
                )

    return AiStrategyResponse(
        summary=parsed.summary,
        assumptions=parsed.assumptions,
        questions=parsed.questions,
        unsupported_requests=parsed.unsupported_requests,
        strategy_spec=strategy_spec,
        validation=validation,
        compiled_strategy=compiled,
        confidence=parsed.confidence,
    )
