"""Prompt construction for AI strategy interpretation."""

from __future__ import annotations

import json

from q_backend.strategy_builder.capability_models import CapabilityRegistry, SCHEMA_VERSION
from q_backend.strategy_builder.interpret_models import StrategyInterpretRequest
from q_backend.strategy_builder.spec_models import SCHEMA_VERSION as STRATEGY_SPEC_VERSION


def build_system_prompt(capabilities: CapabilityRegistry) -> str:
    registry_json = json.dumps(
        capabilities.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    )
    return f"""You are Q's strategy builder assistant. Convert user requests into a structured trading strategy specification.

You must obey these rules:
1. Use ONLY capabilities listed in the Q capability registry below. Never invent indicators, operators, markets, timeframes, or execution semantics that are not supported.
2. Output JSON only. Do not wrap the JSON in markdown fences.
3. Never output executable Python, scripts, or code as the primary representation.
4. If the user asks for unsupported features, keep them in unsupported_requests and do not silently drop them.
5. Ask targeted questions in questions when required details are ambiguous.
6. Record reasonable assumptions in assumptions when you fill in missing but standard details.
7. When validation_errors from a previous attempt are provided, repair the StrategySpec draft instead of repeating the same mistake.
8. strategy_spec must be null when you cannot produce a draft, otherwise it must follow strategy_spec.v1 exactly.
9. Use closed-bar signals and next-bar-open execution assumptions unless the registry allows otherwise.
10. MVP is long-only: execution_assumptions.allow_short must be false and live_trading must be false.

Return a single JSON object with exactly these keys:
- summary (string)
- assumptions (array of strings)
- questions (array of strings)
- unsupported_requests (array of strings)
- strategy_spec (object or null)
- confidence (number from 0.0 to 1.0)

strategy_spec schema version: {STRATEGY_SPEC_VERSION}
Required strategy_spec fields when non-null: schema_version, name, universe, market, timeframe, indicators, entry, exit, risk, execution_assumptions.

Capability registry schema version: {SCHEMA_VERSION}
{registry_json}
"""


def build_user_prompt(request: StrategyInterpretRequest) -> str:
    sections: list[str] = [f"User message:\n{request.message.strip()}"]

    if request.current_spec is not None:
        sections.append(
            "Current StrategySpec draft:\n"
            + json.dumps(request.current_spec, indent=2, sort_keys=True)
        )

    if request.validation_errors:
        errors_payload = [
            error.model_dump(mode="json") for error in request.validation_errors
        ]
        sections.append(
            "Previous validation errors to repair:\n"
            + json.dumps(errors_payload, indent=2, sort_keys=True)
        )

    if request.conversation:
        history_lines = [
            f"{message.role}: {message.content}" for message in request.conversation
        ]
        sections.append("Conversation history:\n" + "\n".join(history_lines))

    sections.append(
        "Respond with JSON only. Do not include commentary outside the JSON object."
    )
    return "\n\n".join(sections)
