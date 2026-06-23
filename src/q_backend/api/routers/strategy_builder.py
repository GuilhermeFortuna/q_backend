from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from q_backend.storage.settings import get_settings
from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.compiler import (
    compile_strategy_spec_payload,
)
from q_backend.strategy_builder.compiler_models import (
    CompileStrategySpecErrorResponse,
    CompileStrategySpecResponse,
    StrategyCompileError,
)
from q_backend.strategy_builder.interpret_models import (
    AiStrategyResponse,
    AiStrategyServiceErrorResponse,
    StrategyInterpretRequest,
)
from q_backend.strategy_builder.interpret_parser import AiParseError
from q_backend.strategy_builder.interpreter import interpret_strategy_request
from q_backend.strategy_builder.providers.factory import (
    AiDisabledError,
    AiMisconfiguredError,
    build_strategy_interpreter_provider,
)
from q_backend.strategy_builder.providers.openai_compatible import ProviderRequestError
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.spec_models import ValidationResult
from q_backend.strategy_builder.validator import validate_strategy_spec_payload

router = APIRouter(tags=["strategy-builder"])


class ValidateStrategySpecRequest(BaseModel):
    strategy_spec: dict


class CompileStrategySpecRequest(BaseModel):
    strategy_spec: dict


@router.get(
    "/api/v1/strategy-builder/capabilities",
    response_model=CapabilityRegistry,
)
def get_strategy_builder_capabilities() -> CapabilityRegistry:
    """Return the machine-readable Q capability registry for the AI strategy builder."""
    return build_capability_registry()


@router.post(
    "/api/v1/strategy-builder/validate",
    response_model=ValidationResult,
)
def validate_strategy_builder_spec(
    request: ValidateStrategySpecRequest,
) -> ValidationResult:
    """Validate a StrategySpec against backend capabilities."""
    return validate_strategy_spec_payload(request.strategy_spec)


@router.post(
    "/api/v1/strategy-builder/compile",
    response_model=CompileStrategySpecResponse,
    responses={
        422: {
            "model": CompileStrategySpecErrorResponse,
            "description": "Validation or compile failure",
        }
    },
)
def compile_strategy_builder_spec(
    request: CompileStrategySpecRequest,
) -> CompileStrategySpecResponse:
    """Compile a valid StrategySpec into a runnable backtest configuration."""
    try:
        compiled = compile_strategy_spec_payload(request.strategy_spec)
    except StrategyCompileError as exc:
        raise HTTPException(
            status_code=422,
            detail=CompileStrategySpecErrorResponse(
                status=exc.status,
                errors=exc.errors,
            ).model_dump(mode="json"),
        ) from exc

    return CompileStrategySpecResponse(
        compiled_strategy_id=compiled.compiled_id,
        compiled_strategy=compiled,
    )


@router.post(
    "/api/v1/strategy-builder/interpret",
    response_model=AiStrategyResponse,
    responses={
        503: {
            "model": AiStrategyServiceErrorResponse,
            "description": "AI disabled or misconfigured",
        },
        502: {
            "model": AiStrategyServiceErrorResponse,
            "description": "Provider or parse failure",
        },
    },
)
def interpret_strategy_builder_request(
    request: StrategyInterpretRequest,
) -> AiStrategyResponse:
    """Interpret a natural-language request into a validated StrategySpec draft."""
    try:
        provider = build_strategy_interpreter_provider(get_settings())
    except AiDisabledError as exc:
        raise HTTPException(
            status_code=503,
            detail=AiStrategyServiceErrorResponse(
                status="ai_disabled",
                message=exc.message,
            ).model_dump(mode="json"),
        ) from exc
    except AiMisconfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail=AiStrategyServiceErrorResponse(
                status="ai_misconfigured",
                message=exc.message,
            ).model_dump(mode="json"),
        ) from exc

    try:
        return interpret_strategy_request(request, provider=provider)
    except ProviderRequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=AiStrategyServiceErrorResponse(
                status="provider_error",
                message=exc.message,
                detail=exc.detail,
            ).model_dump(mode="json"),
        ) from exc
    except AiParseError as exc:
        raise HTTPException(
            status_code=502,
            detail=exc.to_error_response().model_dump(mode="json"),
        ) from exc
