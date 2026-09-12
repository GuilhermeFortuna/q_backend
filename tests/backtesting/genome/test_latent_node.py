"""Tests for ind.latent genome node (WO150)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from q_backend.backtesting.genome.compile import compile_genome
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.features.compute import clear_model_output_cache, compute_feature
from q_backend.features.leakage import assert_neural_oos_only
from q_backend.features.registry import (
    get_feature_spec,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.training import default_train_encoder_config, train_encoder


def _synthetic_bars(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def _classical_input_window(bars: pd.DataFrame) -> pd.DataFrame:
    columns = {}
    for name in ("rsi", "atr"):
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


def _train_production_model(
    db_session: Session,
    *,
    n_latents: int = 3,
    train_end_index: int = 80,
) -> tuple[object, pd.DataFrame]:
    bars = _synthetic_bars()
    train_start = bars["time"].iloc[0].to_pydatetime()
    train_end = bars["time"].iloc[train_end_index].to_pydatetime()

    full_window = _classical_input_window(bars)
    train_mask = (full_window.index >= train_start) & (full_window.index <= train_end)

    config = default_train_encoder_config(
        symbol="SYN",
        timeframe="H1",
        train_start=train_start,
        train_end=train_end,
        n_latents=n_latents,
        input_features=("rsi", "atr"),
        model_key="pca_latent_node_test",
    )
    version = train_encoder(
        db_session,
        config,
        feature_window=full_window.loc[train_mask].dropna(),
    )
    register_neural_model_features(version)
    return version, bars


def _latent_entry_genome(*, latent_index: int = 0) -> dict:
    return {
        "version": 1,
        "genome_id": "latent-entry",
        "nodes": [
            {
                "id": "lat",
                "kind": "ind.latent",
                "params": {"latent_index": latent_index},
                "inputs": [],
            },
            {
                "id": "entry",
                "kind": "cmp.cross_above",
                "params": {"threshold": 0.0},
                "inputs": ["lat"],
            },
            {
                "id": "exit",
                "kind": "cmp.cross_below",
                "params": {"threshold": 0.0},
                "inputs": ["lat"],
            },
        ],
        "entry_long": {"ref": "entry"},
        "entry_short": {"ref": "exit"},
        "exit_long": {"ref": "exit"},
        "exit_short": {"ref": "entry"},
    }


def test_ind_latent_node_spec_and_bounds() -> None:
    spec = NODE_SPECS["ind.latent"]
    assert spec.min_inputs == 0
    assert spec.max_inputs == 0
    assert spec.output_ports == ("out",)
    assert spec.port_types["out"] == "oscillator"
    assert spec.allowed_param_keys == frozenset({"latent_index"})
    assert "latent_index" in GENOME_PARAM_BOUNDS

    plan = compile_genome(_latent_entry_genome())
    latent_node = next(node for node in plan.sorted_nodes if node.node.kind == "ind.latent")
    assert latent_node.input_bindings == []
    assert latent_node.column_by_port["out"] == "g_lat"


@pytest.fixture
def production_model(db_session, lake_root_path):
    clear_model_output_cache()
    version, bars = _train_production_model(db_session)
    yield version, bars
    unregister_neural_model_features([f"{name}@{version.model_hash[:8]}" for name in version.latent_names])
    clear_model_output_cache()


def test_ind_latent_evaluates_pit_safe_with_production_model(production_model) -> None:
    version, bars = production_model
    ohlcv = bars.set_index("time")

    strategy = CompositeStrategy(
        genome=_latent_entry_genome(latent_index=0),
        params={},
        symbol="SYN",
        timeframe="H1",
        latent_model_hash=version.model_hash,
    )
    result = strategy.compute_indicators(ohlcv)

    latent_col = "g_lat"
    assert latent_col in result.columns
    series = result[latent_col].reset_index(drop=True)
    times = bars["time"].reset_index(drop=True)
    train_end = version.train_end

    assert_neural_oos_only(series, times, train_end)
    train_end_ts = pd.Timestamp(train_end)
    if train_end_ts.tzinfo is None:
        train_end_ts = train_end_ts.tz_localize("UTC")
    assert series.loc[times > train_end_ts].notna().any()
    assert "entry_long_signal" in result.columns


def test_ind_latent_without_production_model_is_all_nan() -> None:
    bars = _synthetic_bars(120)
    ohlcv = bars.set_index("time")

    strategy = CompositeStrategy(
        genome=_latent_entry_genome(),
        params={},
        symbol="SYN",
        timeframe="H1",
        latent_model_hash=None,
    )
    result = strategy.compute_indicators(ohlcv)

    assert result["g_lat"].isna().all()
    assert "entry_long_signal" in result.columns


def test_ind_latent_index_clamped_to_model_latent_count(production_model) -> None:
    version, bars = production_model
    ohlcv = bars.set_index("time")

    strategy = CompositeStrategy(
        genome=_latent_entry_genome(latent_index=999),
        params={},
        symbol="SYN",
        timeframe="H1",
        latent_model_hash=version.model_hash,
    )
    result = strategy.compute_indicators(ohlcv)

    last_latent_col = f"latent_{len(version.latent_names):03d}"
    expected = compute_feature(
        bars,
        get_feature_spec(f"{last_latent_col}@{version.model_hash[:8]}"),
        {},
    ).series.reset_index(drop=True)

    actual = result["g_lat"].reset_index(drop=True)
    pd.testing.assert_series_equal(actual, expected, check_names=False)


def _two_latent_genome() -> dict:
    return {
        "version": 1,
        "genome_id": "two-latent",
        "nodes": [
            {"id": "l0", "kind": "ind.latent", "params": {"latent_index": 0}, "inputs": []},
            {"id": "l1", "kind": "ind.latent", "params": {"latent_index": 1}, "inputs": []},
            {"id": "entry", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["l0"]},
            {"id": "exit", "kind": "cmp.cross_below", "params": {"threshold": 0.0}, "inputs": ["l1"]},
        ],
        "entry_long": {"ref": "entry"},
        "entry_short": {"ref": "exit"},
        "exit_long": {"ref": "exit"},
        "exit_short": {"ref": "entry"},
    }


def test_two_latent_nodes_share_cache_but_select_distinct_columns(production_model) -> None:
    # The frame/warmup caches are shared across ind.latent nodes; two nodes with
    # different latent_index must still resolve to their own (distinct) columns and
    # stay byte-identical to the canonical compute_feature path.
    version, bars = production_model
    ohlcv = bars.set_index("time")

    strategy = CompositeStrategy(
        genome=_two_latent_genome(),
        params={},
        symbol="SYN",
        timeframe="H1",
        latent_model_hash=version.model_hash,
    )
    result = strategy.compute_indicators(ohlcv)

    for node_col, latent_col in (("g_l0", "latent_001"), ("g_l1", "latent_002")):
        expected = compute_feature(
            bars,
            get_feature_spec(f"{latent_col}@{version.model_hash[:8]}"),
            {},
        ).series.reset_index(drop=True)
        actual = result[node_col].reset_index(drop=True)
        pd.testing.assert_series_equal(actual, expected, check_names=False)

    # Distinct latents must not collapse to the same series via the shared cache.
    assert not result["g_l0"].equals(result["g_l1"])
