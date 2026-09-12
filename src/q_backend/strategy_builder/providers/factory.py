"""Provider factory for AI strategy interpretation."""

from __future__ import annotations

from q_backend.storage.settings import Settings, get_settings
from q_backend.strategy_builder.providers.base import StrategyInterpreterProvider
from q_backend.strategy_builder.providers.openai_compatible import (
    OpenAICompatibleInterpreterProvider,
)

SUPPORTED_PROVIDERS = ("openai_compatible", "gemini")

PROVIDER_LABELS: dict[str, str] = {
    "openai_compatible": "Local (Ollama)",
    "gemini": "Gemini",
}


class AiMisconfiguredError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AiDisabledError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _validate_ai_enabled(settings: Settings) -> None:
    if not settings.ai_strategy_enabled:
        raise AiDisabledError("AI strategy interpretation is disabled. Set Q_AI_STRATEGY_ENABLED=true to enable.")


def _validate_default_provider_name(settings: Settings) -> str:
    provider = settings.ai_strategy_provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise AiMisconfiguredError(
            f"Unsupported AI strategy provider '{settings.ai_strategy_provider}'. "
            "Supported providers: openai_compatible, gemini."
        )
    return provider


def _is_openai_compatible_configured(settings: Settings) -> bool:
    return bool(settings.ai_strategy_base_url.strip())


def _is_gemini_configured(settings: Settings) -> bool:
    return bool(settings.ai_strategy_gemini_api_key.strip())


def build_strategy_interpreter_providers(
    settings: Settings | None = None,
) -> dict[str, StrategyInterpreterProvider]:
    """Return every configured provider keyed by provider id."""
    resolved = settings or get_settings()
    _validate_ai_enabled(resolved)
    default_provider_id = _validate_default_provider_name(resolved)

    if not resolved.ai_strategy_model.strip():
        raise AiMisconfiguredError("Q_AI_STRATEGY_MODEL must be set.")

    from q_backend.strategy_builder.ai_models import resolve_provider_default_model

    providers: dict[str, StrategyInterpreterProvider] = {}

    if _is_openai_compatible_configured(resolved):
        providers["openai_compatible"] = OpenAICompatibleInterpreterProvider(
            base_url=resolved.ai_strategy_base_url,
            model=resolve_provider_default_model(resolved, "openai_compatible"),
            api_key=resolved.ai_strategy_api_key,
            timeout_seconds=resolved.ai_strategy_timeout_seconds,
            max_output_tokens=resolved.ai_strategy_max_output_tokens,
        )

    if _is_gemini_configured(resolved):
        from q_backend.strategy_builder.providers.gemini import GeminiInterpreterProvider

        providers["gemini"] = GeminiInterpreterProvider(
            base_url=resolved.ai_strategy_gemini_base_url,
            model=resolve_provider_default_model(resolved, "gemini"),
            api_key=resolved.ai_strategy_gemini_api_key,
            timeout_seconds=resolved.ai_strategy_timeout_seconds,
            max_output_tokens=resolved.ai_strategy_max_output_tokens,
        )

    if not providers:
        raise AiMisconfiguredError(
            "No AI strategy providers are configured. Set Q_AI_STRATEGY_BASE_URL for "
            "openai_compatible or Q_AI_STRATEGY_GEMINI_API_KEY for gemini."
        )

    if default_provider_id not in providers:
        raise AiMisconfiguredError(
            f"Default AI strategy provider '{resolved.ai_strategy_provider}' is not configured. "
            f"Configured providers: {', '.join(providers)}."
        )

    return providers


def build_strategy_interpreter_provider(
    settings: Settings | None = None,
) -> StrategyInterpreterProvider:
    resolved = settings or get_settings()
    providers = build_strategy_interpreter_providers(resolved)
    default_provider_id = resolved.ai_strategy_provider.strip().lower()
    return providers[default_provider_id]
