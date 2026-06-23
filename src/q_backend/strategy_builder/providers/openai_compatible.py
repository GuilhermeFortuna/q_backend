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

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def list_models(self) -> list[str]:
        url = f"{self.base_url}/models"
        http_request = urllib.request.Request(
            url,
            headers=self._request_headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError) as exc:
            logger.info("AI provider model listing unavailable: %s", exc)
            return []

        try:
            decoded = json.loads(raw)
            data = decoded["data"]
            model_ids: list[str] = []
            for entry in data:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    model_ids.append(entry["id"])
            return model_ids
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.info("AI provider returned an unexpected models response: %s", exc)
            return []

    def interpret(
        self,
        request: StrategyInterpretRequest,
        capabilities: CapabilityRegistry,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
    ) -> RawAiResponse:
        del capabilities
        selected_model = model or self.model
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": 0.2,
        }
        if self.max_output_tokens > 0:
            payload["max_tokens"] = self.max_output_tokens

        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = self._request_headers()

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
                selected_model,
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
            model=selected_model,
            provider=self.provider_name,
        )
