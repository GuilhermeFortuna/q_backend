from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from q_backend.backtesting.signal_columns import (
    SIGNAL_ENTRY,
    SIGNAL_EXIT_LONG,
    SIGNAL_EXIT_SHORT,
    SIGNAL_STRENGTH,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.ml_filters.adapters import EntryClassifier
from q_backend.ml_filters.features import build_entry_features


class EntryFilteredStrategy(TradingStrategy):
    """Apply a frozen classifier after the base strategy has combined stances."""

    def __init__(
        self,
        base_strategy: TradingStrategy,
        fitted_model: EntryClassifier,
        threshold: float,
        *,
        entry_start: datetime | None = None,
        entry_end: datetime | None = None,
    ) -> None:
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be finite and in [0, 1]")
        self.base_strategy = base_strategy
        self.fitted_model = fitted_model
        self.threshold = float(threshold)
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.candidate_count = 0
        self.accepted_count = 0
        self.not_ready_count = 0
        self.scores: pd.Series | None = None
        self.symbol = getattr(base_strategy, "symbol", "")
        super().__init__(**getattr(base_strategy, "parameters", {}))
        self.parameters = getattr(base_strategy, "parameters", {})
        self.exit_strategy = base_strategy.exit_strategy

    @property
    def holding_period_bars(self) -> int | None:
        return self.base_strategy.holding_period_bars

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return self.base_strategy.get_chart_indicators()

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        frame = self.base_strategy.compute_indicators(data.copy())
        if SIGNAL_ENTRY not in frame:
            raise ValueError("Base strategy did not produce final entry signals")
        output = frame.copy()
        entry = output[SIGNAL_ENTRY].to_numpy(dtype=np.int8, copy=True)
        strength = output[SIGNAL_STRENGTH].to_numpy(dtype=np.float64, copy=True)
        candidates = np.flatnonzero(entry != 0)
        self.candidate_count = 0
        self.accepted_count = 0
        self.not_ready_count = 0
        score_values = np.full(len(frame), np.nan, dtype=np.float64)
        selected_features = tuple(self.fitted_model.feature_names)

        for position in candidates:
            timestamp = pd.Timestamp(frame.index[position])
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(ZoneInfo("America/Sao_Paulo"))
            timestamp = timestamp.tz_convert("UTC")
            start = pd.Timestamp(self.entry_start).tz_convert("UTC") if self.entry_start is not None else None
            end = pd.Timestamp(self.entry_end).tz_convert("UTC") if self.entry_end is not None else None
            if start is not None and timestamp < start:
                entry[position] = 0
                strength[position] = 0.0
                continue
            if end is not None and timestamp >= end:
                entry[position] = 0
                strength[position] = 0.0
                continue
            self.candidate_count += 1
            try:
                candidate = build_entry_features(frame.iloc[[position]], [int(entry[position])], selected_features)
                values = candidate.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
            except (KeyError, ValueError, TypeError):
                self.not_ready_count += 1
                entry[position] = 0
                strength[position] = 0.0
                continue
            if not np.isfinite(values).all():
                self.not_ready_count += 1
                entry[position] = 0
                strength[position] = 0.0
                continue
            score = float(self.fitted_model.predict_good_entry_probability(candidate)[0])
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError("Fitted model produced an invalid probability score")
            score_values[position] = score
            if score < self.threshold:
                entry[position] = 0
                strength[position] = 0.0
            else:
                self.accepted_count += 1

        output[SIGNAL_ENTRY] = entry.astype(np.int8)
        output[SIGNAL_STRENGTH] = strength.astype(np.float64)
        # Keep the existing engine contract: a rejected reversal retains its
        # original exit trigger, allowing the open position to close next open.
        self.scores = pd.Series(score_values, index=frame.index, name="ml_filter_score")
        return output
