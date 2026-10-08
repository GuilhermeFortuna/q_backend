"""Private adapter bridging ResearchStrategy to candle TradingStrategy."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.signal_columns import (
    BAR_INDEX,
    SIGNAL_COLUMNS,
    write_signal_columns,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.research.charting import ChartIndicator
from q_backend.research.orders import TradeOrder
from q_backend.research.frame import FRAME_COLUMNS
from q_backend.research.strategy import ResearchStrategy

_RESERVED_COLUMNS = frozenset((*SIGNAL_COLUMNS, BAR_INDEX))
_MARKET_COLUMNS = ("open", "high", "low", "close")


class ResearchStrategyAdapter(TradingStrategy):
    """Private adapter compiling ResearchStrategy hooks into candle engine signals."""

    def __init__(
        self,
        research_strategy: ResearchStrategy,
        symbol: str,
        exit_params: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(research_strategy, ResearchStrategy):
            raise TypeError(f"Expected ResearchStrategy instance, got {type(research_strategy).__name__}")

        self.research_strategy = research_strategy
        self.symbol = symbol
        self.parameters = dict(exit_params or {})

        # Retain engine exit strategy on the adapter so it does not shadow user's exit_strategy method
        self.exit_strategy = ExitStrategy(self.parameters)
        self._chart_indicators: tuple[ChartIndicator, ...] = ()

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """Call compute_indicators once on an owned copy and compile prefix decisions."""
        if not isinstance(data, pd.DataFrame):
            raise TypeError(f"Expected DataFrame, got {type(data).__name__}")

        if data.columns.has_duplicates:
            raise ValueError("Input DataFrame contains duplicate column names")

        # Check reserved columns on input frame
        for col in _RESERVED_COLUMNS:
            if col in data.columns:
                raise ValueError(f"Input DataFrame contains reserved column {col!r}")

        orig_index = data.index
        orig_len = len(data)

        if orig_len == 0:
            empty = data.copy()
            empty.attrs = {}
            return write_signal_columns(
                empty,
                entry_long=pd.Series(dtype=bool, index=empty.index),
                entry_short=pd.Series(dtype=bool, index=empty.index),
                exit_long=pd.Series(dtype=bool, index=empty.index),
                exit_short=pd.Series(dtype=bool, index=empty.index),
                strategy_name=type(self.research_strategy).__name__,
            )

        # 1. Call compute_indicators once on an owned copy
        data_copy = data.copy()
        if hasattr(data, "attrs"):
            data_copy.attrs = {}  # Strip caller attrs

        try:
            augmented = self.research_strategy.compute_indicators(data_copy)
        except Exception as exc:
            raise RuntimeError(f"Error in {type(self.research_strategy).__name__}.compute_indicators: {exc}") from exc

        if not isinstance(augmented, pd.DataFrame):
            raise TypeError(
                f"{type(self.research_strategy).__name__}.compute_indicators must return a DataFrame, "
                f"got {type(augmented).__name__}"
            )

        if len(augmented) != orig_len or not augmented.index.equals(orig_index):
            raise ValueError(f"{type(self.research_strategy).__name__}.compute_indicators modified row count or index")

        # Disallow duplicate column names
        if augmented.columns.has_duplicates:
            raise ValueError("compute_indicators returned DataFrame with duplicate column names")

        # Disallow reserved columns in augmented frame
        for col in _RESERVED_COLUMNS:
            if col in augmented.columns:
                raise ValueError(
                    f"{type(self.research_strategy).__name__}.compute_indicators returned reserved column {col!r}"
                )

        # Check market columns were not modified/mutated
        for col in (*FRAME_COLUMNS, "volume"):
            if col in data.columns:
                if col not in augmented.columns:
                    raise ValueError(f"Missing original market column {col!r} after compute_indicators")
                if not data[col].equals(augmented[col]):
                    raise ValueError(f"Market column {col!r} was modified in compute_indicators")

        self._chart_indicators = self._declare_chart_indicators(augmented)

        # 2. Iterate bar by bar: evaluate exit_strategy then entry_strategy on owned prefix
        entry_long = np.zeros(orig_len, dtype=bool)
        entry_short = np.zeros(orig_len, dtype=bool)
        exit_long = np.zeros(orig_len, dtype=bool)
        exit_short = np.zeros(orig_len, dtype=bool)

        for i in range(orig_len):
            timestamp = augmented.index[i]

            # 2a. exit_strategy on an isolated owned copy of prefix
            prefix_exit = augmented.iloc[: i + 1].copy()
            prefix_exit.attrs = {}

            try:
                exit_decision = self.research_strategy.exit_strategy(prefix_exit)
            except Exception as exc:
                raise RuntimeError(
                    f"Error in {type(self.research_strategy).__name__}.exit_strategy at bar {timestamp}: {exc}"
                ) from exc

            if exit_decision is not None:
                if not isinstance(exit_decision, TradeOrder):
                    raise TypeError(
                        f"{type(self.research_strategy).__name__}.exit_strategy at bar {timestamp} "
                        f"returned {type(exit_decision).__name__}, expected TradeOrder or None"
                    )
                if exit_decision.action != "close":
                    raise ValueError(
                        f"{type(self.research_strategy).__name__}.exit_strategy at bar {timestamp} "
                        f"returned illegal action {exit_decision.action!r}; only 'close' is permitted"
                    )
                exit_long[i] = True
                exit_short[i] = True

            # 2b. entry_strategy on an isolated owned copy of prefix
            prefix_entry = augmented.iloc[: i + 1].copy()
            prefix_entry.attrs = {}

            try:
                entry_decision = self.research_strategy.entry_strategy(prefix_entry)
            except Exception as exc:
                raise RuntimeError(
                    f"Error in {type(self.research_strategy).__name__}.entry_strategy at bar {timestamp}: {exc}"
                ) from exc

            if entry_decision is not None:
                if not isinstance(entry_decision, TradeOrder):
                    raise TypeError(
                        f"{type(self.research_strategy).__name__}.entry_strategy at bar {timestamp} "
                        f"returned {type(entry_decision).__name__}, expected TradeOrder or None"
                    )
                if entry_decision.action == "buy":
                    entry_long[i] = True
                elif entry_decision.action == "sell":
                    entry_short[i] = True
                else:
                    raise ValueError(
                        f"{type(self.research_strategy).__name__}.entry_strategy at bar {timestamp} "
                        f"returned illegal action {entry_decision.action!r}; only 'buy' or 'sell' is permitted"
                    )

        # Write signal columns onto augmented frame
        result_df = write_signal_columns(
            augmented,
            entry_long=pd.Series(entry_long, index=augmented.index, dtype=bool),
            entry_short=pd.Series(entry_short, index=augmented.index, dtype=bool),
            exit_long=pd.Series(exit_long, index=augmented.index, dtype=bool),
            exit_short=pd.Series(exit_short, index=augmented.index, dtype=bool),
            strategy_name=type(self.research_strategy).__name__,
        )
        return result_df

    def _declare_chart_indicators(self, augmented: pd.DataFrame) -> tuple[ChartIndicator, ...]:
        name = type(self.research_strategy).__name__
        try:
            declared = list(self.research_strategy.chart_indicators())
        except Exception as exc:
            raise RuntimeError(f"Error in {name}.chart_indicators: {exc}") from exc

        seen: set[str] = set()
        for entry in declared:
            if not isinstance(entry, ChartIndicator):
                raise ValueError(f"{name}.chart_indicators returned {entry!r}, expected ChartIndicator")
            column = entry.column
            if column in seen:
                raise ValueError(f"{name}.chart_indicators declares column {column!r} more than once")
            seen.add(column)
            if column in _MARKET_COLUMNS:
                raise ValueError(f"{name}.chart_indicators declares market column {column!r}")
            if column not in augmented.columns:
                raise ValueError(f"{name}.chart_indicators declares column {column!r} missing from compute_indicators")
            if not is_numeric_dtype(augmented[column]):
                raise ValueError(f"{name}.chart_indicators declares non-numeric column {column!r}")
        return tuple(declared)

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(key=indicator.column, label=indicator.label, pane=indicator.pane, color=indicator.color)
            for indicator in self._chart_indicators
        ]
