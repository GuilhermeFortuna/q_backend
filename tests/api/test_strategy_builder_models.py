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
    monkeypatch.setenv("Q_AI_STRATEGY_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("Q_AI_STRATEGY_MODEL", "gemma-4-e4b-it")
    monkeypatch.setenv(
        "Q_AI_STRATEGY_MODELS",
        "gemma-4-e4b-it:Gemma 4 E4B,qwythos-9b:Qwythos 9B",
    )
    monkeypatch.setenv("Q_AI_STRATEGY_API_KEY", "")
    monkeypatch.setenv("Q_AI_STRATEGY_TIMEOUT_SECONDS", "60")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_parse_ai_strategy_models_supports_labels():
    parsed = parse_ai_strategy_models("model-a:Model A,model-b:Model B")
    assert [(option.id, option.label) for option in parsed] == [
        ("model-a", "Model A"),
        ("model-b", "Model B"),
    ]


def test_resolve_interpret_model_uses_default_when_request_model_missing(ai_enabled_settings):
    settings = get_settings()
    assert resolve_interpret_model(None, settings) == "gemma-4-e4b-it"


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
        base_url="http://localhost:1234/v1",
        model="gemma-4-e4b-it",
    )
    with patch.object(provider, "list_models", return_value=["gemma-4-e4b-it"]):
        with patch(
            "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
            return_value=provider,
        ):
            response = get_strategy_builder_models()

    assert response.default_model == "gemma-4-e4b-it"
    assert len(response.models) == 2
    assert response.models[0].id == "gemma-4-e4b-it"
    assert response.models[0].available is True
    assert response.models[1].id == "qwythos-9b"
    assert response.models[1].available is False


def test_get_strategy_builder_models_when_provider_unreachable(ai_enabled_settings):
    provider = OpenAICompatibleInterpreterProvider(
        base_url="http://localhost:1234/v1",
        model="gemma-4-e4b-it",
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
