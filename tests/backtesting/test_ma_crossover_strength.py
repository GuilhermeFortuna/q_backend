import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.backtesting.technical_indicators import compute_atr

NAME = "MACrossoverStrengthFilter"


def bars():
    rng = np.random.default_rng(42)
    close = 100 + rng.normal(0, 0.5, 400).cumsum()
    return pd.DataFrame(
        {"open": close, "high": close + 0.4, "low": close - 0.4, "close": close},
        index=pd.date_range("2025-01-01", periods=len(close), freq="h", tz="UTC"),
    )


def original():
    return build_strategy(
        "MACrossover", {"short_period": 9, "long_period": 20, "short_ma_type": "ema", "long_ma_type": "wma"}, "TEST"
    )


def test_defaults_and_causal_strength():
    defaults = default_params_for(NAME)
    assert {
        key: defaults[key]
        for key in ["short_period", "long_period", "short_ma_type", "long_ma_type", "atr_period", "min_cross_strength"]
    } == dict(
        short_period=9,
        long_period=20,
        short_ma_type="ema",
        long_ma_type="wma",
        atr_period=14,
        min_cross_strength=0.1616,
    )
    strategy = build_strategy(NAME, {}, "TEST")
    data = bars()
    raw = original().compute_indicators(data)
    actual = strategy.compute_indicators(data)
    score = raw.q_signal_entry * (raw.delta - raw.prev_delta) / compute_atr(data.high, data.low, data.close, 14)
    accepted = raw.q_signal_entry.ne(0) & score.gt(0.1616) & np.isfinite(score)
    assert actual.q_signal_entry.ne(0).equals(accepted)
    assert accepted.any() and (raw.q_signal_entry.ne(0) & ~accepted).any()
    for column in ["q_signal_exit_long", "q_signal_exit_short", "buy_signal", "sell_signal"]:
        pd.testing.assert_series_equal(actual[column], raw[column])
    for length in [60, 200, 399]:
        pd.testing.assert_frame_equal(strategy.compute_indicators(data.iloc[:length]), actual.iloc[:length])


def test_research_composite_preserves_gate_and_exits():
    data = bars()
    composite = build_composite_entry([{"strategy": NAME}], "or", {}, {}, "TEST")
    actual = composite.compute_indicators(data)
    direct = build_strategy(NAME, {}, "TEST").compute_indicators(data)
    pd.testing.assert_series_equal(actual.q_signal_entry, direct.q_signal_entry)
    raw = build_composite_entry(
        [
            {
                "strategy": "MACrossover",
                "params": {"short_period": 9, "long_period": 20, "short_ma_type": "ema", "long_ma_type": "wma"},
            }
        ],
        "or",
        {},
        {},
        "TEST",
    ).compute_indicators(data)
    for column in ["net_stance", "q_signal_exit_long", "q_signal_exit_short"]:
        pd.testing.assert_series_equal(actual[column], raw[column])


def test_engine_closes_rejected_reversal_without_reentering():
    data = bars()

    def run(strategy):
        return (
            BacktestEngine(
                strategy,
                FixedQuantitySizer(1),
                point_values={"TEST": 450},
                costs=TransactionCostConfig(cost_per_contract=2.5),
            )
            .run(data)
            .get_closed_trades()
        )

    source = run(original())
    filtered = run(build_composite_entry([{"strategy": NAME}], "or", {}, {}, "TEST"))
    frame = build_strategy(NAME, {}, "TEST").compute_indicators(data)
    accepted_times = set(data.index[1:][frame.q_signal_entry.iloc[:-1].ne(0)])
    expected = [trade for trade in source if trade.entry_time in accepted_times]
    assert 0 < len(filtered) < len(source)
    assert [(t.entry_time, t.exit_time, t.pnl) for t in filtered] == [
        (t.entry_time, t.exit_time, t.pnl) for t in expected
    ]
    # At least one accepted position exits on a crossover whose entry is rejected.
    assert any(frame.q_signal_entry.loc[data.index[data.index.get_loc(t.exit_time) - 1]] == 0 for t in filtered)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_cutoffs(value):
    with pytest.raises(ValueError, match="strength"):
        build_strategy(NAME, {"min_cross_strength": value}, "TEST")


def test_unavailable_or_zero_atr_rejects_entries_but_preserves_exits():
    strategy = build_strategy(NAME, {}, "TEST")
    frame = pd.DataFrame(
        {
            "q_signal_entry": np.array([1, -1], dtype=np.int8),
            "q_signal_strength": [1.0, 1.0],
            "q_signal_exit_long": [False, True],
            "q_signal_exit_short": [True, False],
            "delta": [1.0, -1.0],
            "prev_delta": [-1.0, 1.0],
            "atr": [np.nan, 0.0],
        }
    )
    output = strategy.filter_entry_signals(frame)
    assert output.q_signal_entry.eq(0).all()
    assert output.q_signal_strength.eq(0).all()
    assert output.q_signal_exit_long.tolist() == [False, True]
    assert output.q_signal_exit_short.tolist() == [True, False]


def test_strict_cutoff_equality_is_rejected():
    strategy = build_strategy(NAME, {"min_cross_strength": 0.2}, "TEST")
    frame = pd.DataFrame(
        {
            "q_signal_entry": np.array([1, -1], dtype=np.int8),
            "q_signal_strength": [1.0, 1.0],
            "delta": [0.2, -0.21],
            "prev_delta": [0.0, 0.0],
            "atr": [1.0, 1.0],
        }
    )
    output = strategy.filter_entry_signals(frame)
    assert output.q_signal_entry.tolist() == [0, -1]


def test_filtered_strategy_rejects_multiple_entry_instances():
    with pytest.raises(ValueError, match="single entry"):
        build_composite_entry([{"strategy": NAME}, {"strategy": "MACrossover"}], "or", {}, {}, "TEST")


@pytest.mark.parametrize("value", [0, -1])
def test_invalid_atr_period(value):
    with pytest.raises(ValueError, match="atr_period"):
        build_strategy(NAME, {"atr_period": value}, "TEST")
