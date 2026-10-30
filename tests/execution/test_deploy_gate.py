"""The deployable order kinds and the exit-rule refusal at deploy time (Q-137)."""

from __future__ import annotations

import pytest

from q_backend.execution.validation import DEPLOYABLE_ORDER_KINDS, ExecutionValidationError, _refuse_exit_rules


@pytest.mark.parametrize(
    "kind",
    [
        "market_entry",
        "market_close",
        "protective_levels",
        "amend",
        "cancel",
        "resting_stop",
        "resting_limit",
        "oco",
        "timed_exit",
        "expiring_entry",
    ],
)
def test_the_q137_kinds_are_deployable(kind: str):
    assert kind in DEPLOYABLE_ORDER_KINDS


@pytest.mark.parametrize("kind", ["priced_entry", "priced_close", "tick_hooks"])
def test_priced_orders_and_tick_hooks_stay_outside_the_deployable_set(kind: str):
    assert kind not in DEPLOYABLE_ORDER_KINDS


def test_no_exit_rules_is_deployable():
    _refuse_exit_rules({})
    _refuse_exit_rules(None)


@pytest.mark.parametrize("params", [{"trailing_stop_pct": 2.0}, {"breakeven_trigger_pct": 1.0}])
def test_enabled_exit_rules_are_refused_until_the_forward_path_applies_them(params):
    with pytest.raises(ExecutionValidationError) as exc:
        _refuse_exit_rules(params)
    assert exc.value.status_code == 422
    assert "not deployable yet" in str(exc.value)
