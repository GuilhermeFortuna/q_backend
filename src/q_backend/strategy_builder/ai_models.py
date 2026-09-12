"""Curated local model registry for the AI strategy builder."""

from __future__ import annotations

from dataclasses import dataclass

from q_backend.storage.settings import Settings
from q_backend.strategy_builder.providers.factory import AiMisconfiguredError

DEFAULT_GEMINI_MODELS = "gemini-2.5-flash|Gemini 2.5 Flash,gemini-2.5-pro|Gemini 2.5 Pro"


@dataclass(frozen=True)
class CuratedModelOption:
    id: str
    label: str


def parse_ai_strategy_models(raw: str) -> list[CuratedModelOption]:
    options: list[CuratedModelOption] = []
    seen: set[str] = set()
    # Entries are "model_id|Label" pairs. The "|" delimiter (rather than ":") is
    # deliberate: Ollama model ids embed a colon in their "name:tag" form (e.g.
    # "gemma4-e4b:latest"), so a ":" separator would split the id itself.
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if "|" in token:
            model_id, label = token.split("|", 1)
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


def _curated_models_raw(settings: Settings, provider_id: str) -> str:
    if provider_id == "gemini":
        raw = settings.ai_strategy_gemini_models.strip()
        return raw or DEFAULT_GEMINI_MODELS
    return settings.ai_strategy_models


def build_curated_model_options(
    settings: Settings,
    provider_id: str = "openai_compatible",
) -> list[CuratedModelOption]:
    parsed = parse_ai_strategy_models(_curated_models_raw(settings, provider_id))
    if parsed:
        return parsed
    default_model = settings.ai_strategy_model.strip()
    if provider_id == settings.ai_strategy_provider.strip().lower() and default_model:
        return [CuratedModelOption(id=default_model, label=default_model)]
    return []


def build_model_allowlist(
    settings: Settings,
    provider_id: str = "openai_compatible",
) -> list[str]:
    allowlist: list[str] = []
    seen: set[str] = set()
    for option in build_curated_model_options(settings, provider_id):
        if option.id not in seen:
            seen.add(option.id)
            allowlist.append(option.id)
    if provider_id == settings.ai_strategy_provider.strip().lower():
        default_model = settings.ai_strategy_model.strip()
        if default_model and default_model not in seen:
            allowlist.insert(0, default_model)
    return allowlist


def resolve_provider_default_model(settings: Settings, provider_id: str) -> str:
    allowlist = build_model_allowlist(settings, provider_id)
    if not allowlist:
        if provider_id == settings.ai_strategy_provider.strip().lower():
            default_model = settings.ai_strategy_model.strip()
            if default_model:
                return default_model
        raise AiMisconfiguredError(f"No curated models configured for provider '{provider_id}'.")

    if provider_id == settings.ai_strategy_provider.strip().lower():
        selected = settings.ai_strategy_model.strip()
        if selected in allowlist:
            return selected
    return allowlist[0]


def resolve_interpret_model(
    request_model: str | None,
    settings: Settings,
    provider_id: str | None = None,
) -> str:
    resolved_provider = (provider_id or settings.ai_strategy_provider).strip().lower()
    allowlist = build_model_allowlist(settings, resolved_provider)
    if not allowlist:
        raise AiMisconfiguredError(f"No curated models configured for provider '{resolved_provider}'.")

    if request_model is None or not request_model.strip():
        selected = resolve_provider_default_model(settings, resolved_provider)
    else:
        selected = request_model.strip()

    if not selected:
        raise AiMisconfiguredError("Q_AI_STRATEGY_MODEL must be set.")
    if selected not in allowlist:
        raise AiMisconfiguredError(
            f"Model '{selected}' is not in the configured allowlist. " f"Allowed models: {', '.join(allowlist)}."
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
