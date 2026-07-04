"""Provider factory for AI strategy interpretation."""

from __future__ import annotations

from q_backend.storage.settings import Settings, get_settings
from q_backend.strategy_builder.providers.base import StrategyInterpreterProvider
from q_backend.strategy_builder.providers.openai_compatible import (
    OpenAICompatibleInterpreterProvider,
)


class AiMisconfiguredError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AiDisabledError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def build_strategy_interpreter_provider(
    settings: Settings | None = None,
) -> StrategyInterpreterProvider:
    resolved = settings or get_settings()
    if not resolved.ai_strategy_enabled:
        raise AiDisabledError(
            "AI strategy interpretation is disabled. Set Q_AI_STRATEGY_ENABLED=true to enable."
        )

    provider = resolved.ai_strategy_provider.strip().lower()
    if provider not in ("openai_compatible", "gemini"):
        raise AiMisconfiguredError(
            f"Unsupported AI strategy provider '{resolved.ai_strategy_provider}'. "
            "Supported providers: openai_compatible, gemini."
        )

    if not resolved.ai_strategy_model.strip():
        raise AiMisconfiguredError("Q_AI_STRATEGY_MODEL must be set.")

    if provider == "gemini":
        if not resolved.ai_strategy_gemini_api_key.strip():
            raise AiMisconfiguredError("Q_AI_STRATEGY_GEMINI_API_KEY must be set.")
        from q_backend.strategy_builder.providers.gemini import GeminiInterpreterProvider
        return GeminiInterpreterProvider(
            base_url=resolved.ai_strategy_gemini_base_url,
            model=resolved.ai_strategy_model,
            api_key=resolved.ai_strategy_gemini_api_key,
            timeout_seconds=resolved.ai_strategy_timeout_seconds,
            max_output_tokens=resolved.ai_strategy_max_output_tokens,
        )

    if not resolved.ai_strategy_base_url.strip():
        raise AiMisconfiguredError("Q_AI_STRATEGY_BASE_URL must be set.")

    return OpenAICompatibleInterpreterProvider(
        base_url=resolved.ai_strategy_base_url,
        model=resolved.ai_strategy_model,
        api_key=resolved.ai_strategy_api_key,
        timeout_seconds=resolved.ai_strategy_timeout_seconds,
        max_output_tokens=resolved.ai_strategy_max_output_tokens,
    )

