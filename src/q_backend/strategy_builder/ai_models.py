"""Curated local model registry for the AI strategy builder."""

from __future__ import annotations

from dataclasses import dataclass

from q_backend.storage.settings import Settings
from q_backend.strategy_builder.providers.factory import AiMisconfiguredError


@dataclass(frozen=True)
class CuratedModelOption:
    id: str
    label: str


def parse_ai_strategy_models(raw: str) -> list[CuratedModelOption]:
    options: list[CuratedModelOption] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if ":" in token:
            model_id, label = token.split(":", 1)
            model_id = model_id.strip()
            label = label.strip() or model_id
        else:
            model_id = token
            label = model_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        options.append(CuratedModelOption(id=model_id, label=label))
    return options


def build_curated_model_options(settings: Settings) -> list[CuratedModelOption]:
    parsed = parse_ai_strategy_models(settings.ai_strategy_models)
    if parsed:
        return parsed
    default_model = settings.ai_strategy_model.strip()
    if not default_model:
        return []
    return [CuratedModelOption(id=default_model, label=default_model)]


def build_model_allowlist(settings: Settings) -> list[str]:
    allowlist: list[str] = []
    seen: set[str] = set()
    for option in build_curated_model_options(settings):
        if option.id not in seen:
            seen.add(option.id)
            allowlist.append(option.id)
    default_model = settings.ai_strategy_model.strip()
    if default_model and default_model not in seen:
        allowlist.insert(0, default_model)
    return allowlist


def resolve_interpret_model(request_model: str | None, settings: Settings) -> str:
    allowlist = build_model_allowlist(settings)
    if not allowlist:
        raise AiMisconfiguredError("Q_AI_STRATEGY_MODEL must be set.")

    selected = (request_model or settings.ai_strategy_model).strip()
    if not selected:
        raise AiMisconfiguredError("Q_AI_STRATEGY_MODEL must be set.")
    if selected not in allowlist:
        raise AiMisconfiguredError(
            f"Model '{selected}' is not in the configured allowlist. "
            f"Allowed models: {', '.join(allowlist)}."
        )
    return selected


def is_model_available(model_id: str, provider_model_ids: list[str]) -> bool:
    if not provider_model_ids:
        return False
    normalized = model_id.strip().lower()
    for provider_id in provider_model_ids:
        provider_normalized = provider_id.strip().lower()
        if normalized == provider_normalized:
            return True
        if normalized in provider_normalized or provider_normalized in normalized:
            return True
    return False
