"""Restart-and-replay must reconstruct exit-rule state for trailing, PSAR and time stops."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.models import OrderAction, Signal, Trade
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.execution.bars import bar_close_time
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.parity import signals_equal


def _identity(*, strategy_name: str, compiled_config: dict) -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name=strategy_name,
        strategy_version=1,
        compiled_config=compiled_config,
        config_hash="test-hash",
        symbol="WIN$",
        timeframe="H1",
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
    )


def _synthetic_ohlcv(n: int = 200, *, freq: str = "h", profile: str = "default") -> pd.DataFrame:
    close = np.full(n, 100.0)
    if profile == "trailing":
        close[41:121] = np.linspace(100.0, 140.0, 121 - 41)
        close[121:160] = np.linspace(140.0, 120.0, 160 - 121)
        close[160:170] = np.linspace(120.0, 125.0, 170 - 160)
        close[170:] = np.linspace(125.0, 118.0, n - 170)
    else:
        close[41:170] = np.linspace(100.0, 130.0, 170 - 41)
        close[170:] = np.linspace(130.0, 118.0, n - 170)
    open_ = close.copy()
    high = close + 1.0
    low = close - 1.0
    volume = np.full(n, 1_000.0)
    index = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
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


def _evaluator_for(compiled: dict, *, deployment_id: str = "dep-1", data: pd.DataFrame) -> StrategyEvaluator:
    params = compiled["strategy_params"]
    strategy = build_strategy(compiled["strategy"], params, compiled.get("symbol", "WIN$"))
    return StrategyEvaluator(
        deployment_id=deployment_id,
        identity=_identity(strategy_name=compiled.get("strategy", "MACrossover"), compiled_config=compiled),
        strategy=strategy,
        window_bound=len(data),
    )


def _queued_from_result(result) -> tuple[list[Signal], list[Signal]]:
    exits = [Signal.model_validate(s) for s in result.queued_exit_signals]
    entries = [Signal.model_validate(s) for s in result.queued_entry_signals]
    return exits, entries


@dataclass(frozen=True)
class RecoveryCase:
    exit_params: dict
    label: str


CASES = (
    RecoveryCase({"trailing_stop_pct": 0.03}, "trailing"),
    RecoveryCase({"psar_af_start": 0.02, "psar_af_step": 0.02, "psar_af_max": 0.2}, "psar"),
    RecoveryCase({"max_bars_in_trade": 15}, "time_stop"),
)


def _open_trade(data, deployment_id: str = "exec-dep-1", *, entry_idx: int = 40) -> Trade:
    return Trade(
        id=f"{deployment_id}-trade",
        order_id="o1",
        symbol="WIN$",
        action=OrderAction.BUY,
        quantity=1.0,
        entry_time=data.index[entry_idx].to_pydatetime(),
        entry_price=float(data.iloc[entry_idx]["close"]),
    )


def _decisions_by_close(evaluator: StrategyEvaluator, results) -> dict:
    out = {}
    for result in results:
        out[result.bar_close_time] = result
    return out


def _run_uninterrupted(compiled: dict, data, trade: Trade) -> dict:
    evaluator = _evaluator_for(compiled, deployment_id="dep-live", data=data)
    evaluator.set_open_trade(trade)
    results = []
    for i in range(len(data)):
        results.extend(evaluator.ingest_completed_bars(data.iloc[i : i + 1]))
    return _decisions_by_close(evaluator, results)


def _run_with_replay(compiled: dict, data, trade: Trade, *, replay: bool) -> dict:
    evaluator = _evaluator_for(compiled, deployment_id="dep-replay", data=data)
    evaluator.set_open_trade(trade)
    evaluator.seed_window(data.iloc[:121])
    replay_end = bar_close_time(data.index[159].to_pydatetime(), "H1")
    results: list = []
    if replay:
        evaluator.replay_recovery(data.iloc[:160], end_close=replay_end)
    for i in range(160, len(data)):
        results.extend(evaluator.ingest_completed_bars(data.iloc[i : i + 1]))
    return _decisions_by_close(evaluator, results)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.label)
def test_restart_replay_matches_uninterrupted_evaluation(case: RecoveryCase):
    params = {**default_params_for("MACrossover"), **case.exit_params, "stop_loss_pct": 0.0, "take_profit_pct": 0.0}
    compiled = _compiled_for_strategy("MACrossover", params)
    data = _synthetic_ohlcv(200, profile="trailing" if case.label == "trailing" else "default")
    entry_idx = 145 if case.label == "time_stop" else 40
    trade = _open_trade(data, entry_idx=entry_idx)

    live = _run_uninterrupted(compiled, data, trade)
    replayed = _run_with_replay(compiled, data, trade, replay=True)

    for i in range(160, len(data)):
        close_time = bar_close_time(data.index[i].to_pydatetime(), "H1")
        live_result = live[close_time]
        replay_result = replayed[close_time]
        assert live_result.signal_action == replay_result.signal_action
        assert live_result.reason == replay_result.reason
        live_exits, live_entries = _queued_from_result(live_result)
        replay_exits, replay_entries = _queued_from_result(replay_result)
        assert signals_equal(live_exits, replay_exits)
        assert signals_equal(live_entries, replay_entries)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.label)
def test_skipping_replay_changes_at_least_one_decision(case: RecoveryCase):
    params = {**default_params_for("MACrossover"), **case.exit_params, "stop_loss_pct": 0.0, "take_profit_pct": 0.0}
    compiled = _compiled_for_strategy("MACrossover", params)
    data = _synthetic_ohlcv(200, profile="trailing" if case.label == "trailing" else "default")
    entry_idx = 145 if case.label == "time_stop" else 40
    trade = _open_trade(data, entry_idx=entry_idx)

    replayed = _run_with_replay(compiled, data, trade, replay=True)
    no_replay = _run_with_replay(compiled, data, trade, replay=False)

    mismatches = 0
    for i in range(160, len(data)):
        close_time = bar_close_time(data.index[i].to_pydatetime(), "H1")
        replay_result = replayed[close_time]
        skip_result = no_replay.get(close_time)
        if skip_result is None:
            mismatches += 1
            continue
        replay_exits, replay_entries = _queued_from_result(replay_result)
        skip_exits, skip_entries = _queued_from_result(skip_result)
        if (
            replay_result.signal_action != skip_result.signal_action
            or replay_result.reason != skip_result.reason
            or not signals_equal(replay_exits, skip_exits)
            or not signals_equal(replay_entries, skip_entries)
        ):
            mismatches += 1
    assert mismatches >= 1
