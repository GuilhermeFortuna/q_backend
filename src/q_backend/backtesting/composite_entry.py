from __future__ import annotations

from typing import Any, List

import numpy as np
import pandas as pd

from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.signal_columns import SIGNAL_ENTRY, write_signal_columns
from q_backend.backtesting.signal_managers.base import SignalManager, Stance
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.backtesting.strategy_registry import (
    get_registered_strategy,
    merge_strategy_params,
)


def derive_stance(buy: pd.Series, sell: pd.Series) -> pd.Series:
    raw = np.where(buy, Stance.LONG, np.where(sell, Stance.SHORT, np.nan))
    return pd.Series(raw, index=buy.index).ffill().fillna(Stance.FLAT).astype(int)


class CompositeEntryStrategy(TradingStrategy):
    def __init__(
        self,
        instances: list[tuple[str, dict[str, Any]]],
        manager: SignalManager,
        exit_params: dict[str, Any] | None = None,
        symbol: str = "BTCUSDT",
        **kwargs: Any,
    ) -> None:
        self.symbol = symbol
        self.manager = manager
        merged_exit = dict(exit_params or {})

        self._instances: list[tuple[str, str, TradingStrategy]] = []
        for index, (name, params) in enumerate(instances):
            slot_id = f"e{index}"
            merged = merge_strategy_params(name, params)
            sub = get_registered_strategy(name).build(merged, symbol)
            self._instances.append((slot_id, name, sub))

        super().__init__(symbol=symbol, **merged_exit, **kwargs)
        self.exit_strategy = ExitStrategy(merged_exit)
        self.parameters = merged_exit

    def _instance_edge_columns(
        self, sub: TradingStrategy, data: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
        sub_df = sub.compute_indicators(data.copy())
        if "buy_signal" in sub_df.columns and "sell_signal" in sub_df.columns:
            buy = sub_df["buy_signal"].fillna(False).astype(bool)
            sell = sub_df["sell_signal"].fillna(False).astype(bool)
            return sub_df, buy, sell

        entry = sub_df[SIGNAL_ENTRY]
        buy = entry == 1
        sell = entry == -1
        return sub_df, buy, sell

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        stance_columns: list[str] = []

        for slot_id, _name, sub in self._instances:
            sub_df, buy, sell = self._instance_edge_columns(sub, data)
            df[f"{slot_id}__stance"] = derive_stance(buy, sell)

            for spec in sub.get_chart_indicators():
                if spec.key in sub_df.columns:
                    df[f"{slot_id}__{spec.key}"] = sub_df[spec.key]

            stance_columns.append(f"{slot_id}__stance")

        net = np.zeros(len(df), dtype=int)
        if stance_columns:
            stance_matrix = df[stance_columns].to_numpy()
            for row_index in range(len(df)):
                row_stances = [Stance(int(value)) for value in stance_matrix[row_index]]
                net[row_index] = int(self.manager.combine(row_stances))

        net_series = pd.Series(net, index=df.index, dtype=int)
        prev_net = net_series.shift(1).fillna(Stance.FLAT).astype(int)

        df["net_stance"] = net_series
        df["net_long_signal"] = (net_series == Stance.LONG) & (prev_net != Stance.LONG)
        df["net_short_signal"] = (net_series == Stance.SHORT) & (prev_net != Stance.SHORT)
        df["buy_signal"] = df["net_long_signal"]
        df["sell_signal"] = df["net_short_signal"]
        return write_signal_columns(
            df,
            entry_long=df["net_long_signal"],
            entry_short=df["net_short_signal"],
            exit_long=(net_series == Stance.SHORT).astype(bool),
            exit_short=(net_series == Stance.LONG).astype(bool),
            strategy_name=type(self).__name__,
        )

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        specs: List[ChartIndicatorSpec] = []
        for slot_id, _name, sub in self._instances:
            for spec in sub.get_chart_indicators():
                specs.append(
                    ChartIndicatorSpec(
                        key=f"{slot_id}__{spec.key}",
                        label=f"{slot_id} · {spec.label}",
                        pane=spec.pane,
                        color=spec.color,
                    )
                )
        return specs
