from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.composite_entry import CompositeEntryStrategy, derive_stance
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.models import SignalAction, Trade
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.signal_managers.base import Stance
from q_backend.backtesting.signal_managers.registry import get_manager
from q_backend.backtesting.strategy_registry import merge_strategy_params


def _ma_crossover_fixture() -> pd.DataFrame:
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(10)]
    closes = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0, 4.0, 4.0]
    opens = [10.0, 10.0, 10.0, 10.0, 12.0, 15.0, 11.0, 9.0, 5.0, 4.0]
    return pd.DataFrame({"open": opens, "close": closes}, index=times)


def _trade_signatures(registry) -> list[tuple]:
    trades = registry.get_all_trades()
    return [
        (
            trade.action,
            trade.entry_time,
            trade.entry_price,
            trade.exit_time,
            trade.exit_price,
            trade.status,
        )
        for trade in sorted(trades, key=lambda item: item.entry_time)
    ]


def _long_trade(symbol: str = "BTCUSDT", entry_price: float = 100.0) -> Trade:
    return Trade(
        id="t1",
        order_id="o1",
        symbol=symbol,
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_price=entry_price,
    )


class _StubSubStrategy:
    def __init__(self, buy: list[bool], sell: list[bool]) -> None:
        self._buy = buy
        self._sell = sell

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        df["buy_signal"] = self._buy
        df["sell_signal"] = self._sell
        return df

    def get_chart_indicators(self) -> list:
        return []


def _composite_with_stub_instances(
    stub_rows: list[tuple[list[bool], list[bool]]],
    manager_kind: str,
    manager_params: dict | None = None,
) -> CompositeEntryStrategy:
    manager = get_manager(manager_kind, manager_params or {})
    comp = CompositeEntryStrategy(
        instances=[("MACrossover", {"short_period": 2, "long_period": 4, "threshold": 1.0})],
        manager=manager,
        exit_params={},
        symbol="TEST",
    )
    comp._instances = [
        (f"e{index}", "stub", _StubSubStrategy(buy, sell)) for index, (buy, sell) in enumerate(stub_rows)
    ]
    return comp


def test_single_instance_or_equivalent_to_direct_macrossover():
    params = {"short_period": 2, "long_period": 4, "threshold": 1.0}
    symbol = "BTCUSDT"
    merged = merge_strategy_params("MACrossover", params)
    data = _ma_crossover_fixture()

    direct = build_strategy("MACrossover", params, symbol)
    composite = build_composite_entry(
        [{"strategy": "MACrossover", "params": params}],
        "or",
        {},
        merged,
        symbol,
    )

    sizer = FixedQuantitySizer(quantity=1.0)
    direct_registry = BacktestEngine(direct, sizer, initial_capital=1000).run(
        data.copy(), parallel_mode=ParallelMode.SEQUENTIAL
    )
    composite_registry = BacktestEngine(composite, sizer, initial_capital=1000).run(
        data.copy(), parallel_mode=ParallelMode.SEQUENTIAL
    )

    assert _trade_signatures(direct_registry) == _trade_signatures(composite_registry)


def test_stance_derivation_forward_fills_edges():
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz=timezone.utc)
    buy = pd.Series([False, True, False, False, False], index=index)
    sell = pd.Series([False, False, False, True, False], index=index)

    stance = derive_stance(buy, sell)

    assert stance.tolist() == [0, 1, 1, -1, -1]


def test_stance_derivation_via_compute_indicators():
    comp = _composite_with_stub_instances(
        [([False, True, False, False, False], [False, False, False, True, False])],
        "or",
    )
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz=timezone.utc)
    data = pd.DataFrame({"open": [1.0] * 5, "close": [1.0] * 5}, index=index)

    result = comp.compute_indicators(data)

    assert result["e0__stance"].tolist() == [0, 1, 1, -1, -1]


def test_and_manager_entries_only_when_non_flat_stances_agree():
    comp = _composite_with_stub_instances(
        [
            ([False, True, False, False, False], [False, False, False, False, False]),
            ([False, False, False, True, False], [False, True, False, False, False]),
        ],
        "and",
    )
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz=timezone.utc)
    data = pd.DataFrame({"open": [1.0] * 5, "close": [1.0] * 5}, index=index)

    result = comp.compute_indicators(data)

    assert result["e0__stance"].tolist() == [0, 1, 1, 1, 1]
    assert result["e1__stance"].tolist() == [0, -1, -1, 1, 1]
    assert result["net_stance"].tolist() == [0, 0, 0, 1, 1]
    assert result["net_long_signal"].tolist() == [False, False, False, True, False]


def test_and_manager_disagreement_produces_no_entry():
    comp = _composite_with_stub_instances(
        [
            ([False, True, False, False], [False, False, False, False]),
            ([False, False, False, False], [False, True, False, False]),
        ],
        "and",
    )
    index = pd.date_range("2024-01-01", periods=4, freq="h", tz=timezone.utc)
    data = pd.DataFrame({"open": [1.0] * 4, "close": [1.0] * 4}, index=index)

    result = comp.compute_indicators(data)

    assert result["net_stance"].tolist() == [0, 0, 0, 0]
    assert not result["net_long_signal"].any()
    assert not result["net_short_signal"].any()


