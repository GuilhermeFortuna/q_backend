"""OpenAI-compatible chat completion provider for strategy interpretation."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.interpret_models import StrategyInterpretRequest
from q_backend.strategy_builder.providers.base import RawAiResponse

logger = logging.getLogger(__name__)


class ProviderRequestError(RuntimeError):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class OpenAICompatibleInterpreterProvider:
    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: int = 60,
        max_output_tokens: int = 4096,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    def interpret(
        self,
        request: StrategyInterpretRequest,
        capabilities: CapabilityRegistry,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> RawAiResponse:
        del capabilities
        del request
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if self.max_output_tokens > 0:
            payload["max_tokens"] = self.max_output_tokens

        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        http_request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "AI provider HTTP error status=%s model=%s",
                exc.code,
                self.model,
            )
            raise ProviderRequestError(
                "AI provider request failed.",
                detail=f"HTTP {exc.code}: {error_body[:500]}",
            ) from exc
        except TimeoutError as exc:
            raise ProviderRequestError(
                "AI provider request timed out.",
                detail=f"Timed out after {self.timeout_seconds}s.",
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderRequestError(
                "AI provider is unreachable.",
                detail=str(exc.reason),
            ) from exc

        try:
            decoded = json.loads(raw)
            content = decoded["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("AI provider returned an unexpected response shape.")
            raise ProviderRequestError(
                "AI provider returned an unexpected response shape.",
                detail=str(exc),
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise ProviderRequestError("AI provider returned empty content.")

        return RawAiResponse(
            content=content,
            model=self.model,
            provider=self.provider_name,
        )
