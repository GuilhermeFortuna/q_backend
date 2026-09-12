"""Tests for PIT-safe feature computation (WO128)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.features.compute import compute_feature
from q_backend.features.registry import get_feature_spec, list_feature_specs, resolve_params


def _synthetic_bars(n: int = 260) -> pd.DataFrame:
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


def _indicator_column(node_id: str, port: str) -> str:
    if port == "out":
        return f"g_{node_id}"
    return f"g_{node_id}__{port}"


def _minimal_genome(spec_name: str, params: dict) -> dict:
    spec = get_feature_spec(spec_name)
    node_id = "ind"
    source_id = "src"
    node_spec = NODE_SPECS[spec.node_kind]
    nodes: list[dict] = []

    if node_spec.min_inputs > 0:
        nodes.append({"id": source_id, "kind": "source.close", "params": {}, "inputs": []})
        ind_inputs = [source_id]
    else:
        ind_inputs = []
        nodes.append({"id": source_id, "kind": "source.close", "params": {}, "inputs": []})

    nodes.append(
        {
            "id": node_id,
            "kind": spec.node_kind,
            "params": params,
            "inputs": ind_inputs,
        }
    )
    nodes.append(
        {
            "id": "never",
            "kind": "cmp.lt",
            "params": {},
            "inputs": [source_id, source_id],
        }
    )

    return {
        "version": 1,
        "genome_id": f"parity-{spec_name}",
        "nodes": nodes,
        "entry_long": {"ref": "never"},
        "entry_short": {"ref": "never"},
        "exit_long": {"ref": "never"},
        "exit_short": {"ref": "never"},
    }


def _output_port_for(spec_name: str) -> str:
    from q_backend.features.compute import _FEATURE_OUTPUT_PORT

    return _FEATURE_OUTPUT_PORT[spec_name]


def _genome_series(bars: pd.DataFrame, spec_name: str, params: dict) -> pd.Series:
    spec = get_feature_spec(spec_name)
    port = _output_port_for(spec_name)
    col = _indicator_column("ind", port)
    ohlcv = bars.set_index("time")
    genome = _minimal_genome(spec_name, params)
    strategy = CompositeStrategy(genome=genome, params={}, symbol="TEST")
    result = strategy.compute_indicators(ohlcv)
    return result[col].reset_index(drop=True)


@pytest.mark.parametrize(
    ("spec_name", "params"),
    [
        ("rsi", {"period": 14}),
        ("atr", {"period": 14}),
        ("realized_vol", {"window": 21}),
        ("ma", {"period": 20, "ma_type": "sma"}),
    ],
)
def test_compute_feature_shape_and_warmup(spec_name: str, params: dict) -> None:
    bars = _synthetic_bars()
    spec = get_feature_spec(spec_name)
    resolved = resolve_params(spec, params)
    result = compute_feature(bars, spec, params)

    assert len(result.series) == len(bars)
    pd.testing.assert_index_equal(result.series.index, pd.DatetimeIndex(bars["time"]))
    assert result.warmup_bars == int(resolved[spec.lookback_param])
    assert result.series.iloc[: result.warmup_bars].isna().all()
    if result.warmup_bars < len(result.series):
        assert result.series.iloc[result.warmup_bars :].notna().any()


def test_compute_feature_rejects_unsorted_bars() -> None:
    bars = _synthetic_bars(20)
    bars = bars.sort_values("time", ascending=False).reset_index(drop=True)
    spec = get_feature_spec("rsi")
    with pytest.raises(ValueError, match="time-sorted ascending"):
        compute_feature(bars, spec, {})


def test_compute_feature_rejects_missing_columns() -> None:
    bars = _synthetic_bars(20).drop(columns=["volume"])
    spec = get_feature_spec("rsi")
    with pytest.raises(ValueError, match="missing required columns"):
        compute_feature(bars, spec, {})


@pytest.mark.parametrize("spec_name", ["rsi", "atr"])
def test_bit_parity_with_compute_indicators(spec_name: str) -> None:
    bars = _synthetic_bars()
    spec = get_feature_spec(spec_name)
    params = {"period": 14}
    computed = compute_feature(bars, spec, params)
    genome_vals = _genome_series(bars, spec_name, params)

    warmup = computed.warmup_bars
    pd.testing.assert_series_equal(
        computed.series.iloc[warmup:].reset_index(drop=True),
        genome_vals.iloc[warmup:].reset_index(drop=True),
        check_names=False,
        rtol=1e-9,
        atol=1e-9,
    )


def test_catalog_has_atr_spec() -> None:
    names = {spec.name for spec in list_feature_specs()}
    assert "atr" in names
