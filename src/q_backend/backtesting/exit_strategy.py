from __future__ import annotations

from typing import Any, List

import pandas as pd

from q_backend.backtesting.exit_rules.registry import (
    all_param_specs,
    enabled_rules,
    required_columns as registry_required_columns,
)
from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class ExitStrategy:
    def __init__(self, params: dict[str, Any] | None = None, **kwargs: Any):
        merged: dict[str, Any] = {}
        if params is not None:
            merged.update(params)
        merged.update(kwargs)
        self.params = merged
        self._rules = enabled_rules(self.params)
        self._state: dict[str, dict[str, dict[str, Any]]] = {}

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
    def _extreme_prices(self) -> dict[str, float]:
        extremes: dict[str, float] = {}
        for trade_id, rule_states in self._state.items():
            trailing_state = rule_states.get("trailing")
            if trailing_state and "extreme" in trailing_state:
                extremes[trade_id] = trailing_state["extreme"]
        return extremes

    def _prune_stale_state(self, open_trades: List[Trade]) -> None:
        active_ids = {trade.id for trade in open_trades}
        self._state = {
            trade_id: rule_states
            for trade_id, rule_states in self._state.items()
            if trade_id in active_ids
        }

    def _rule_state(self, trade_id: str, rule_id: str) -> dict[str, Any]:
        trade_state = self._state.setdefault(trade_id, {})
        return trade_state.setdefault(rule_id, {})

    def _columns_ready(self, rule, data: pd.Series) -> bool:
        for col in rule.required_columns(self.params):
            val = data.get(col, None)
            if val is None or pd.isna(val):
                return False
        return True

    def check_exits(self, open_trades: List[Trade], current_data: pd.Series) -> List[Signal]:
        self._prune_stale_state(open_trades)
        if not open_trades:
            return []

        signals: list[Signal] = []
        for trade in open_trades:
            for rule in self._rules:
                state = self._rule_state(trade.id, rule.id)
                rule.on_bar(trade, current_data, state, self.params)
                if not self._columns_ready(rule, current_data):
                    continue
                if rule.should_exit(trade, current_data, state, self.params):
                    signals.append(Signal(symbol=trade.symbol, action=SignalAction.CLOSE, exit_reason=rule.id))
                    break

        return signals

    def required_columns(self) -> list[str]:
        return registry_required_columns(self.params)


def get_exit_strategy_params() -> List[StrategyParamSpec]:
    return all_param_specs()