def test_majority_threshold_two_requires_two_long_votes():
    comp = _composite_with_stub_instances(
        [
            ([False, True, False, False, False], [False, False, False, False, False]),
            ([False, True, False, False, False], [False, False, False, False, False]),
            ([False, False, False, False, True], [False, False, False, False, False]),
        ],
        "majority",
        {"vote_threshold": 2},
    )
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz=timezone.utc)
    data = pd.DataFrame({"open": [1.0] * 5, "close": [1.0] * 5}, index=index)

    result = comp.compute_indicators(data)

    assert result["net_stance"].tolist() == [0, 1, 1, 1, 1]
    assert result["net_long_signal"].tolist() == [False, True, False, False, False]


def test_majority_tie_stays_flat():
    comp = _composite_with_stub_instances(
        [
            ([False, True, False], [False, False, False]),
            ([False, False, False], [False, True, False]),
        ],
        "majority",
        {"vote_threshold": 2},
    )
    index = pd.date_range("2024-01-01", periods=3, freq="h", tz=timezone.utc)
    data = pd.DataFrame({"open": [1.0] * 3, "close": [1.0] * 3}, index=index)

    result = comp.compute_indicators(data)

    assert result["net_stance"].tolist() == [0, 0, 0]
    assert not result["net_long_signal"].any()


def test_reversal_closes_long_on_opposite_stance_not_flat():
    comp = _composite_with_stub_instances(
        [([False, True, False, False], [False, False, False, True])],
        "or",
    )
    index = pd.date_range("2024-01-01", periods=4, freq="h", tz=timezone.utc)
    data = pd.DataFrame({"open": [1.0] * 4, "close": [1.0] * 4}, index=index)
    result = comp.compute_indicators(data)

    # Flat stance must not exit a long; short stance must (same bars/counts as before).
    flat_mask = result["net_stance"] == Stance.FLAT
    short_mask = result["net_stance"] == Stance.SHORT
    assert flat_mask.any()
    assert short_mask.any()
    assert not result.loc[flat_mask, "q_signal_exit_long"].any()
    assert result.loc[short_mask, "q_signal_exit_long"].all()


def test_reversal_via_engine_fills_on_next_open():
    comp = _composite_with_stub_instances(
        [([False, True, False, False, False], [False, False, False, True, False])],
        "or",
    )
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz=timezone.utc)
    data = pd.DataFrame(
        {"open": [10.0, 11.0, 12.0, 13.0, 14.0], "close": [10.0] * 5},
        index=index,
    )

    registry = BacktestEngine(comp, FixedQuantitySizer(quantity=1.0), initial_capital=1000).run(
        data, parallel_mode=ParallelMode.SEQUENTIAL
    )

    closed = registry.get_closed_trades()
    assert len(closed) == 1
    assert closed[0].entry_price == 12.0
    assert closed[0].exit_price == 14.0


def test_flat_net_stance_does_not_close_open_long_in_engine():
    comp = _composite_with_stub_instances(
        [
            ([False, True, False, False, False], [False, False, False, False, False]),
            ([False, False, False, False, False], [False, False, True, False, False]),
        ],
        "or",
    )
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz=timezone.utc)
    data = pd.DataFrame(
        {"open": [10.0, 11.0, 12.0, 13.0, 14.0], "close": [10.0] * 5},
        index=index,
    )

    registry = BacktestEngine(comp, FixedQuantitySizer(quantity=1.0), initial_capital=1000).run(
        data, parallel_mode=ParallelMode.SEQUENTIAL
    )

    open_trades = registry.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0].entry_price == 12.0


def test_explicit_exit_still_fires_independently():
    from q_backend.backtesting.exit_strategy import ExitStrategy

    comp = _composite_with_stub_instances(
        [([False, True, False, False, False, False], [False] * 6)],
        "or",
    )
    comp.exit_strategy = ExitStrategy({"stop_loss_pct": 0.05})

    index = pd.date_range("2024-01-01", periods=6, freq="h", tz=timezone.utc)
    data = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 90.0, 90.0],
            "high": [100.0, 101.0, 102.0, 103.0, 91.0, 90.0],
            "low": [100.0, 101.0, 102.0, 103.0, 89.0, 90.0],
            "close": [100.0, 101.0, 102.0, 103.0, 90.0, 90.0],
        },
        index=index,
    )

    registry = BacktestEngine(comp, FixedQuantitySizer(quantity=1.0), initial_capital=10_000).run(
        data, parallel_mode=ParallelMode.SEQUENTIAL
    )

    closed = registry.get_closed_trades()
    assert len(closed) == 1
    assert closed[0].entry_price == 102.0
    assert closed[0].exit_price == 90.0
