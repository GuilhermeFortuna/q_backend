"""Tests for WO159 B3 session / regime / HTF context features."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.node_specs import NODE_SPECS, add_node_kinds
from q_backend.backtesting.genome.operators import _mutate_add_node, build_random_genome
from q_backend.backtesting.genome.schema import Genome, GenomeNode, NodeRef
from q_backend.backtesting.genome.validate import GenomeValidationError, validate_genome
from q_backend.backtesting.session_context import prepare_evaluation_frame
from q_backend.backtesting.session_context.config import SessionContextConfig
from q_backend.backtesting.session_context.compute import (
    CTX_D1_PREV_CLOSE,
    CTX_D1_PREV_HIGH,
    compute_minutes_from_open,
    compute_session_context_bundle,
)
from q_backend.features.compute import compute_feature
from q_backend.features.registry import get_feature_spec
from q_backend.optimization.backtest_runner import DefaultBacktestRunner

CONTEXT_KINDS = tuple(
    sorted(kind for kind in NODE_SPECS if kind.startswith("feature."))
)


def _intraday_frame(
    *,
    start: datetime,
    periods: int,
    freq: str = "h",
    close_start: float = 100.0,
) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=freq)
    close = close_start + np.arange(periods, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(periods, 1000.0),
        },
        index=index,
    )


def _feature_genome(kind: str, params: dict) -> dict:
    nodes = [
        {"id": "src", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "feat", "kind": kind, "params": params, "inputs": []},
        {"id": "never", "kind": "cmp.lt", "params": {}, "inputs": ["src", "src"]},
    ]
    if NODE_SPECS[kind].min_inputs > 0:
        nodes[1]["inputs"] = ["src"]
    return {
        "version": 1,
        "genome_id": f"ctx-{kind}",
        "nodes": nodes,
        "entry_long": {"ref": "never"},
        "entry_short": {"ref": "never"},
        "exit_long": {"ref": "never"},
        "exit_short": {"ref": "never"},
    }


def _series(kind: str, params: dict, frame: pd.DataFrame) -> pd.Series:
    strategy = CompositeStrategy(
        genome=_feature_genome(kind, params),
        params={},
        symbol="TEST",
    )
    return strategy.compute_indicators(frame)["g_feat"].reset_index(drop=True)


def test_minutes_from_open_and_session_window_boundaries():
    start = datetime(2024, 1, 2, 8, 0)
    frame = _intraday_frame(start=start, periods=12, freq="h")
    minutes = _series("feature.minutes_from_open", {"session_open": "09:00"}, frame)
    assert np.isnan(minutes.iloc[0])
    assert minutes.iloc[1] == 0.0
    assert minutes.iloc[2] == 60.0

    window = _series(
        "feature.session_window",
        {"window_from": "10:00", "window_to": "11:00"},
        frame,
    )
    assert not bool(window.iloc[1])
    assert bool(window.iloc[2])


def test_session_window_validation_rejects_invalid_bounds():
    genome = Genome.model_validate(
        _feature_genome(
            "feature.session_window",
            {"window_from": "12:00", "window_to": "10:00"},
        )
    )
    with pytest.raises(GenomeValidationError, match="window_from must be before window_to"):
        validate_genome(genome)


def test_previous_session_visible_only_after_prior_session_closes():
    day1 = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=4, freq="h")
    day2 = _intraday_frame(start=datetime(2024, 1, 3, 9, 0), periods=4, freq="h", close_start=110.0)
    frame = pd.concat([day1, day2])
    prepared = prepare_evaluation_frame(frame)
    assert prepared.loc[day1.index, CTX_D1_PREV_CLOSE].isna().all()
    assert prepared.loc[day2.index[0], CTX_D1_PREV_CLOSE] == day1["close"].iloc[-1]


def test_intraday_bar_cannot_see_current_daily_high():
    day1 = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=4, freq="h", close_start=100.0)
    day2 = _intraday_frame(start=datetime(2024, 1, 3, 9, 0), periods=4, freq="h", close_start=120.0)
    frame = pd.concat([day1, day2])
    prepared = prepare_evaluation_frame(frame)
    current_day_high = float(day2["high"].max())
    mapped_prev_high = prepared.loc[day2.index, CTX_D1_PREV_HIGH]
    assert mapped_prev_high.notna().all()
    assert (mapped_prev_high != current_day_high).all()
    assert mapped_prev_high.iloc[0] == float(day1["high"].max())


def test_opening_range_unavailable_until_range_completes():
    frame = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=4, freq="30min")
    opening_high = _series(
        "feature.opening_range_high",
        {"session_open": "09:00", "range_minutes": 60},
        frame,
    )
    assert np.isnan(opening_high.iloc[0])
    assert np.isnan(opening_high.iloc[1])
    assert opening_high.notna().iloc[2:].all()


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
def test_prefix_causality(kind: str):
    params: dict = {}
    spec = NODE_SPECS[kind]
    if "session_open" in spec.allowed_param_keys:
        params["session_open"] = "09:00"
    if "session_close" in spec.allowed_param_keys:
        params["session_close"] = "18:00"
    if "window_from" in spec.allowed_param_keys:
        params["window_from"] = "10:00"
    if "window_to" in spec.allowed_param_keys:
        params["window_to"] = "12:00"
    if "window" in spec.allowed_param_keys:
        params["window"] = 5
    if "regime_lookback" in spec.allowed_param_keys:
        params["regime_lookback"] = 10
    if "ma_period" in spec.allowed_param_keys:
        params["ma_period"] = 10
    if "atr_period" in spec.allowed_param_keys:
        params["atr_period"] = 7
    if "range_minutes" in spec.allowed_param_keys:
        params["range_minutes"] = 60

    frame = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=40, freq="h")
    full = _series(kind, params, frame)
    prefix = _series(kind, params, frame.iloc[:25])
    compare = min(20, len(prefix) - 1)
    pd.testing.assert_series_equal(
        full.iloc[:compare].reset_index(drop=True),
        prefix.iloc[:compare].reset_index(drop=True),
        check_names=False,
    )


def test_context_features_in_add_node_pool():
    pool = set(add_node_kinds())
    assert set(CONTEXT_KINDS).issubset(pool)


def test_mutate_add_node_can_insert_context_features():
    rng = random.Random(159)
    base = build_random_genome(
        rng,
        genome_id="base",
        generation=0,
        max_nodes=8,
        max_depth=8,
    )
    hits: set[str] = set()
    for index in range(300):
        mutated = _mutate_add_node(
            rng,
            base,
            max_nodes=24,
            max_depth=12,
        )
        hits.update(node.kind for node in mutated.nodes if node.kind.startswith("feature."))
    assert hits
    assert hits.issubset(set(add_node_kinds()))


def test_prepare_evaluation_frame_called_once_per_runner(monkeypatch):
    calls = {"count": 0}
    original = prepare_evaluation_frame

    def counting(frame, **kwargs):
        calls["count"] += 1
        return original(frame, **kwargs)

    monkeypatch.setattr(
        "q_backend.optimization.backtest_runner.prepare_evaluation_frame",
        counting,
    )
    service = MagicMock()
    start = datetime(2024, 1, 1)
    end = datetime(2024, 1, 5)

    class _Bar:
        def __init__(self, payload: dict):
            self._payload = payload

        def model_dump(self) -> dict:
            return self._payload

    bars = []
    for hour in range(24 * 4):
        ts = start + timedelta(hours=hour)
        bars.append(
            _Bar(
                {
                    "time": ts,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000.0,
                }
            )
        )
    service.get_ohlcv.return_value = bars

    runner = DefaultBacktestRunner.from_market_data_sliced(
        service,
        symbol="TEST",
        timeframe="H1",
        start=start,
        end=end,
    )
    assert calls["count"] == 1
    assert runner._df is not None
    assert CTX_D1_PREV_CLOSE in runner._df.columns


def test_feature_store_parity_for_prev_session_close():
    day1 = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=4, freq="h")
    day2 = _intraday_frame(start=datetime(2024, 1, 3, 9, 0), periods=4, freq="h", close_start=110.0)
    frame = pd.concat([day1, day2]).reset_index().rename(columns={"index": "time"})
    spec = get_feature_spec("prev_session_close")
    computed = compute_feature(frame, spec, {})
    genome_vals = _series("feature.prev_session_close", {}, pd.concat([day1, day2]))
    pd.testing.assert_series_equal(
        computed.series.reset_index(drop=True),
        genome_vals,
        check_names=False,
    )


def test_session_gate_logic_and_end_to_end():
    frame = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=6, freq="h")
    genome = Genome(
        version=1,
        genome_id="gate",
        nodes=[
            GenomeNode(id="src", kind="source.close", params={}, inputs=[]),
            GenomeNode(
                id="win",
                kind="feature.session_window",
                params={"window_from": "10:00", "window_to": "11:00"},
                inputs=[],
            ),
            GenomeNode(
                id="entry",
                kind="cmp.gte",
                params={},
                inputs=["src", "src"],
            ),
            GenomeNode(id="gate", kind="logic.and", params={}, inputs=["entry", "win"]),
        ],
        entry_long=NodeRef(ref="gate"),
        entry_short=NodeRef(ref="gate"),
        exit_long=NodeRef(ref="gate"),
        exit_short=NodeRef(ref="gate"),
    )
    strategy = CompositeStrategy(genome=genome, params={}, symbol="TEST")
    result = strategy.compute_indicators(frame)
    assert result["entry_long_signal"].iloc[0] is np.False_
    assert bool(result["entry_long_signal"].iloc[1]) is True


def test_compute_session_context_bundle_handles_missing_day_gap():
    day1 = _intraday_frame(start=datetime(2024, 1, 2, 9, 0), periods=3, freq="h")
    day3 = _intraday_frame(start=datetime(2024, 1, 4, 9, 0), periods=3, freq="h", close_start=105.0)
    frame = pd.concat([day1, day3])
    bundle = compute_session_context_bundle(frame, SessionContextConfig.default_b3())
    mapped = bundle.columns[CTX_D1_PREV_CLOSE]
    assert mapped.loc[day3.index[0]] == day1["close"].iloc[-1]
