"""Engine-level behavior of the MA Crossover · ML Filter research variant (Q-087)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_composite_entry, wrap_with_ml_filter
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.signal_columns import SIGNAL_ENTRY, SIGNAL_EXIT_LONG, SIGNAL_EXIT_SHORT, SIGNAL_STRENGTH
from q_backend.backtesting.strategy_registry import get_registered_strategy, list_registered_strategies

SYMBOL = "WIN$"
MA_PARAMS = {"short_period": 2, "long_period": 4}
# Alternating swings produce several long/short crossovers with distinct closes.
CLOSES = [
    100.0, 99.0, 98.0, 97.0, 96.0, 97.5, 99.5, 101.5, 103.5, 105.5, 104.0, 102.0, 100.0, 98.0, 96.0,
    94.0, 95.5, 97.5, 99.5, 101.5, 103.5, 102.0, 100.0, 98.0, 96.0, 94.0, 92.0,
]  # fmt: skip


class _ScoreByClose:
    """Fake fitted model: probability is looked up from the signal bar close."""

    feature_names = ("close", "side")

    def __init__(self, scores: dict[float, float], default: float = 1.0) -> None:
        self.scores = scores
        self.default = default
        self.batches: list[int] = []

    def predict_good_entry_probability(self, X):
        self.batches.append(len(X))
        return np.array([self.scores.get(round(float(value), 4), self.default) for value in X["close"]])


def _bars() -> pd.DataFrame:
    index = pd.date_range("2024-01-02 09:00", periods=len(CLOSES), freq="5min")
    close = np.array(CLOSES)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 0.5,
            "low": np.minimum(open_, close) - 0.5,
            "close": close,
            "tick_volume": 100,
        },
        index=index,
    )


def _composite(exit_params: dict | None = None):
    return build_composite_entry(
        [{"strategy": "MACrossover", "params": MA_PARAMS}], "or", {}, exit_params or {}, SYMBOL
    )


def _filtered(model: _ScoreByClose, threshold: float = 0.5, exit_params: dict | None = None, **window):
    return wrap_with_ml_filter(_composite(exit_params), model, threshold, **window)


def _run(strategy, bars: pd.DataFrame):
    engine = BacktestEngine(
        strategy,
        FixedQuantitySizer(quantity=1.0),
        initial_capital=100000.0,
        point_values={SYMBOL: 1.0},
    )
    return engine.run(bars, parallel_mode=ParallelMode.SEQUENTIAL).get_closed_trades()


def _candidates(bars: pd.DataFrame) -> list[tuple[int, int]]:
    frame = _composite().compute_indicators(bars.copy())
    entry = frame[SIGNAL_ENTRY].to_numpy()
    return [(int(pos), int(entry[pos])) for pos in np.flatnonzero(entry != 0)]


def test_variant_is_registered_with_original_parameters_and_capabilities():
    original = get_registered_strategy("MACrossover").info
    variant = get_registered_strategy("MACrossoverMLFilter").info

    assert variant.label == "MA Crossover · ML Filter"
    assert variant.engine == "candle"
    assert [spec.model_dump() for spec in variant.params] == [spec.model_dump() for spec in original.params]
    assert {"ml_entry_filter", "research_only"} <= set(variant.capabilities)
    assert "MACrossover" in {info.name for info in list_registered_strategies()}
    assert not original.capabilities


def test_raw_crossover_columns_are_identical_before_gating():
    bars = _bars()
    plain = _composite().compute_indicators(bars.copy())
    gated = _filtered(_ScoreByClose({}, default=0.0)).compute_indicators(bars.copy())

    shared = [column for column in plain.columns if column not in {SIGNAL_ENTRY, SIGNAL_STRENGTH}]
    pd.testing.assert_frame_equal(plain[shared], gated[shared])
    assert (gated[SIGNAL_ENTRY] == 0).all()


def test_accepting_every_candidate_reproduces_the_original_trades():
    bars = _bars()
    plain = _run(_composite(), bars)
    gated = _run(_filtered(_ScoreByClose({})), bars)

    assert len(_candidates(bars)) == 4 and len(plain) == 3
    assert [(t.action, t.entry_time, t.entry_price, t.exit_time, t.exit_price) for t in gated] == [
        (t.action, t.entry_time, t.entry_price, t.exit_time, t.exit_price) for t in plain
    ]


@pytest.mark.parametrize("rejected_side", [1, -1])
def test_rejected_reversal_closes_at_next_open_and_stays_flat(rejected_side):
    bars = _bars()
    candidates = _candidates(bars)
    # Reject a reversal candidate (not the first, so a position is open) of the chosen side.
    position, _side = next((p, s) for p, s in candidates[1:] if s == rejected_side)
    model = _ScoreByClose({round(float(bars["close"].iloc[position]), 4): 0.1})
    plain = _run(_composite(), bars)
    gated = _run(_filtered(model), bars)

    reversal_time = bars.index[position + 1]
    closing = [t for t in gated if t.exit_time == reversal_time]
    assert len(closing) == 1
    assert closing[0].exit_price == bars["open"].iloc[position + 1]
    assert all(t.entry_time != reversal_time for t in gated)
    # The rejected entry removes exactly that trade; every other entry/exit is unchanged.
    assert len(gated) == len(plain) - 1
    assert [t.entry_time for t in gated if t.entry_time < reversal_time] == [
        t.entry_time for t in plain if t.entry_time < reversal_time
    ]


def test_high_score_without_crossover_never_enters_and_scoring_is_batched():
    bars = _bars()
    model = _ScoreByClose({}, default=0.99)
    strategy = _filtered(model)
    frame = strategy.compute_indicators(bars.copy())

    candidate_positions = {position for position, _side in _candidates(bars)}
    assert set(np.flatnonzero(frame[SIGNAL_ENTRY].to_numpy() != 0)) == candidate_positions
    assert model.batches == [len(candidate_positions)]
    assert strategy.candidate_count == strategy.accepted_count == len(candidate_positions)


def test_exits_fire_regardless_of_score_and_accepted_entries_keep_original_strength():
    bars = _bars()
    plain_frame = _composite({"stop_loss_pct": 0.01}).compute_indicators(bars.copy())
    rejecting = _filtered(_ScoreByClose({}, default=0.0), exit_params={"stop_loss_pct": 0.01})
    gated_frame = rejecting.compute_indicators(bars.copy())

    assert gated_frame[SIGNAL_EXIT_LONG].tolist() == plain_frame[SIGNAL_EXIT_LONG].tolist()
    assert gated_frame[SIGNAL_EXIT_SHORT].tolist() == plain_frame[SIGNAL_EXIT_SHORT].tolist()
    assert rejecting.exit_strategy.stop_loss_pct == pytest.approx(0.01)
    assert _run(rejecting, bars) == []

    accepting = _filtered(_ScoreByClose({}, default=0.77), exit_params={"stop_loss_pct": 0.01})
    accepted_frame = accepting.compute_indicators(bars.copy())
    accepted = accepted_frame[SIGNAL_ENTRY] != 0
    assert accepted_frame.loc[accepted, SIGNAL_STRENGTH].tolist() == plain_frame.loc[accepted, SIGNAL_STRENGTH].tolist()
    assert [t.exit_reason for t in _run(accepting, bars)] == [
        t.exit_reason for t in _run(_composite({"stop_loss_pct": 0.01}), bars)
    ]


def test_chart_preparation_and_engine_use_the_same_gated_decisions():
    bars = _bars()
    candidates = _candidates(bars)
    rejected_close = round(float(bars["close"].iloc[candidates[1][0]]), 4)
    strategy = _filtered(_ScoreByClose({rejected_close: 0.0}))

    chart = augment_indicator_frame(strategy, bars)
    trades = _run(strategy, bars)

    entry_positions = np.flatnonzero(chart[SIGNAL_ENTRY].to_numpy() != 0)
    expected_entries = [bars.index[p + 1] for p in entry_positions]
    # The final accepted entry is still open at the end of the data, so only closed trades are listed.
    assert [t.entry_time for t in trades] == expected_entries[: len(trades)]
    assert len(expected_entries) == len(trades) + 1
    assert "ml_filter_score" in chart and "ml_filter_accepted" in chart
    assert chart["ml_filter_score"].notna().sum() == len(candidates)
    assert int(chart["ml_filter_accepted"].sum()) == len(candidates) - 1


def test_entries_before_train_end_are_suppressed_but_not_scored():
    bars = _bars()
    candidates = _candidates(bars)
    cutoff = bars.index[candidates[2][0]].tz_localize("America/Sao_Paulo").tz_convert("UTC")
    model = _ScoreByClose({})
    strategy = _filtered(model, entry_start=cutoff.to_pydatetime())
    frame = strategy.compute_indicators(bars.copy())

    assert np.flatnonzero(frame[SIGNAL_ENTRY].to_numpy() != 0).tolist() == [p for p, _ in candidates[2:]]
    assert model.batches == [len(candidates) - 2]
