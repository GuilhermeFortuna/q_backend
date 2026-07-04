"""Unit tests for AI strategy interpretation prompt construction (WO191)."""

from __future__ import annotations

from q_backend.strategy_builder.interpret_models import ConversationMessage, StrategyInterpretRequest
from q_backend.strategy_builder.interpret_prompt import (
    _EARLIER_TURNS_OMITTED,
    build_system_prompt,
    build_user_prompt,
)
from q_backend.strategy_builder.registry import build_capability_registry
from q_backend.strategy_builder.spec_models import ValidationErrorDetail


def _message(role: str, content: str) -> ConversationMessage:
    return ConversationMessage(role=role, content=content)  # type: ignore[arg-type]


def test_empty_conversation_prompt_matches_pre_wo_shape():
    request = StrategyInterpretRequest(message="Create an EMA crossover strategy.")
    prompt = build_user_prompt(request)

    assert prompt == (
        "User message:\n"
        "Create an EMA crossover strategy.\n\n"
        "Respond with JSON only. Do not include commentary outside the JSON object."
    )


def test_conversation_over_turn_limit_drops_oldest_whole_turns():
    conversation = [
        _message("user", f"turn-{index}")
        for index in range(15)
    ]
    request = StrategyInterpretRequest(
        message="latest user turn",
        conversation=conversation,
    )

    prompt = build_user_prompt(request, max_conversation_turns=5, conversation_char_budget=10_000)

    assert prompt.count(_EARLIER_TURNS_OMITTED) == 1
    assert "turn-0" not in prompt
    assert "turn-9" not in prompt
    assert "turn-10" in prompt
    assert "turn-14" in prompt


def test_conversation_over_char_budget_drops_oldest_whole_turns():
    conversation = [
        _message("user", "x" * 100),
        _message("assistant", "y" * 100),
        _message("user", "z" * 100),
    ]
    request = StrategyInterpretRequest(
        message="current",
        conversation=conversation,
    )

    prompt = build_user_prompt(
        request,
        max_conversation_turns=12,
        conversation_char_budget=220,
    )

    assert prompt.count(_EARLIER_TURNS_OMITTED) == 1
    assert "xxx" not in prompt
    assert "yyy" in prompt
    assert "zzz" in prompt


def test_conversation_under_budget_keeps_all_turns_without_marker():
    conversation = [
        _message("user", "first"),
        _message("assistant", "second"),
    ]
    request = StrategyInterpretRequest(
        message="third",
        conversation=conversation,
    )

    prompt = build_user_prompt(request, max_conversation_turns=12, conversation_char_budget=8000)

    assert _EARLIER_TURNS_OMITTED not in prompt
    assert "user: first" in prompt
    assert "assistant: second" in prompt


def test_prompt_construction_is_deterministic():
    request = StrategyInterpretRequest(
        message="Refine the stop.",
        conversation=[
            _message("user", "Create a trend strategy."),
            _message("assistant", "Draft ready."),
            _message("user", "Refine the stop."),
        ],
    )

    first = build_user_prompt(request, max_conversation_turns=12, conversation_char_budget=8000)
    second = build_user_prompt(request, max_conversation_turns=12, conversation_char_budget=8000)

    assert first == second


def test_dedupe_drops_trailing_user_turn_equal_to_message():
    request = StrategyInterpretRequest(
        message="Tighten the stop loss.",
        conversation=[
            _message("assistant", "What symbol?"),
            _message("user", "Tighten the stop loss."),
        ],
    )

    prompt = build_user_prompt(request)

    assert prompt.count("Tighten the stop loss.") == 1
    assert "assistant: What symbol?" in prompt


def test_dedupe_preserves_different_trailing_user_turn():
    request = StrategyInterpretRequest(
        message="Tighten the stop loss.",
        conversation=[
            _message("assistant", "What symbol?"),
            _message("user", "Use PETR4."),
        ],
    )

    prompt = build_user_prompt(request)

    assert "user: Use PETR4." in prompt
    assert "Tighten the stop loss." in prompt


def test_current_spec_and_validation_errors_are_never_trimmed():
    request = StrategyInterpretRequest(
        message="Fix it.",
        current_spec={"name": "draft", "schema_version": "strategy_spec.v1"},
        validation_errors=[
            ValidationErrorDetail(
                path="timeframe",
                code="unsupported_timeframe",
                message="bad",
                suggestions=["D1"],
            )
        ],
        conversation=[_message("user", f"old-{index}") for index in range(20)],
    )

    prompt = build_user_prompt(request, max_conversation_turns=2, conversation_char_budget=50)

    assert "Current StrategySpec draft:" in prompt
    assert '"name": "draft"' in prompt
    assert "Previous validation errors to repair:" in prompt
    assert _EARLIER_TURNS_OMITTED in prompt


def test_system_prompt_lists_change_notes_output_key():
    prompt = build_system_prompt(build_capability_registry())

    assert "- change_notes (array of strings" in prompt
    assert "11. When a Current StrategySpec draft is provided" in prompt
    assert "13. When the conversation shows the user answering" in prompt
