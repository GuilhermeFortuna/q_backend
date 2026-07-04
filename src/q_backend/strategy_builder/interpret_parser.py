"""Parse and validate raw AI interpreter JSON."""

from __future__ import annotations

import json
import re
from typing import Any

from q_backend.strategy_builder.interpret_models import (
    AiStrategyServiceErrorResponse,
    ParsedAiInterpreterPayload,
)
from q_backend.strategy_builder.spec_models import FORBIDDEN_SPEC_KEYS

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


class AiParseError(ValueError):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_error_response(self) -> AiStrategyServiceErrorResponse:
        return AiStrategyServiceErrorResponse(
            status="parse_error",
            message=self.message,
            detail=self.detail,
        )


def parse_ai_interpreter_response(raw_text: str) -> ParsedAiInterpreterPayload:
    payload = _load_json_object(raw_text)
    _reject_forbidden_top_level_keys(payload)
    _reject_executable_primary_output(payload)

    try:
        parsed = ParsedAiInterpreterPayload.model_validate(
            {
                "summary": payload.get("summary"),
                "assumptions": payload.get("assumptions", []),
                "questions": payload.get("questions", []),
                "unsupported_requests": payload.get("unsupported_requests", []),
                "change_notes": payload.get("change_notes", []),
                "strategy_spec": payload.get("strategy_spec"),
                "confidence": payload.get("confidence", 0.0),
            }
        )
    except Exception as exc:
        raise AiParseError(
            "Model response is missing required interpreter fields.",
            detail=str(exc),
        ) from exc

    if parsed.strategy_spec is not None:
        _reject_forbidden_spec_keys(parsed.strategy_spec, path="strategy_spec")
    return parsed


def _load_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if not text:
        raise AiParseError("Model returned an empty response.")

    candidates = [text]
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(loaded, dict):
            raise AiParseError("Model response must be a JSON object.")
        return loaded

    raise AiParseError(
        "Model response is not valid JSON.",
        detail=str(last_error) if last_error is not None else None,
    )


def _reject_forbidden_top_level_keys(payload: dict[str, Any]) -> None:
    for key in FORBIDDEN_SPEC_KEYS:
        if key in payload:
            raise AiParseError(
                f"Model response contained forbidden field '{key}'.",
                detail="Executable code is not allowed in AI strategy interpretation.",
            )


def _reject_executable_primary_output(payload: dict[str, Any]) -> None:
    for key in ("python", "python_code", "code", "script"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            raise AiParseError(
                "Model attempted to return executable code instead of StrategySpec JSON.",
                detail=f"Forbidden top-level field '{key}'.",
            )


def _reject_forbidden_spec_keys(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in FORBIDDEN_SPEC_KEYS:
                raise AiParseError(
                    f"StrategySpec contained forbidden field '{child_path}'.",
                    detail="Executable code is not allowed in StrategySpec.",
                )
            _reject_forbidden_spec_keys(nested, path=child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_spec_keys(item, path=f"{path}[{index}]")
