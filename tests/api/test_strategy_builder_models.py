"""AI strategy model listing endpoint tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from q_backend.api.routers.strategy_builder import get_strategy_builder_models
from q_backend.storage.settings import get_settings
from q_backend.strategy_builder.ai_models import (
    build_curated_model_options,
    is_model_available,
    parse_ai_strategy_models,
    resolve_interpret_model,
)
from q_backend.strategy_builder.providers.factory import AiMisconfiguredError
from q_backend.strategy_builder.providers.openai_compatible import (
    OpenAICompatibleInterpreterProvider,
)


@pytest.fixture
def ai_enabled_settings(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("Q_AI_STRATEGY_PROVIDER", "openai_compatible")
    monkeypatch.setenv("Q_AI_STRATEGY_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("Q_AI_STRATEGY_MODEL", "gemma4-e4b:latest")
    monkeypatch.setenv(
        "Q_AI_STRATEGY_MODELS",
        "gemma4-e4b:latest|Gemma 4 E4B,qwen3.6:27b|Qwen 3.6 27B",
    )
    monkeypatch.setenv("Q_AI_STRATEGY_API_KEY", "")
    monkeypatch.setenv("Q_AI_STRATEGY_TIMEOUT_SECONDS", "60")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_parse_ai_strategy_models_supports_labels():
    # "|" separates id from label so a colon inside an Ollama "name:tag" id survives.
    parsed = parse_ai_strategy_models("gemma4-e4b:latest|Gemma 4 E4B,qwen3.6:27b|Qwen 3.6 27B")
    assert [(option.id, option.label) for option in parsed] == [
        ("gemma4-e4b:latest", "Gemma 4 E4B"),
        ("qwen3.6:27b", "Qwen 3.6 27B"),
    ]


def test_parse_ai_strategy_models_keeps_colon_ids_without_labels():
    # Bare ids (no "|") must not be split on their colon.
    parsed = parse_ai_strategy_models("gemma3:4b,qwen3.6:27b")
    assert [option.id for option in parsed] == ["gemma3:4b", "qwen3.6:27b"]


def test_resolve_interpret_model_uses_default_when_request_model_missing(ai_enabled_settings):
    settings = get_settings()
    assert resolve_interpret_model(None, settings) == "gemma4-e4b:latest"


def test_resolve_interpret_model_rejects_unknown_model(ai_enabled_settings):
    settings = get_settings()
    with pytest.raises(AiMisconfiguredError, match="not in the configured allowlist"):
        resolve_interpret_model("unknown-model", settings)


def test_is_model_available_matches_substrings():
    provider_ids = [
        "/home/user/.lmstudio/models/gemma-4-e4b-it-Q4_K_M.gguf",
    ]
    assert is_model_available("gemma-4-e4b-it", provider_ids) is True
    assert is_model_available("qwythos-9b", provider_ids) is False


def test_get_strategy_builder_models_marks_provider_availability(ai_enabled_settings):
    provider = OpenAICompatibleInterpreterProvider(
        base_url="http://localhost:11434/v1",
        model="gemma4-e4b:latest",
    )
    with patch.object(provider, "list_models", return_value=["gemma4-e4b:latest"]):
        with patch(
            "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
            return_value=provider,
        ):
            response = get_strategy_builder_models()

    assert response.default_model == "gemma4-e4b:latest"
    assert len(response.models) == 2
    assert response.models[0].id == "gemma4-e4b:latest"
    assert response.models[0].available is True
    assert response.models[1].id == "qwen3.6:27b"
    assert response.models[1].available is False


def test_get_strategy_builder_models_when_provider_unreachable(ai_enabled_settings):
    provider = OpenAICompatibleInterpreterProvider(
        base_url="http://localhost:11434/v1",
        model="gemma4-e4b:latest",
    )
    with patch.object(provider, "list_models", return_value=[]):
        with patch(
            "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
            return_value=provider,
        ):
            response = get_strategy_builder_models()

    assert all(model.available is False for model in response.models)
    assert build_curated_model_options(get_settings())


def test_get_strategy_builder_models_when_ai_disabled(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "false")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        get_strategy_builder_models()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["status"] == "ai_disabled"
    get_settings.cache_clear()


def test_get_strategy_builder_models_gemini_provider(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("Q_AI_STRATEGY_PROVIDER", "gemini")
    monkeypatch.setenv("Q_AI_STRATEGY_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("Q_AI_STRATEGY_GEMINI_API_KEY", "test-key")
    monkeypatch.setenv(
        "Q_AI_STRATEGY_MODELS",
        "gemini-2.5-flash|Gemini 2.5 Flash,gemini-2.5-pro|Gemini 2.5 Pro",
    )
    get_settings.cache_clear()

    from q_backend.strategy_builder.providers.gemini import GeminiInterpreterProvider
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
        api_key="test-key",
    )

    with patch.object(provider, "list_models", return_value=["gemini-2.5-flash"]):
        with patch(
            "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
            return_value=provider,
        ):
            response = get_strategy_builder_models()

    assert response.provider == "gemini"
    assert response.default_model == "gemini-2.5-flash"
    assert len(response.models) == 2
    assert response.models[0].id == "gemini-2.5-flash"
    assert response.models[0].available is True
    assert response.models[1].id == "gemini-2.5-pro"
    assert response.models[1].available is False
    get_settings.cache_clear()


def test_factory_gemini_missing_api_key(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("Q_AI_STRATEGY_PROVIDER", "gemini")
    monkeypatch.setenv("Q_AI_STRATEGY_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("Q_AI_STRATEGY_GEMINI_API_KEY", "")
    get_settings.cache_clear()

    from q_backend.strategy_builder.providers.factory import (
        build_strategy_interpreter_provider,
        AiMisconfiguredError,
    )

    with pytest.raises(AiMisconfiguredError) as exc_info:
        build_strategy_interpreter_provider()

    assert "Q_AI_STRATEGY_GEMINI_API_KEY" in str(exc_info.value)
    get_settings.cache_clear()


def test_factory_unsupported_provider_lists_both(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("Q_AI_STRATEGY_PROVIDER", "unknown_provider")
    get_settings.cache_clear()

    from q_backend.strategy_builder.providers.factory import (
        build_strategy_interpreter_provider,
        AiMisconfiguredError,
    )

    with pytest.raises(AiMisconfiguredError) as exc_info:
        build_strategy_interpreter_provider()

    assert "openai_compatible" in str(exc_info.value)
    assert "gemini" in str(exc_info.value)
    get_settings.cache_clear()

