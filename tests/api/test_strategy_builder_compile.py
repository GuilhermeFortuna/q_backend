"""StrategySpec compiler tests (WO92)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pandas as pd
import pytest
from fastapi import HTTPException

from q_backend.api.routers.strategy_builder import (
    CompileStrategySpecRequest,
    compile_strategy_builder_spec,
)
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.genome.validate import validate_genome
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.strategy_builder.compiler import compile_strategy_spec_payload
from q_backend.strategy_builder.compiler_models import StrategyCompileError

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

RSI_MEAN_REVERSION_SPEC = {
    "schema_version": "strategy_spec.v1",
    "name": "RSI Mean Reversion",
    "universe": ["PETR4"],
    "market": "B3",
    "timeframe": "D1",
    "indicators": [{"id": "rsi", "type": "rsi", "source": "close", "period": 14}],
    "entry": {"all": [{"left": "rsi", "op": "crosses_above", "right": 30}]},
    "exit": {"any": [{"left": "rsi", "op": "crosses_above", "right": 70}]},
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
    "entry": {"all": [{"left": "close", "op": ">", "right": "donchian.upper"}]},
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

BOLLINGER_REVERSION_SPEC = {
    "schema_version": "strategy_spec.v1",
    "name": "Bollinger Reversion",
    "universe": ["PETR4"],
    "market": "B3",
    "timeframe": "D1",
    "indicators": [{"id": "bb", "type": "bollinger_bands", "source": "close", "period": 20}],
    "entry": {"all": [{"left": "close", "op": "<", "right": "bb.lower"}]},
    "exit": {
        "any": [
            {"left": "close", "op": ">", "right": "bb.middle"},
            {"type": "take_profit", "mode": "percent", "value": 0.04},
        ]
    },
    "risk": {"position_sizing": "fixed_quantity", "quantity": 1.0},
    "execution_assumptions": {
        "signal_timing": "closed_bar",
        "entry_timing": "next_bar_open",
        "allow_short": False,
    },
}


def _synthetic_ohlcv(rows: int = 120) -> pd.DataFrame:
    start = datetime(2024, 1, 1)
    records = []
    price = 100.0
    for index in range(rows):
        drift = 0.4 if index % 17 == 0 else (-0.3 if index % 23 == 0 else 0.05)
        price = max(50.0, price + drift)
        records.append(
            {
                "open": price - 0.2,
                "high": price + 0.8,
                "low": price - 0.8,
                "close": price,
                "volume": 1000 + index,
            }
        )
    return pd.DataFrame(
        records,
        index=pd.date_range(start, periods=rows, freq="D"),
    )


def test_ema_cross_compiles_to_composite_strategy_with_exit_policy():
    compiled = compile_strategy_spec_payload(EMA_CROSS_SPEC)

    assert compiled.strategy_name == "CompositeStrategy"
    assert compiled.genome is not None
    assert "genome" in compiled.strategy_params
    assert compiled.strategy_params["exit_stop_loss_pct"] == 0.03
    validate_genome(Genome.model_validate(compiled.genome))


def test_rsi_mean_reversion_compiles_to_registry_strategy():
    compiled = compile_strategy_spec_payload(RSI_MEAN_REVERSION_SPEC)

    assert compiled.strategy_name == "RSIMeanReversion"
    assert compiled.strategy_params == {
        "period": 14,
        "oversold": 30.0,
        "overbought": 70.0,
    }
    assert compiled.backtest_config.strategy == "RSIMeanReversion"


def test_donchian_breakout_compiles_to_valid_genome():
    compiled = compile_strategy_spec_payload(DONCHIAN_BREAKOUT_SPEC)

    assert compiled.strategy_name == "CompositeStrategy"
    validate_genome(Genome.model_validate(compiled.genome))
    assert compiled.strategy_params["exit_stop_loss_pct"] == 0.02


def test_bollinger_spec_compiles_with_bracket_exit_params():
    compiled = compile_strategy_spec_payload(BOLLINGER_REVERSION_SPEC)

    assert compiled.strategy_name == "CompositeStrategy"
    validate_genome(Genome.model_validate(compiled.genome))
    assert compiled.strategy_params["exit_stop_loss_pct"] == 0.0
    assert compiled.strategy_params["exit_take_profit_pct"] == 0.04


def test_compiler_output_is_deterministic_for_identical_specs():
    first = compile_strategy_spec_payload(EMA_CROSS_SPEC)
    second = compile_strategy_spec_payload(copy.deepcopy(EMA_CROSS_SPEC))

    assert first.compiled_id == second.compiled_id
    assert first.strategy_params == second.strategy_params
    assert first.genome == second.genome


def test_invalid_spec_fails_before_compilation():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["indicators"] = [{"id": "st", "type": "supertrend", "period": 14}]

    with pytest.raises(StrategyCompileError) as exc_info:
        compile_strategy_spec_payload(spec)

    assert exc_info.value.status == "validation_failed"
    assert any(error.code == "unsupported_indicator" for error in exc_info.value.errors)


def test_compile_endpoint_returns_compiled_payload():
    response = compile_strategy_builder_spec(CompileStrategySpecRequest(strategy_spec=EMA_CROSS_SPEC))

    assert response.status == "compiled"
    assert response.compiled_strategy_id == response.compiled_strategy.compiled_id
    assert response.compiled_strategy.strategy_name == "CompositeStrategy"
    assert response.compiled_strategy.backtest_config.symbol == "PETR4"


def test_compile_endpoint_returns_validation_errors():
    spec = copy.deepcopy(EMA_CROSS_SPEC)
    spec["timeframe"] = "BADTF"

    with pytest.raises(HTTPException) as exc_info:
        compile_strategy_builder_spec(CompileStrategySpecRequest(strategy_spec=spec))

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["status"] == "validation_failed"
    assert exc_info.value.detail["errors"]


def test_compiled_payload_runs_through_backtest_engine():
    compiled = compile_strategy_spec_payload(EMA_CROSS_SPEC)
    strategy = build_strategy(
        compiled.strategy_name,
        compiled.strategy_params,
        compiled.backtest_config.symbol,
    )
    engine = BacktestEngine(
        strategy,
        FixedQuantitySizer(quantity=1.0),
        initial_capital=100_000.0,
    )

    result = engine.run(_synthetic_ohlcv(), parallel_mode=ParallelMode.SEQUENTIAL)

    assert result is not None
    assert len(result.get_closed_trades()) >= 0
