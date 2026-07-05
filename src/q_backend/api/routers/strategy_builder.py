import logging

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
    AiProviderOption,
    StrategyInterpretRequest,
)
from q_backend.strategy_builder.interpret_parser import AiParseError
from q_backend.strategy_builder.interpreter import interpret_strategy_request
from q_backend.strategy_builder.providers.factory import (
    AiDisabledError,
    AiMisconfiguredError,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    build_strategy_interpreter_provider,
    build_strategy_interpreter_providers,
)
from q_backend.strategy_builder.providers.openai_compatible import (
    ProviderRequestError,
)
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.spec_models import ValidationResult
from q_backend.strategy_builder.validator import validate_strategy_spec_payload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["strategy-builder"])


class ValidateStrategySpecRequest(BaseModel):
    strategy_spec: dict


class CompileStrategySpecRequest(BaseModel):
    strategy_spec: dict


def _build_ai_provider():
    return build_strategy_interpreter_provider(get_settings())


def _ordered_provider_ids(
    default_provider_id: str,
    configured_ids: set[str] | list[str],
) -> list[str]:
    configured = list(configured_ids)
    if default_provider_id in configured:
        return [default_provider_id, *[pid for pid in configured if pid != default_provider_id]]
    return configured


def _gather_provider_models(
    settings,
    provider,
    provider_id: str,
) -> list[AiModelOption]:
    curated = build_curated_model_options(settings, provider_id)
    if provider_id == "gemini":
        return [
            AiModelOption(
                id=option.id,
                label=option.label,
                available=True,
                provider=provider_id,
            )
            for option in curated
        ]

    provider_model_ids: list[str] = []
    list_models = getattr(provider, "list_models", None)
    if callable(list_models):
        provider_model_ids = list_models()

    return [
        AiModelOption(
            id=option.id,
            label=option.label,
            available=is_model_available(option.id, provider_model_ids),
            provider=provider_id,
        )
        for option in curated
    ]


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
    """Return curated models from all configured providers."""
    settings = get_settings()
    try:
        registry = build_strategy_interpreter_providers(settings)
    except (AiDisabledError, AiMisconfiguredError) as exc:
        raise _ai_service_error_response(exc) from exc

    default_provider_id = settings.ai_strategy_provider.strip().lower()
    provider_ids = _ordered_provider_ids(default_provider_id, registry.keys())
    models: list[AiModelOption] = []

    for provider_id in provider_ids:
        try:
            models.extend(
                _gather_provider_models(settings, registry[provider_id], provider_id)
            )
        except Exception:
            logger.exception(
                "Failed to gather models for provider '%s'; skipping.",
                provider_id,
            )

    return AiStrategyModelsResponse(
        provider=default_provider_id,
        default_model=settings.ai_strategy_model,
        providers=[
            AiProviderOption(id=provider_id, label=PROVIDER_LABELS[provider_id])
            for provider_id in provider_ids
        ],
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
    """Interpret a natural-language request into a validated StrategySpec draft.

  Returns ``change_notes``: short, concrete edits the model made to the draft on
  this turn (empty for a first draft).
    """
    settings = get_settings()
    requested_provider = (
        request.provider or settings.ai_strategy_provider
    ).strip().lower()

    if request.provider is not None and requested_provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "invalid_provider",
                "message": (
                    f"Unsupported AI strategy provider '{request.provider or settings.ai_strategy_provider}'. "
                    f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
                ),
            },
        )

    try:
        registry = build_strategy_interpreter_providers(settings)
    except AiDisabledError as exc:
        raise _ai_service_error_response(exc) from exc
    except AiMisconfiguredError as exc:
        raise _ai_service_error_response(exc) from exc

    if requested_provider not in registry:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "provider_not_configured",
                "message": (
                    f"AI strategy provider '{requested_provider}' is not configured. "
                    f"Configured providers: {', '.join(registry)}."
                ),
            },
        )

    provider = registry[requested_provider]
    try:
        selected_model = resolve_interpret_model(
            request.model,
            settings,
            requested_provider,
        )
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
