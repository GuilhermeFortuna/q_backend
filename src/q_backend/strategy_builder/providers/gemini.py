"""Gemini chat completion provider for strategy interpretation."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from q_backend.strategy_builder.capability_models import CapabilityRegistry
from q_backend.strategy_builder.interpret_models import StrategyInterpretRequest
from q_backend.strategy_builder.providers.base import RawAiResponse, ProviderRequestError

logger = logging.getLogger(__name__)


class GeminiInterpreterProvider:
    """Gemini-native strategy interpreter provider.

    Endpoints:
      - POST {base_url}/models/{model}:generateContent (with x-goog-api-key header)
      - GET  {base_url}/models (with x-goog-api-key header)

    Uses responseMimeType="application/json" config to ensure the API outputs valid
    JSON directly, without markdown code block fences.

    Escape Hatch:
      Note that Google Gemini also exposes an OpenAI-compatible endpoint
      ({base_url}/openai/ with a Bearer key) which works with the existing
      openai_compatible provider as a config-only escape hatch. We still build
      this native provider for first-class error mapping, the x-goog-api-key header,
      and responseMimeType JSON mode.
    """

    provider_name = "gemini"

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
            headers["x-goog-api-key"] = self.api_key
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
            models_list = decoded.get("models", [])
            if not isinstance(models_list, list):
                logger.info("AI provider returned an unexpected models response structure.")
                return []

            model_ids: list[str] = []
            for entry in models_list:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str):
                    continue
                supported_methods = entry.get("supportedGenerationMethods", [])
                if not isinstance(supported_methods, list) or "generateContent" not in supported_methods:
                    continue

                # return ids stripped of the `models/` prefix
                clean_id = name
                if clean_id.startswith("models/"):
                    clean_id = clean_id[len("models/") :]
                model_ids.append(clean_id)
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
        clean_model = selected_model
        if clean_model.startswith("models/"):
            clean_model = clean_model[len("models/") :]

        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        if self.max_output_tokens > 0:
            payload["generationConfig"]["maxOutputTokens"] = self.max_output_tokens

        url = f"{self.base_url}/models/{clean_model}:generateContent"
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
        except json.JSONDecodeError as exc:
            logger.warning("AI provider returned an unexpected response shape.")
            raise ProviderRequestError(
                "AI provider returned an unexpected response shape.",
                detail=str(exc),
            ) from exc

        # Extract candidates, promptFeedback, finishReason, blockReason
        candidates = decoded.get("candidates")
        prompt_feedback = decoded.get("promptFeedback")

        block_reason = None
        if isinstance(prompt_feedback, dict):
            block_reason = prompt_feedback.get("blockReason")

        finish_reason = None
        if isinstance(candidates, list) and len(candidates) > 0:
            first_cand = candidates[0]
            if isinstance(first_cand, dict):
                finish_reason = first_cand.get("finishReason")

        # Now extract parts
        text_content = ""
        has_parts = False
        if isinstance(candidates, list) and len(candidates) > 0:
            first_cand = candidates[0]
            if isinstance(first_cand, dict):
                content_dict = first_cand.get("content")
                if isinstance(content_dict, dict):
                    parts_list = content_dict.get("parts")
                    if isinstance(parts_list, list):
                        has_parts = True
                        text_content = "".join(
                            part.get("text", "") for part in parts_list if isinstance(part, dict) and "text" in part
                        )

        if not has_parts or not text_content.strip():
            detail_parts = []
            if finish_reason:
                detail_parts.append(f"finishReason: {finish_reason}")
            if block_reason:
                detail_parts.append(f"blockReason: {block_reason}")

            err_msg = "AI provider returned empty content."
            if detail_parts:
                err_detail = f"{err_msg} ({', '.join(detail_parts)})"
            else:
                err_detail = err_msg

            raise ProviderRequestError(err_msg, detail=err_detail)

        return RawAiResponse(
            content=text_content,
            model=selected_model,
            provider=self.provider_name,
        )
