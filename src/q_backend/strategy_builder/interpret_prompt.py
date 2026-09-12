"""Prompt construction for AI strategy interpretation."""

from __future__ import annotations

import json

from q_backend.strategy_builder.capability_models import CapabilityRegistry, SCHEMA_VERSION
from q_backend.strategy_builder.interpret_models import ConversationMessage, StrategyInterpretRequest
from q_backend.strategy_builder.spec_models import SCHEMA_VERSION as STRATEGY_SPEC_VERSION

_EARLIER_TURNS_OMITTED = "[earlier turns omitted]"

# Optimizer-only metadata on parameter specs. The NL interpreter builds a spec
# from the capability vocabulary; these search ranges only matter to the
# optimizer, so they are stripped from the prompt copy of the registry.
_OPTIMIZER_ONLY_PARAM_KEYS: tuple[str, ...] = (
    "search_min",
    "search_max",
    "search_step",
    "search_scale",
    "searchable",
)


def _strip_keys(obj: object, keys: tuple[str, ...]) -> None:
    """Recursively delete the given keys from every nested dict, in place."""
    if isinstance(obj, dict):
        for key in keys:
            obj.pop(key, None)
        for value in obj.values():
            _strip_keys(value, keys)
    elif isinstance(obj, list):
        for item in obj:
            _strip_keys(item, keys)


def _slim_registry_for_prompt(capabilities: CapabilityRegistry) -> dict:
    """Produce a token-lean view of the registry for embedding in the system prompt.

    The full registry (used by /capabilities and validation) is ~34k tokens, which
    overflows modest local-model context windows. The bulk is the per-strategy
    ``params`` lists — reference detail the interpreter does not need, since it
    builds specs from the genome/exit vocabulary, not from template parameters.
    We keep the strategy catalog (name/label/description/thesis) but drop those
    params, and strip optimizer-only search metadata everywhere.
    """
    data = capabilities.model_dump(mode="json")
    for strategy in data.get("strategies", []):
        strategy.pop("params", None)
    _strip_keys(data, _OPTIMIZER_ONLY_PARAM_KEYS)
    return data


def build_system_prompt(capabilities: CapabilityRegistry) -> str:
    # Serialize a slimmed, compact registry: the full document is ~34k tokens and
    # overflows modest local-model context windows. Slimming + compaction keeps the
    # interpreter's vocabulary while fitting the prompt into a usable context.
    registry_json = json.dumps(
        _slim_registry_for_prompt(capabilities),
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"""You are Q's strategy builder assistant — a collaborator who turns trading ideas into structured strategy specifications through conversation. Users may arrive with a complete spec or a rough thought; both are valid openings.

You must obey these rules:
1. Use ONLY capabilities listed in the Q capability registry below. Never invent indicators, operators, markets, timeframes, or execution semantics that are not supported.
2. Output JSON only. Do not wrap the JSON in markdown fences.
3. Never output executable Python, scripts, or code as the primary representation.
4. If the user asks for unsupported features, keep them in unsupported_requests and do not silently drop them.
5. When a request is partial or high-level: populate strategy_spec with what was actually specified plus clearly-flagged standard assumptions, and return at most 3 questions in questions, ordered by impact. Questions must be answerable in a short phrase without trivia.
6. Record reasonable assumptions in assumptions when you fill in missing but standard details.
7. When validation_errors from a previous attempt are provided, repair the StrategySpec draft instead of repeating the same mistake.
8. strategy_spec must be null when you cannot produce a draft, otherwise it must follow strategy_spec.v1 exactly.
9. Use closed-bar signals and next-bar-open execution assumptions unless the registry allows otherwise.
10. MVP is long-only: execution_assumptions.allow_short must be false and live_trading must be false.
11. When a Current StrategySpec draft is provided, treat it as the shared working draft: apply only the changes the user asked for and preserve every unrelated field verbatim.
12. Prefer targeted questions over guessing. Guess and flag standard parameters (per rule 6), but ask for core intent (direction, market, style) instead of guessing.
13. When the conversation shows the user answering a previous question, incorporate that answer instead of re-asking the same question.

Return a single JSON object with exactly these keys:
- summary (string)
- assumptions (array of strings)
- questions (array of strings)
- unsupported_requests (array of strings)
- change_notes (array of strings — short, concrete edits made to the draft this turn, e.g. "tightened stop_loss_points 200 -> 150"; empty when producing a first draft)
- strategy_spec (object or null)
- confidence (number from 0.0 to 1.0)

strategy_spec schema version: {STRATEGY_SPEC_VERSION}
Required strategy_spec fields when non-null: schema_version, name, universe, market, timeframe, indicators, entry, exit, risk, execution_assumptions.

Capability registry schema version: {SCHEMA_VERSION}
{registry_json}
"""


def _format_conversation_turn(message: ConversationMessage) -> str:
    return f"{message.role}: {message.content}"


def _dedupe_trailing_user_message(
    conversation: list[ConversationMessage],
    message: str,
) -> list[ConversationMessage]:
    if not conversation:
        return conversation
    last = conversation[-1]
    if last.role == "user" and last.content == message.strip():
        return conversation[:-1]
    return conversation


def _trim_conversation_history(
    messages: list[ConversationMessage],
    *,
    max_turns: int,
    char_budget: int,
) -> tuple[list[ConversationMessage], bool]:
    """Keep the most recent whole turns that fit both limits; never split a turn."""
    if not messages:
        return [], False

    kept_reversed: list[ConversationMessage] = []
    char_count = 0

    for msg in reversed(messages):
        line = _format_conversation_turn(msg)
        additional = len(line) + (1 if kept_reversed else 0)

        if kept_reversed and (len(kept_reversed) >= max_turns or char_count + additional > char_budget):
            break

        kept_reversed.append(msg)
        char_count += additional

    kept = list(reversed(kept_reversed))
    return kept, len(kept) < len(messages)


def build_user_prompt(
    request: StrategyInterpretRequest,
    *,
    max_conversation_turns: int = 12,
    conversation_char_budget: int = 8000,
) -> str:
    sections: list[str] = [f"User message:\n{request.message.strip()}"]

    if request.current_spec is not None:
        sections.append("Current StrategySpec draft:\n" + json.dumps(request.current_spec, indent=2, sort_keys=True))

    if request.validation_errors:
        errors_payload = [error.model_dump(mode="json") for error in request.validation_errors]
        sections.append(
            "Previous validation errors to repair:\n" + json.dumps(errors_payload, indent=2, sort_keys=True)
        )

    history = _dedupe_trailing_user_message(request.conversation, request.message)
    trimmed, omitted = _trim_conversation_history(
        history,
        max_turns=max_conversation_turns,
        char_budget=conversation_char_budget,
    )
    if trimmed:
        history_lines = [_format_conversation_turn(message) for message in trimmed]
        if omitted:
            history_lines.insert(0, _EARLIER_TURNS_OMITTED)
        sections.append("Conversation history:\n" + "\n".join(history_lines))

    sections.append("Respond with JSON only. Do not include commentary outside the JSON object.")
    return "\n\n".join(sections)
