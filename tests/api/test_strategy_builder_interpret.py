"""AI strategy interpretation endpoint tests (WO93)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from q_backend.api.routers.strategy_builder import interpret_strategy_builder_request
from q_backend.storage.settings import get_settings
from q_backend.strategy_builder.interpret_models import StrategyInterpretRequest
from q_backend.strategy_builder.interpret_parser import AiParseError, parse_ai_interpreter_response
from q_backend.strategy_builder.interpreter import interpret_strategy_request
from q_backend.strategy_builder.providers.base import RawAiResponse
from q_backend.strategy_builder.providers.factory import build_strategy_interpreter_provider
from q_backend.strategy_builder.providers.openai_compatible import (
    OpenAICompatibleInterpreterProvider,
    ProviderRequestError,
)
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.spec_models import ValidationErrorDetail

EMA_CROSS_SPEC = {
    "schema_version": "strategy_spec.v1",
    "name": "EMA Trend Cross",
    "universe": ["PETR4"],
    "market": "B3",
    "timeframe": "D1",
    "indicators": [
        {"id": "ema_fast", "type": "ema", "source": "close", "period": 20},
        {"id": "ema_slow", "type": "ema", "source": "close", "period": 50},
    ],
    "entry": {
        "all": [
            {
                "left": "ema_fast",
                "op": "crosses_above",
                "right": "ema_slow",
            }
        ]
    },
    "exit": {
        "any": [
            {
                "left": "ema_fast",
                "op": "crosses_below",
                "right": "ema_slow",
            },
            {"type": "stop_loss", "mode": "percent", "value": 0.03},
        ]
    },
    "risk": {"position_sizing": "fixed_quantity", "quantity": 1.0},
    "execution_assumptions": {
        "signal_timing": "closed_bar",
        "entry_timing": "next_bar_open",
        "allow_short": False,
    },
}


@dataclass
class FakeInterpreterProvider:
    content: str
    provider_name: str = "fake"
    model: str = "fake-model"
    base_url: str = "http://fake.local/v1"
    calls: int = 0

    def interpret(self, request, capabilities, *, system_prompt, user_prompt, **kwargs):
        del request, capabilities, system_prompt, user_prompt, kwargs
        self.calls += 1
        return RawAiResponse(
            content=self.content,
            model=self.model,
            provider=self.provider_name,
        )


def _ai_response_payload(**overrides) -> dict:
    payload = {
        "summary": "Created an EMA crossover strategy.",
        "assumptions": [
            "Uses closed-bar signals.",
            "Entries occur at the next bar open.",
        ],
        "questions": [],
        "unsupported_requests": [],
        "strategy_spec": EMA_CROSS_SPEC,
        "confidence": 0.92,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def ai_enabled_settings(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("Q_AI_STRATEGY_PROVIDER", "openai_compatible")
    monkeypatch.setenv("Q_AI_STRATEGY_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("Q_AI_STRATEGY_MODEL", "test-model-a")
    monkeypatch.setenv("Q_AI_STRATEGY_MODELS", "test-model-a|Model A,test-model-b|Model B")
    monkeypatch.setenv("Q_AI_STRATEGY_API_KEY", "")
    monkeypatch.setenv("Q_AI_STRATEGY_TIMEOUT_SECONDS", "60")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_supported_prompt_returns_valid_spec_and_compiled_strategy():
    provider = FakeInterpreterProvider(content=json.dumps(_ai_response_payload()))
    response = interpret_strategy_request(
        StrategyInterpretRequest(
            message="Create a trend strategy using EMA 20 and EMA 50."
        ),
        provider=provider,
    )

    assert response.summary
    assert response.strategy_spec is not None
    assert response.validation is not None
    assert response.validation.valid is True
    assert response.compiled_strategy is not None
    assert response.compiled_strategy.strategy_name == "CompositeStrategy"


def test_unsupported_prompt_lists_requests_without_runtime_payload():
    provider = FakeInterpreterProvider(
        content=json.dumps(
            _ai_response_payload(
                summary="Cannot fully support the request.",
                unsupported_requests=["Supertrend indicator", "Live trading"],
                strategy_spec=None,
                questions=["Which symbol should we trade?"],
                confidence=0.4,
            )
        )
    )

    response = interpret_strategy_request(
        StrategyInterpretRequest(message="Build a supertrend strategy for live trading."),
        provider=provider,
    )

    assert response.unsupported_requests == ["Supertrend indicator", "Live trading"]
    assert response.strategy_spec is None
    assert response.validation is None
    assert response.compiled_strategy is None


def test_validation_error_repair_path_returns_compiled_strategy():
    repaired_spec = dict(EMA_CROSS_SPEC)
    repaired_spec["timeframe"] = "D1"
    provider = FakeInterpreterProvider(content=json.dumps(_ai_response_payload(strategy_spec=repaired_spec)))

    response = interpret_strategy_request(
        StrategyInterpretRequest(
            message="Fix the timeframe.",
            current_spec=dict(EMA_CROSS_SPEC, timeframe="BADTF"),
            validation_errors=[
                ValidationErrorDetail(
                    path="timeframe",
                    code="unsupported_timeframe",
                    message="Timeframe 'BADTF' is not currently supported.",
                    suggestions=["D1"],
                )
            ],
        ),
        provider=provider,
    )

    assert response.validation is not None
    assert response.validation.valid is True
    assert response.compiled_strategy is not None


def test_malformed_model_json_is_rejected_safely():
    with pytest.raises(AiParseError, match="not valid JSON"):
        parse_ai_interpreter_response("{ this is not json")


def test_executable_code_output_is_rejected():
    with pytest.raises(AiParseError, match="forbidden field 'python_code'"):
        parse_ai_interpreter_response(
            json.dumps(
                {
                    "summary": "bad",
                    "python_code": "print('hack')",
                    "assumptions": [],
                    "questions": [],
                    "unsupported_requests": [],
                    "strategy_spec": None,
                    "confidence": 0.1,
                }
            )
        )

    with pytest.raises(AiParseError, match="StrategySpec contained forbidden field"):
        parse_ai_interpreter_response(
            json.dumps(
                _ai_response_payload(
                    strategy_spec={**EMA_CROSS_SPEC, "python_code": "print('hack')"}
                )
            )
        )


def test_ai_disabled_returns_service_error(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "false")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        interpret_strategy_builder_request(
            StrategyInterpretRequest(message="Create an EMA strategy.")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["status"] == "ai_disabled"


def test_ai_misconfigured_returns_service_error(monkeypatch):
    monkeypatch.setenv("Q_AI_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("Q_AI_STRATEGY_PROVIDER", "unknown_provider")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        interpret_strategy_builder_request(
            StrategyInterpretRequest(message="Create an EMA strategy.")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["status"] == "ai_misconfigured"


def test_provider_selection_uses_configured_provider_model_and_base_url(ai_enabled_settings):
    settings = get_settings()
    with patch(
        "q_backend.strategy_builder.providers.factory.OpenAICompatibleInterpreterProvider"
    ) as mock_provider_cls:
        mock_provider_cls.return_value = FakeInterpreterProvider(
            content=json.dumps(_ai_response_payload())
        )
        provider = build_strategy_interpreter_provider(settings)

    mock_provider_cls.assert_called_once_with(
        base_url="http://localhost:11434/v1",
        model="test-model-a",
        api_key="",
        timeout_seconds=60,
        max_output_tokens=settings.ai_strategy_max_output_tokens,
    )
    assert isinstance(provider, FakeInterpreterProvider)


def test_local_openai_compatible_config_allows_empty_api_key(ai_enabled_settings):
    provider = build_strategy_interpreter_provider(get_settings())
    assert isinstance(provider, OpenAICompatibleInterpreterProvider)
    assert provider.api_key == ""


def test_timeout_provider_errors_return_frontend_renderable_service_error(ai_enabled_settings):
    provider = FakeInterpreterProvider(content="")

    def _raise_timeout(*args, **kwargs):
        del args, kwargs
        raise ProviderRequestError(
            "AI provider request timed out.",
            detail="Timed out after 60s.",
        )

    provider.interpret = _raise_timeout  # type: ignore[method-assign]

    with patch(
        "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
        return_value=provider,
    ):
        with pytest.raises(HTTPException) as exc_info:
            interpret_strategy_builder_request(
                StrategyInterpretRequest(message="Create an EMA strategy.")
            )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["status"] == "provider_error"
    assert "timed out" in exc_info.value.detail["message"].lower()


def test_interpret_endpoint_uses_registry_backed_prompt(ai_enabled_settings):
    provider = FakeInterpreterProvider(content=json.dumps(_ai_response_payload()))
    capabilities = build_capability_registry()

    with patch(
        "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
        return_value=provider,
    ):
        response = interpret_strategy_builder_request(
            StrategyInterpretRequest(
                message="Create a trend strategy using EMA 20 and EMA 50.",
                capabilities_version=capabilities.schema_version,
            )
        )

    assert response.compiled_strategy is not None
    assert provider.calls == 1


def test_invalid_model_spec_returns_validation_without_compilation():
    invalid_spec = dict(EMA_CROSS_SPEC)
    invalid_spec["timeframe"] = "BADTF"
    provider = FakeInterpreterProvider(
        content=json.dumps(_ai_response_payload(strategy_spec=invalid_spec))
    )

    response = interpret_strategy_request(
        StrategyInterpretRequest(message="Create a strategy with a bad timeframe."),
        provider=provider,
    )

    assert response.strategy_spec is not None
    assert response.validation is not None
    assert response.validation.valid is False
    assert response.compiled_strategy is None
    assert any(error.code == "unsupported_timeframe" for error in response.validation.errors)


def test_interpret_request_model_override_is_passed_to_provider(ai_enabled_settings):
    provider = FakeInterpreterProvider(content=json.dumps(_ai_response_payload()))
    captured: dict[str, str | None] = {"model": None}

    def _capture_interpret(*args, **kwargs):
        captured["model"] = kwargs.get("model")
        return FakeInterpreterProvider.interpret(provider, *args, **kwargs)

    provider.interpret = _capture_interpret  # type: ignore[method-assign]

    with patch(
        "q_backend.api.routers.strategy_builder.build_strategy_interpreter_provider",
        return_value=provider,
    ):
        interpret_strategy_builder_request(
            StrategyInterpretRequest(
                message="Create an EMA strategy.",
                model="test-model-b",
            )
        )

    assert captured["model"] == "test-model-b"


def test_interpret_unknown_model_returns_misconfigured(ai_enabled_settings):
    with pytest.raises(HTTPException) as exc_info:
        interpret_strategy_builder_request(
            StrategyInterpretRequest(
                message="Create an EMA strategy.",
                model="not-allowed",
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["status"] == "ai_misconfigured"


def test_openai_compatible_provider_list_models_parses_response():
    provider = OpenAICompatibleInterpreterProvider(
        base_url="http://localhost:1234/v1",
        model="gemma-4-e4b-it",
    )
    payload = json.dumps({"data": [{"id": "gemma-4-e4b-it"}, {"id": "qwythos-9b"}]})

    class _FakeResponse:
        def __init__(self, body: str) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args
            return False

    with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
        assert provider.list_models() == ["gemma-4-e4b-it", "qwythos-9b"]
