"""Tests for the Gemini AI strategy provider."""

from __future__ import annotations

import json
from unittest.mock import patch
import urllib.error

import pytest

from q_backend.strategy_builder.providers.gemini import GeminiInterpreterProvider
from q_backend.strategy_builder.providers.base import ProviderRequestError
from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.interpret_models import StrategyInterpretRequest
from q_backend.strategy_builder.registry import build_capability_registry


class _FakeResponse:
    def __init__(self, body: str, code: int = 200) -> None:
        self._body = body
        self.code = code

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        del args
        return False


def test_gemini_provider_happy_path_interpret():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
        api_key="test-api-key",
        max_output_tokens=1024,
    )

    response_payload = {
        "candidates": [
            {"content": {"parts": [{"text": '{\n  "schema_version": "strategy_spec.v1"\n}'}]}, "finishReason": "STOP"}
        ]
    }

    request = StrategyInterpretRequest(message="Build a test strategy")
    capabilities = build_capability_registry()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse(json.dumps(response_payload))

        res = provider.interpret(
            request,
            capabilities,
            system_prompt="system instructions",
            user_prompt="user prompt text",
        )

        assert res.content == '{\n  "schema_version": "strategy_spec.v1"\n}'
        assert res.model == "gemini-2.5-flash"
        assert res.provider == "gemini"

        # Verify request parameters
        mock_urlopen.assert_called_once()
        args, kwargs = mock_urlopen.call_args
        http_req = args[0]

        # Check URL
        assert (
            http_req.full_url
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        )
        # Check header
        assert http_req.get_header("X-goog-api-key") == "test-api-key"

        # Check body
        req_body = json.loads(http_req.data.decode("utf-8"))
        assert req_body["system_instruction"]["parts"][0]["text"] == "system instructions"
        assert req_body["contents"][0]["role"] == "user"
        assert req_body["contents"][0]["parts"][0]["text"] == "user prompt text"
        assert req_body["generationConfig"]["temperature"] == 0.2
        assert req_body["generationConfig"]["responseMimeType"] == "application/json"
        assert req_body["generationConfig"]["maxOutputTokens"] == 1024


def test_gemini_provider_interpret_strips_models_prefix_if_present():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="models/gemini-2.5-flash",
        api_key="test-api-key",
    )

    response_payload = {"candidates": [{"content": {"parts": [{"text": '{"key": "val"}'}]}}]}

    request = StrategyInterpretRequest(message="test")
    capabilities = build_capability_registry()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse(json.dumps(response_payload))

        provider.interpret(
            request,
            capabilities,
            system_prompt="sys",
            user_prompt="user",
        )

        mock_urlopen.assert_called_once()
        args, kwargs = mock_urlopen.call_args
        http_req = args[0]
        assert (
            http_req.full_url
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        )


def test_gemini_provider_parts_concatenation():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
    )

    response_payload = {"candidates": [{"content": {"parts": [{"text": "part1 "}, {"text": "part2"}]}}]}

    request = StrategyInterpretRequest(message="test")
    capabilities = build_capability_registry()

    with patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps(response_payload))):
        res = provider.interpret(
            request,
            capabilities,
            system_prompt="sys",
            user_prompt="user",
        )
        assert res.content == "part1 part2"


def test_gemini_provider_empty_candidates_with_block_reason():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
    )

    response_payload = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}

    request = StrategyInterpretRequest(message="test")
    capabilities = build_capability_registry()

    with patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps(response_payload))):
        with pytest.raises(ProviderRequestError) as exc_info:
            provider.interpret(
                request,
                capabilities,
                system_prompt="sys",
                user_prompt="user",
            )
        assert "SAFETY" in exc_info.value.detail
        assert "AI provider returned empty content." in exc_info.value.message


def test_gemini_provider_http_error_handling():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
    )

    request = StrategyInterpretRequest(message="test")
    capabilities = build_capability_registry()

    class _FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://url", 429, "Too Many Requests", {}, None)

        def read(self, *args, **kwargs):
            return b"Rate limit exceeded"

    with patch("urllib.request.urlopen", side_effect=_FakeHTTPError()):
        with pytest.raises(ProviderRequestError) as exc_info:
            provider.interpret(
                request,
                capabilities,
                system_prompt="sys",
                user_prompt="user",
            )
        assert exc_info.value.message == "AI provider request failed."
        assert "HTTP 429" in exc_info.value.detail
        assert "Rate limit exceeded" in exc_info.value.detail


def test_gemini_provider_list_models():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
        api_key="test-api-key",
    )

    response_payload = {
        "models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        ]
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse(json.dumps(response_payload))
        models = provider.list_models()

        assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]

        mock_urlopen.assert_called_once()
        args, kwargs = mock_urlopen.call_args
        http_req = args[0]
        assert http_req.full_url == "https://generativelanguage.googleapis.com/v1beta/models"
        assert http_req.get_header("X-goog-api-key") == "test-api-key"


def test_gemini_provider_list_models_network_failure():
    provider = GeminiInterpreterProvider(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-flash",
    )

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        models = provider.list_models()
        assert models == []
