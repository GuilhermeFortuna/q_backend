import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.signal_columns import (
    SIGNAL_ENTRY,
    SIGNAL_EXIT_LONG,
    SIGNAL_EXIT_SHORT,
    SIGNAL_STRENGTH,
    write_signal_columns,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.ml_filters.filter import EntryFilteredStrategy


class _BaseStrategy(TradingStrategy):
    def compute_indicators(self, data):
        frame = data.copy()
        return write_signal_columns(
            frame,
            entry_long=pd.Series([False, True, False, True], index=frame.index, dtype=bool),
            entry_short=pd.Series([False, False, True, False], index=frame.index, dtype=bool),
            exit_long=pd.Series([False, False, True, False], index=frame.index, dtype=bool),
            exit_short=pd.Series([False, True, False, False], index=frame.index, dtype=bool),
            strength=pd.Series([0.0, 0.7, 0.8, 0.9], index=frame.index),
        )

    def get_chart_indicators(self):
        return [ChartIndicatorSpec(key="existing", label="Existing", pane="price")]


class _Model:
    feature_names = ("close", "side")

    def predict_good_entry_probability(self, X):
        return X["close"].to_numpy(dtype=float)


def test_entry_gate_masks_only_entries_and_preserves_exits_strength_and_indicators():
    index = pd.date_range("2026-01-01", periods=4, freq="5min", tz="UTC")
    bars = pd.DataFrame({"close": [0.0, 0.6, 0.4, 0.8]}, index=index)
    wrapper = EntryFilteredStrategy(_BaseStrategy(), _Model(), 0.5)

    result = wrapper.compute_indicators(bars)

    assert result[SIGNAL_ENTRY].tolist() == [0, 1, 0, 1]
    assert result[SIGNAL_STRENGTH].tolist() == [0.0, 0.7, 0.0, 0.9]
    assert result[SIGNAL_EXIT_LONG].tolist() == [False, False, True, False]
    assert result[SIGNAL_EXIT_SHORT].tolist() == [False, True, False, False]
    assert wrapper.candidate_count == 3
    assert wrapper.accepted_count == 2
    assert [spec.key for spec in wrapper.get_chart_indicators()] == ["existing", "ml_filter_score"]


def test_entry_gate_rejects_nonfinite_features_and_bad_model_scores_fail_closed():
    index = pd.date_range("2026-01-01", periods=4, freq="5min", tz="UTC")
    bars = pd.DataFrame({"close": [0.0, np.nan, 0.4, 0.8]}, index=index)
    wrapper = EntryFilteredStrategy(_BaseStrategy(), _Model(), 0.5)
    result = wrapper.compute_indicators(bars)
    assert result[SIGNAL_ENTRY].tolist() == [0, 0, 0, 1]
    assert wrapper.not_ready_count == 1

    class _BadModel(_Model):
        def predict_good_entry_probability(self, X):
            return np.array([np.nan])

    with pytest.raises(ValueError, match="invalid probability"):
        EntryFilteredStrategy(_BaseStrategy(), _BadModel(), 0.5).compute_indicators(bars.fillna(0.2))


def test_entry_gate_applies_inclusive_threshold_and_partition_window():
    index = pd.date_range("2026-01-01", periods=4, freq="5min", tz="UTC")
    bars = pd.DataFrame({"close": [0.0, 0.5, 0.5, 0.9]}, index=index)
    wrapper = EntryFilteredStrategy(
        _BaseStrategy(), _Model(), 0.5, entry_start=index[2].to_pydatetime(), entry_end=index[3].to_pydatetime()
    )
    result = wrapper.compute_indicators(bars)

    assert result[SIGNAL_ENTRY].tolist() == [0, 0, -1, 0]
    assert wrapper.candidate_count == 1


@pytest.mark.parametrize("threshold", [-0.01, 1.01, float("nan")])
def test_entry_gate_rejects_invalid_thresholds(threshold):
    with pytest.raises(ValueError, match="threshold"):
        EntryFilteredStrategy(_BaseStrategy(), _Model(), threshold)
