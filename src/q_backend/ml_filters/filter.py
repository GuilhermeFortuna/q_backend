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
        self.suppressed_count = 0
        self.scores: pd.Series | None = None
        self.symbol = getattr(base_strategy, "symbol", "")
        super().__init__(**getattr(base_strategy, "parameters", {}))
        self.parameters = getattr(base_strategy, "parameters", {})
        self.exit_strategy = base_strategy.exit_strategy

    @property
    def holding_period_bars(self) -> int | None:
        return self.base_strategy.holding_period_bars

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            *self.base_strategy.get_chart_indicators(),
            ChartIndicatorSpec(key="ml_filter_score", label="ML filter score", pane="oscillator"),
        ]

    @property
    def scored_count(self) -> int:
        return self.candidate_count - self.not_ready_count

    @property
    def rejected_count(self) -> int:
        return self.scored_count - self.accepted_count

    def diagnostics_summary(self) -> dict[str, Any]:
        """Candidate-level counts from the latest full-frame ``compute_indicators``."""
        return {
            "candidate_signals": self.candidate_count,
            "scored": self.scored_count,
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "not_ready": self.not_ready_count,
            "suppressed_outside_window": self.suppressed_count,
            "threshold": self.threshold,
        }

    def _in_window(self, timestamp: pd.Timestamp) -> bool:
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize(ZoneInfo("America/Sao_Paulo"))
        timestamp = timestamp.tz_convert("UTC")
        if self.entry_start is not None and timestamp < pd.Timestamp(self.entry_start).tz_convert("UTC"):
            return False
        if self.entry_end is not None and timestamp >= pd.Timestamp(self.entry_end).tz_convert("UTC"):
            return False
        return True

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        frame = self.base_strategy.compute_indicators(data.copy())
        if SIGNAL_ENTRY not in frame:
            raise ValueError("Base strategy did not produce final entry signals")
        output = frame.copy()
        entry = output[SIGNAL_ENTRY].to_numpy(dtype=np.int8, copy=True)
        strength = output[SIGNAL_STRENGTH].to_numpy(dtype=np.float64, copy=True)
        score_values = np.full(len(frame), np.nan, dtype=np.float64)
        accepted = np.zeros(len(frame), dtype=bool)
        self.candidate_count = 0
        self.accepted_count = 0
        self.not_ready_count = 0
        self.suppressed_count = 0

        in_window: list[int] = []
        for position in np.flatnonzero(entry != 0):
            if self._in_window(pd.Timestamp(frame.index[position])):
                in_window.append(int(position))
            else:
                self.suppressed_count += 1
                entry[position] = 0
                strength[position] = 0.0
        self.candidate_count = len(in_window)

        # One batched feature build and one model call per frame (the pipeline is
        # fitted once, so scoring cost is linear in candidates, not in bars).
        ready = np.zeros(len(in_window), dtype=bool)
        candidate_features = pd.DataFrame()
        if in_window:
            sides = [int(entry[position]) for position in in_window]
            try:
                candidate_features = build_entry_features(
                    frame.iloc[in_window], sides, tuple(self.fitted_model.feature_names)
                ).apply(pd.to_numeric, errors="coerce")
                ready = np.isfinite(candidate_features.to_numpy(dtype=float)).all(axis=1)
            except (KeyError, ValueError, TypeError):
                pass
        self.not_ready_count = int((~ready).sum())
        for position in np.asarray(in_window, dtype=int)[~ready]:
            entry[position] = 0
            strength[position] = 0.0

        if ready.any():
            scores = np.asarray(
                self.fitted_model.predict_good_entry_probability(candidate_features.loc[ready]), dtype=float
            )
            if (
                scores.shape != (int(ready.sum()),)
                or not np.isfinite(scores).all()
                or ((scores < 0) | (scores > 1)).any()
            ):
                raise ValueError("Fitted model produced an invalid probability score")
            for position, score in zip(np.asarray(in_window, dtype=int)[ready], scores):
                score_values[position] = float(score)
                if score >= self.threshold:
                    accepted[position] = True
                    self.accepted_count += 1
                else:
                    entry[position] = 0
                    strength[position] = 0.0

        output[SIGNAL_ENTRY] = entry.astype(np.int8)
        output[SIGNAL_STRENGTH] = strength.astype(np.float64)
        # Additive diagnostics next to the raw indicators. Exit columns are left as the
        # base strategy wrote them so a rejected reversal still closes the open position.
        output["ml_filter_score"] = score_values
        output["ml_filter_accepted"] = accepted
        self.scores = pd.Series(score_values, index=frame.index, name="ml_filter_score")
        return output
