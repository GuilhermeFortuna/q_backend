"""Forward evaluator parity, deduplication, replay, and bounded-window tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.models import OrderAction, SignalAction, Trade
from q_backend.backtesting.strategy_registry import default_params_for, get_registered_strategy
from q_backend.execution.bars import bar_close_time, drop_forming_bar
from q_backend.execution.domain import SignalAction as DomainSignalAction, StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.parity import reference_queued_signals_by_close, signals_equal


def _identity(
    *,
    strategy_name: str = "MACrossover",
    compiled_config: dict,
    symbol: str = "WIN$",
    timeframe: str = "H1",
) -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name=strategy_name,
        strategy_version=1,
        compiled_config=compiled_config,
        config_hash="test-hash",
        symbol=symbol,
        timeframe=timeframe,
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
    )


def _synthetic_ohlcv(n: int = 260, *, freq: str = "h") -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
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


def _evaluator_for(
    compiled: dict,
    *,
    symbol: str = "WIN$",
    timeframe: str = "H1",
    deployment_id: str = "dep-1",
) -> StrategyEvaluator:
    return StrategyEvaluator(
        deployment_id=deployment_id,
        identity=_identity(
            strategy_name=compiled.get("strategy", "composite"),
            compiled_config=compiled,
            symbol=symbol,
            timeframe=timeframe,
        ),
    )


def _queued_from_result(result) -> tuple[list, list]:
    from q_backend.backtesting.models import Signal

    exits = [Signal.model_validate(s) for s in result.queued_exit_signals]
    entries = [Signal.model_validate(s) for s in result.queued_entry_signals]
    return exits, entries


@pytest.mark.parametrize("strategy_name", ["MACrossover", "MACD"])
def test_builtin_strategy_decision_parity(strategy_name: str):
    compiled = _compiled_for_strategy(strategy_name)
    data = _synthetic_ohlcv()
    strategy = build_strategy(strategy_name, compiled["strategy_params"], "WIN$")
    reference = reference_queued_signals_by_close(
        strategy, data, timeframe="H1"
    )

    evaluator = _evaluator_for(compiled)
    evaluator.seed_window(data.iloc[:-1])
    last = data.iloc[-1:]
    result = evaluator.ingest_completed_bars(last)[0]
    close_time = bar_close_time(last.index[-1].to_pydatetime(), "H1")
    ref_exits, ref_entries = next(
        row for row in reference if row[0] == close_time
    )[1:]
    fwd_exits, fwd_entries = _queued_from_result(result)
    assert signals_equal(ref_exits, fwd_exits)
    assert signals_equal(ref_entries, fwd_entries)


def test_composite_strategy_decision_parity():
    compiled = {
        "entries": [
            {"strategy": "MACrossover", "params": {"short_period": 5, "long_period": 20}},
            {"strategy": "MACD", "params": default_params_for("MACD")},
        ],
        "entry_manager": {"kind": "or", "params": {}},
        "exit_params": {},
        "symbol": "WIN$",
        "timeframe": "H1",
    }
    data = _synthetic_ohlcv()
    strategy = build_composite_entry(
        compiled["entries"],
        "or",
        {},
        {},
        "WIN$",
    )
    reference = reference_queued_signals_by_close(strategy, data, timeframe="H1")
    evaluator = _evaluator_for(compiled)
    evaluator.seed_window(data.iloc[:-1])
    result = evaluator.ingest_completed_bars(data.iloc[-1:])[0]
    close_time = bar_close_time(data.index[-1].to_pydatetime(), "H1")
    ref_exits, ref_entries = next(row for row in reference if row[0] == close_time)[1:]
    assert signals_equal(ref_exits, _queued_from_result(result)[0])
    assert signals_equal(ref_entries, _queued_from_result(result)[1])


def test_stateful_trailing_exit_parity_with_open_trade():
    params = {
        **default_params_for("MACrossover"),
        "trailing_stop_pct": 2.0,
        "stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
    }
    compiled = _compiled_for_strategy("MACrossover", params)
    data = _synthetic_ohlcv(80)
    strategy = build_strategy("MACrossover", params, "WIN$")
    trade = Trade(
        id="exec-dep-1",
        order_id="o1",
        symbol="WIN$",
        action=OrderAction.BUY,
        quantity=1.0,
        entry_time=data.index[10].to_pydatetime(),
        entry_price=float(data.iloc[10]["close"]),
    )
    reference = reference_queued_signals_by_close(
        strategy, data, timeframe="H1", open_trade=trade
    )

    evaluator = _evaluator_for(compiled)
    evaluator.set_open_trade(trade)
    evaluator.seed_window(data.iloc[:-1])
    result = evaluator.ingest_completed_bars(data.iloc[-1:])[0]
    close_time = bar_close_time(data.index[-1].to_pydatetime(), "H1")
    ref_exits, ref_entries = next(row for row in reference if row[0] == close_time)[1:]
    assert signals_equal(ref_exits, _queued_from_result(result)[0])
    assert signals_equal(ref_entries, _queued_from_result(result)[1])


def test_forming_bar_mutation_does_not_change_decision():
    compiled = _compiled_for_strategy("MACrossover")
    data = _synthetic_ohlcv(120)
    completed = data.iloc[:-1]
    forming = data.iloc[-1:].copy()
    now = bar_close_time(forming.index[-1].to_pydatetime(), "H1") - timedelta(minutes=1)

    trimmed = drop_forming_bar(
        pd.concat([completed, forming]),
        "H1",
        now=now,
    )
    evaluator = _evaluator_for(compiled)
    evaluator.seed_window(trimmed.iloc[:-1])
    baseline = evaluator.ingest_completed_bars(trimmed.iloc[-1:])[0]

    mutated = forming.copy()
    mutated.iloc[0, mutated.columns.get_loc("close")] = 1e9
    still_forming = drop_forming_bar(
        pd.concat([completed, mutated]),
        "H1",
        now=now,
    )
    evaluator2 = _evaluator_for(compiled, deployment_id="dep-2")
    evaluator2.seed_window(still_forming.iloc[:-1])
    after_mutation = evaluator2.ingest_completed_bars(still_forming.iloc[-1:])[0]

    assert baseline.signal_action == after_mutation.signal_action
    assert baseline.queued_entry_signals == after_mutation.queued_entry_signals
    assert baseline.queued_exit_signals == after_mutation.queued_exit_signals


def test_duplicate_bar_delivery_is_deduplicated():
    compiled = _compiled_for_strategy("MACrossover")
    data = _synthetic_ohlcv(80)
    bar = data.iloc[-1:]
    evaluator = _evaluator_for(compiled)
    evaluator.seed_window(data.iloc[:-1])
    first = evaluator.ingest_completed_bars(bar)[0]
    second = evaluator.ingest_completed_bars(bar)[0]
    assert first.emits_decision
    assert second.skipped_duplicate
    assert not second.emits_decision


def test_rolling_window_stays_bounded_over_long_stream():
    compiled = _compiled_for_strategy("MACrossover")
    evaluator = _evaluator_for(compiled)
    bound = evaluator.window_bound
    stream = _synthetic_ohlcv(800)
    for i in range(1, len(stream)):
        chunk = stream.iloc[i : i + 1]
        evaluator.ingest_completed_bars(chunk)
        assert evaluator.rolling_bar_count <= bound


def test_recovery_replay_restores_trailing_state_without_emitting_orders():
    params = {
        **default_params_for("MACrossover"),
        "trailing_stop_pct": 1.0,
        "stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
    }
    compiled = _compiled_for_strategy("MACrossover", params)
    data = _synthetic_ohlcv(60)
    trade = Trade(
        id="exec-dep-1",
        order_id="o1",
        symbol="WIN$",
        action=OrderAction.BUY,
        quantity=1.0,
        entry_time=data.index[5].to_pydatetime(),
        entry_price=float(data.iloc[5]["close"]),
    )

    replay_evaluator = _evaluator_for(compiled, deployment_id="dep-replay")
    replay_evaluator.set_open_trade(trade)
    replay_evaluator.seed_window(data.iloc[:10])
    replay_results = replay_evaluator.replay_recovery(data.iloc[10:-1])
    assert replay_results
    assert all(not r.emits_decision for r in replay_results)

    live = _evaluator_for(compiled, deployment_id="dep-live")
    live.set_open_trade(trade)
    live.seed_window(data.iloc[:10])
    live.replay_recovery(data.iloc[10:-1])
    result = live.ingest_completed_bars(data.iloc[-1:])[0]
    assert result.replay is False
    assert result.emits_decision

    strategy = build_strategy("MACrossover", params, "WIN$")
    reference = reference_queued_signals_by_close(
        strategy, data, timeframe="H1", open_trade=trade
    )
    close_time = bar_close_time(data.index[-1].to_pydatetime(), "H1")
    ref_exits, _ref_entries = next(row for row in reference if row[0] == close_time)[1:]
    assert signals_equal(ref_exits, _queued_from_result(result)[0])


def test_domain_signal_action_mapping():
    compiled = _compiled_for_strategy("MACrossover")
    data = _synthetic_ohlcv(80)
    evaluator = _evaluator_for(compiled)
    evaluator.seed_window(data)
    result = evaluator.ingest_completed_bars(data.iloc[-1:])[0]
    assert result.signal_action in DomainSignalAction
