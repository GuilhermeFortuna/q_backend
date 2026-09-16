"""Evaluator decisions must come from the q_core bridge."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.candle_kernel import evaluate_bar
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.models import Signal, SignalAction
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.parity import reference_queued_signals_by_close, signals_equal


def _synthetic_ohlcv(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _compiled_for_strategy(name: str, extra: dict | None = None) -> dict:
    params = dict(default_params_for(name))
    if extra:
        params.update(extra)
    return {
        "strategy": name,
        "strategy_params": params,
        "symbol": "WIN$",
        "timeframe": "H1",
    }


def _evaluator_for(compiled: dict) -> StrategyEvaluator:
    return StrategyEvaluator(
        deployment_id="dep-1",
        identity=StrategyIdentity(
            strategy_name=compiled["strategy"],
            strategy_version=1,
            compiled_config=compiled,
            config_hash="test-hash",
            symbol="WIN$",
            timeframe="H1",
            sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        ),
    )


def _queued_from_result(result) -> tuple[list[Signal], list[Signal]]:
    exits = [Signal.model_validate(s) for s in result.queued_exit_signals]
    entries = [Signal.model_validate(s) for s in result.queued_entry_signals]
    return exits, entries


def test_evaluate_bar_patch_raises_through_ingest():
    compiled = _compiled_for_strategy("MACrossover")
    data = _synthetic_ohlcv(40)
    evaluator = _evaluator_for(compiled)
    evaluator.seed_window(data.iloc[:-1])
    with patch("q_backend.execution.evaluator.evaluate_bar", side_effect=RuntimeError("q_core bridge")):
        with pytest.raises(RuntimeError, match="q_core bridge"):
            evaluator.ingest_completed_bars(data.iloc[-1:])


def test_tampered_exit_reason_breaks_parity():
    params = {
        **default_params_for("MACrossover"),
        "trailing_stop_pct": 0.03,
        "stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
    }
    compiled = _compiled_for_strategy("MACrossover", params)
    data = _synthetic_ohlcv(80)
    strategy = build_strategy("MACrossover", params, "WIN$")

    original = evaluate_bar

    def tamper(strategy_obj, frame, signals, position, open_trades):
        exits, entries = original(strategy_obj, frame, signals, position, open_trades)
        if position == 10:
            return exits, [
                Signal(symbol=strategy_obj.symbol, action=SignalAction.BUY, strength=1.0),
            ]
        return exits, entries

    reference = reference_queued_signals_by_close(strategy, data, timeframe="H1")
    identity = StrategyIdentity(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config=compiled,
        config_hash="tamper",
        symbol="WIN$",
        timeframe="H1",
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
    )
    evaluator = StrategyEvaluator(
        deployment_id="tamper",
        identity=identity,
        strategy=strategy,
        window_bound=len(data),
    )
    with patch("q_backend.execution.evaluator.evaluate_bar", side_effect=tamper):
        results = evaluator.ingest_completed_bars(data)

    ref_by_close = {close: (exits, entries) for close, exits, entries in reference}
    mismatches = 0
    for result in results:
        ref_exits, ref_entries = ref_by_close[result.bar_close_time]
        fwd_exits, fwd_entries = _queued_from_result(result)
        if not signals_equal(ref_exits, fwd_exits) or not signals_equal(ref_entries, fwd_entries):
            mismatches += 1
    assert mismatches >= 1
