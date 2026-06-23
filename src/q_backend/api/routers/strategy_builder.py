from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from q_backend.storage.settings import get_settings
from q_backend.strategy_builder.ai_models import (
    build_curated_model_options,
    is_model_available,
    resolve_interpret_model,
)
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
    AiStrategyModelsResponse,
    AiStrategyResponse,
    AiStrategyServiceErrorResponse,
    AiModelOption,
    StrategyInterpretRequest,
)
from q_backend.strategy_builder.interpret_parser import AiParseError
from q_backend.strategy_builder.interpreter import interpret_strategy_request
from q_backend.strategy_builder.providers.factory import (
    AiDisabledError,
    AiMisconfiguredError,
    build_strategy_interpreter_provider,
)
from q_backend.strategy_builder.providers.openai_compatible import (
    OpenAICompatibleInterpreterProvider,
    ProviderRequestError,
)
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.spec_models import ValidationResult
from q_backend.strategy_builder.validator import validate_strategy_spec_payload

router = APIRouter(tags=["strategy-builder"])


class ValidateStrategySpecRequest(BaseModel):
    strategy_spec: dict


class CompileStrategySpecRequest(BaseModel):
    strategy_spec: dict


def _build_ai_provider():
    return build_strategy_interpreter_provider(get_settings())


def _ai_service_error_response(exc: AiDisabledError | AiMisconfiguredError) -> HTTPException:
    status = "ai_disabled" if isinstance(exc, AiDisabledError) else "ai_misconfigured"
    return HTTPException(
        status_code=503,
        detail=AiStrategyServiceErrorResponse(
            status=status,
            message=exc.message,
        ).model_dump(mode="json"),
    )


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


@router.get(
    "/api/v1/strategy-builder/models",
    response_model=AiStrategyModelsResponse,
    responses={
        503: {
            "model": AiStrategyServiceErrorResponse,
            "description": "AI disabled or misconfigured",
        },
    },
)
def get_strategy_builder_models() -> AiStrategyModelsResponse:
    """Return curated local models with live availability from the AI provider."""
    settings = get_settings()
    try:
        provider = _build_ai_provider()
    except (AiDisabledError, AiMisconfiguredError) as exc:
        raise _ai_service_error_response(exc) from exc

    provider_model_ids: list[str] = []
    if isinstance(provider, OpenAICompatibleInterpreterProvider):
        provider_model_ids = provider.list_models()

    curated = build_curated_model_options(settings)
    models = [
        AiModelOption(
            id=option.id,
            label=option.label,
            available=is_model_available(option.id, provider_model_ids),
        )
        for option in curated
    ]
    return AiStrategyModelsResponse(
        provider=settings.ai_strategy_provider,
        default_model=settings.ai_strategy_model,
        models=models,
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
    settings = get_settings()
    try:
        provider = _build_ai_provider()
        selected_model = resolve_interpret_model(request.model, settings)
    except AiDisabledError as exc:
        raise _ai_service_error_response(exc) from exc
    except AiMisconfiguredError as exc:
        raise _ai_service_error_response(exc) from exc

    try:
        return interpret_strategy_request(
            request,
            provider=provider,
            model=selected_model,
        )
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
