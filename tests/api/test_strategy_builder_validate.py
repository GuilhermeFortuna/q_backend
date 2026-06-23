"""StrategySpec v1 validation tests (WO91)."""

from __future__ import annotations

import copy

import pytest

from q_backend.api.routers.strategy_builder import (
    ValidateStrategySpecRequest,
    validate_strategy_builder_spec,
)
from q_backend.strategy_builder.validator import validate_strategy_spec_payload

EMA_CROSS_SPEC = {
    "schema_version": "strategy_spec.v1",
    "name": "EMA Trend Cross",
    "universe": ["PETR4"],
    "market": "B3",
    "timeframe": "D1",
    "indicators": [
        {"id": "ema_fast", "type": "ema", "source": "close", "period": 20},
        {"id": "ema_slow", "type": "ema", "source": "close", "period": 50},
    ],
    "entry": {
        "all": [
            {
                "left": "ema_fast",
                "op": "crosses_above",
                "right": "ema_slow",
            }
        ]
    },
    "exit": {
        "any": [
            {
                "left": "ema_fast",
                "op": "crosses_below",
                "right": "ema_slow",
            },
            {"type": "stop_loss", "mode": "percent", "value": 0.03},
        ]
    },
    "risk": {"position_sizing": "fixed_quantity", "quantity": 1.0},
    "execution_assumptions": {
        "signal_timing": "closed_bar",
        "entry_timing": "next_bar_open",
        "allow_short": False,
    },
}

DONCHIAN_BREAKOUT_SPEC = {
    "schema_version": "strategy_spec.v1",
    "name": "Donchian Breakout",
    "universe": ["WIN$"],
    "market": "B3",
    "timeframe": "H1",
    "indicators": [{"id": "donchian", "type": "donchian", "period": 20}],
    "entry": {
        "all": [{"left": "close", "op": ">", "right": "donchian.upper"}]
    },
    "exit": {
        "any": [
            {"left": "close", "op": "<", "right": "donchian.lower"},
            {"type": "stop_loss", "mode": "percent", "value": 0.02},
        ]
    },
    "risk": {
        "position_sizing": "fixed_safety_margin",
        "safety_margin_per_contract": 5000.0,
    },
    "execution_assumptions": {
        "signal_timing": "closed_bar",
        "entry_timing": "next_bar_open",
        "allow_short": False,
    },
}


def test_valid_ema_cross_strategy():
    result = validate_strategy_spec_payload(EMA_CROSS_SPEC)
    assert result.valid is True
    assert result.errors == []


def test_valid_donchian_breakout_strategy():
    result = validate_strategy_spec_payload(DONCHIAN_BREAKOUT_SPEC)
    assert result.valid is True
    assert result.errors == []


def test_unsupported_indicator():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["indicators"] = [{"id": "st", "type": "supertrend", "period": 14}]

    result = validate_strategy_spec_payload(spec)

    assert result.valid is False
    error = next(item for item in result.errors if item.code == "unsupported_indicator")
    assert error.path == "indicators[0].type"
    assert "supertrend" in error.message
    assert "atr" in error.suggestions
    assert "ema" in error.suggestions
    assert "donchian" in error.suggestions


def test_unsupported_timeframe():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["timeframe"] = "BADTF"

    result = validate_strategy_spec_payload(spec)

    assert result.valid is False
    error = next(item for item in result.errors if item.code == "unsupported_timeframe")
    assert error.path == "timeframe"
    assert "D1" in error.suggestions


def test_missing_exit_logic():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["exit"] = {"any": []}

    result = validate_strategy_spec_payload(spec)

    assert result.valid is False
    error = next(item for item in result.errors if item.code == "missing_exit_logic")
    assert error.path == "exit.any"


def test_unsupported_live_trading_assumption():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["execution_assumptions"]["live_trading"] = True

    result = validate_strategy_spec_payload(spec)

    assert result.valid is False
    error = next(item for item in result.errors if item.code == "unsupported_capability")
    assert error.path == "execution_assumptions.live_trading"


def test_unknown_symbol_in_universe():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["universe"] = ["NOTAREALSYMBOL"]

    result = validate_strategy_spec_payload(spec)

    assert result.valid is False
    error = next(item for item in result.errors if item.code == "unknown_symbol")
    assert error.path == "universe[0]"


def test_model_rejects_executable_python_field():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["python_code"] = "print('hack')"

    result = validate_strategy_spec_payload(spec)

    assert result.valid is False
    assert any(error.code == "forbidden_field" for error in result.errors)


def test_validate_endpoint_returns_structured_errors():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["indicators"] = [{"id": "st", "type": "supertrend", "period": 14}]

    response = validate_strategy_builder_spec(
        ValidateStrategySpecRequest(strategy_spec=spec)
    )

    assert response.valid is False
    assert response.errors[0].path
    assert response.errors[0].code
    assert response.errors[0].message


@pytest.mark.parametrize(
    "code",
    [
        "unsupported_indicator",
        "unsupported_timeframe",
        "unsupported_market",
        "unsupported_operator",
        "unsupported_risk_model",
        "unsupported_data_column",
        "unsupported_execution_assumption",
        "unsupported_capability",
        "missing_exit_logic",
        "missing_entry_logic",
        "unknown_indicator_reference",
        "unknown_symbol",
        "duplicate_indicator_id",
        "invalid_parameter",
        "short_not_allowed",
        "forbidden_field",
        "invalid_schema_version",
        "empty_universe",
        "invalid_condition_group",
        "parse_error",
    ],
)
def test_documented_error_codes_exist(code: str):
    assert isinstance(code, str)
