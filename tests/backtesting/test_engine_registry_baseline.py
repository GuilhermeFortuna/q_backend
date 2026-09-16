"""Pre-Q-028 registry baselines, including the orders goldens do not capture.

These files are intentionally generated once from the Python candle loop.  They
must never be regenerated after the engine switches to q_core.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest

from backtesting.test_goldens import (
    BASE_MA_PARAMS,
    CANDLE_CASES,
    INITIAL_CAPITAL,
    POINT_VALUE,
    SYMBOL,
    _canonical_json,
    synthetic_ohlcv,
)
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.registry import TradeRegistry

REGISTRY_GOLDENS_DIR = Path(__file__).parent / "goldens" / "registry"


def _trade_payload(trade: Any, registry: TradeRegistry) -> dict[str, Any]:
    order = registry.orders[trade.order_id]
    return {
        "symbol": trade.symbol,
        "action": trade.action.value,
        "quantity": trade.quantity,
        "entry_time": trade.entry_time,
        "entry_time_type": type(trade.entry_time).__name__,
        "entry_price": trade.entry_price,
        "exit_time": trade.exit_time,
        "exit_time_type": type(trade.exit_time).__name__ if trade.exit_time is not None else None,
        "exit_price": trade.exit_price,
        "status": trade.status.value,
        "pnl": trade.pnl,
        "commission": trade.commission,
        "point_value": trade.point_value,
        "exit_reason": trade.exit_reason,
        "order": {
            "symbol": order.symbol,
            "action": order.action.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
        },
    }


def _payload(name: str, registry: TradeRegistry) -> dict[str, Any]:
    return {
        "case": name,
        "trade_count": len(registry.trades),
        "order_count": len(registry.orders),
        "trades": [_trade_payload(trade, registry) for trade in registry.get_all_trades()],
    }


def _run_case(name: str) -> dict[str, Any]:
    if name in CANDLE_CASES:
        case = CANDLE_CASES[name]
        engine = BacktestEngine(
            case.make_strategy(),
            case.make_sizer(),
            initial_capital=INITIAL_CAPITAL,
            point_values={SYMBOL: POINT_VALUE},
            day_trade=case.day_trade,
            day_trade_start_time=case.day_trade_start_time,
            day_trade_end_time=case.day_trade_end_time,
            day_trade_close_time=case.day_trade_close_time,
        )
        registry = engine.run(case.data(), parallel_mode=case.parallel_mode)
    elif name == "ma_crossover_trade_start":
        data = synthetic_ohlcv()
        engine = BacktestEngine(build_strategy("MACrossover", BASE_MA_PARAMS, SYMBOL), FixedQuantitySizer())
        registry = engine.run(data, trade_start=data.index[120])
    elif name == "ma_crossover_costs_day_trade":
        data = synthetic_ohlcv(freq="15min")
        engine = BacktestEngine(
            build_strategy("MACrossover", BASE_MA_PARAMS, SYMBOL),
            FixedQuantitySizer(),
            point_values={SYMBOL: 0.2},
            day_trade=True,
            day_trade_start_time="00:00",
            day_trade_end_time="23:00",
            day_trade_close_time="23:30",
            costs=TransactionCostConfig(cost_per_contract=1.5, cost_bps=3),
        )
        registry = engine.run(data, parallel_mode=ParallelMode.DAY_TRADE)
    else:  # pragma: no cover - parametrization is the only caller
        raise AssertionError(name)
    return _payload(name, registry)


REGISTRY_CASES = [*CANDLE_CASES, "ma_crossover_trade_start", "ma_crossover_costs_day_trade"]


def _path(name: str) -> Path:
    return REGISTRY_GOLDENS_DIR / f"{name}.json"


@pytest.mark.parametrize("name", REGISTRY_CASES)
def test_registry_baseline(name: str, regen_goldens: bool) -> None:
    actual = _canonical_json(_run_case(name))
    path = _path(name)
    if regen_goldens:
        REGISTRY_GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    assert path.exists(), f"Missing registry baseline {path}; run with --regen-goldens before the Q-028 swap."
    expected = path.read_text(encoding="utf-8")
    assert expected == actual, "\n".join(
        difflib.unified_diff(
            expected.splitlines(), actual.splitlines(), fromfile=str(path), tofile="current", lineterm=""
        )
    )


def test_registry_baseline_tamper_fails() -> None:
    payload = _run_case("ma_crossover_baseline")
    assert payload["trades"]
    payload["trades"][0]["order"]["quantity"] += 1
    assert _canonical_json(payload) != _path("ma_crossover_baseline").read_text(encoding="utf-8")
