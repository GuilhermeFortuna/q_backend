from __future__ import annotations

from collections.abc import Mapping
from typing import Any, List

import numpy as np
import pandas as pd
import q_core

from q_backend.backtesting.exit_rules.registry import (
    all_param_specs,
    enabled_rules,
    required_columns as registry_required_columns,
)
from q_backend.backtesting.models import OrderAction, Signal, SignalAction, Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec

_engine = q_core.engine


class ExitStrategy:
    def __init__(self, params: dict[str, Any] | None = None, **kwargs: Any):
        merged: dict[str, Any] = {}
        if params is not None:
            merged.update(params)
        merged.update(kwargs)
        self.params = merged
        filtered = {spec.name: merged[spec.name] for spec in all_param_specs() if spec.name in merged}
        self._rules = enabled_rules(filtered)
        self._step = _engine.DecisionStep(exit_params=filtered)
        self._last_open_ids: set[str] = set()

    @property
    def stop_loss_pct(self) -> float:
        return float(self.params.get("stop_loss_pct", 0.0))

    @property
    def take_profit_pct(self) -> float:
        return float(self.params.get("take_profit_pct", 0.0))

    @property
    def trailing_stop_pct(self) -> float:
        return float(self.params.get("trailing_stop_pct", 0.0))

    @property
    def stop_loss_atr(self) -> float:
        return float(self.params.get("stop_loss_atr", 0.0))

    @property
    def take_profit_atr(self) -> float:
        return float(self.params.get("take_profit_atr", 0.0))

    @property
    def atr_period(self) -> int:
        return int(self.params.get("atr_period", 14))

    @property
    def _state(self) -> Mapping[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for trade_id in self._last_open_ids:
            state = self._step.state(trade_id)
            if state is not None:
                result[trade_id] = state
        return result

    @property
    def _extreme_prices(self) -> dict[str, float]:
        extremes: dict[str, float] = {}
        for trade_id, rule_states in self._state.items():
            trailing_state = rule_states.get("trailing")
            if trailing_state and "extreme" in trailing_state:
                extremes[trade_id] = trailing_state["extreme"]
        return extremes

    def _sync_open_trade_ids(self, open_trades: List[Trade]) -> None:
        self._last_open_ids = {trade.id for trade in open_trades}

    def check_exits(self, open_trades: List[Trade], current_data: pd.Series) -> List[Signal]:
        self._sync_open_trade_ids(open_trades)
        if not open_trades:
            return []

        close = np.array([float(current_data.get("close", 0.0))], dtype=np.float64)
        high = (
            np.array([float(current_data.get("high", close[0]))], dtype=np.float64)
            if "high" in current_data.index
            else None
        )
        low = (
            np.array([float(current_data.get("low", close[0]))], dtype=np.float64)
            if "low" in current_data.index
            else None
        )
        columns: dict[str, np.ndarray] = {}
        for col in self.required_columns():
            if col in current_data.index:
                val = current_data.get(col)
                columns[col] = np.array([float(val) if pd.notna(val) else np.nan], dtype=np.float64)

        trades = [
            (
                trade.id,
                1 if trade.action in {SignalAction.BUY, OrderAction.BUY} else -1,
                trade.entry_price,
                0,
            )
            for trade in open_trades
        ]
        bar = 0
        exits, _entry = self._step.decide(
            bar=bar,
            trades=trades,
            close=close,
            high=high,
            low=low,
            entry=np.zeros(1, dtype=np.int8),
            exit_long=np.zeros(1, dtype=bool),
            exit_short=np.zeros(1, dtype=bool),
            strength=np.zeros(1, dtype=np.float64),
            bar_index=None,
            columns=columns,
            holding_period_bars=None,
        )
        trade_by_id = {trade.id: trade for trade in open_trades}
        return [
            Signal(symbol=trade_by_id[trade_id].symbol, action=SignalAction.CLOSE, exit_reason=reason)
            for trade_id, reason in exits
            if trade_id in trade_by_id
        ]

    def required_columns(self) -> list[str]:
        return registry_required_columns(self.params)


def get_exit_strategy_params() -> List[StrategyParamSpec]:
    return all_param_specs()
