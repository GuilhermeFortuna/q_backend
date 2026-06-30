"""Tests for WO158 normalization transform primitives."""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.node_specs import (
    NODE_SPECS,
    NodeSpec,
    add_node_kinds,
    base_indicator_kinds,
    random_init_transform_kinds,
    resolve_node_gen_metadata,
)
from q_backend.backtesting.genome.operators import (
    ADD_NODE_KINDS,
    INDICATOR_KINDS,
    _mutate_add_node,
    build_random_genome,
)
from q_backend.backtesting.genome.schema import Genome, GenomeNode, NodeRef
from q_backend.backtesting.genome.validate import GenomeValidationError, validate_genome
from q_backend.backtesting.transforms import (
    compute_clip,
    compute_pct_change,
    compute_rolling_rank,
    compute_rolling_zscore,
)
from q_backend.features.compute import compute_feature
from q_backend.features.registry import get_feature_spec

TRANSFORM_KINDS = (
    "transform.zscore",
    "transform.rank",
    "transform.pct_change",
    "transform.clip",
)


def _series_frame(values: list[float]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(values), freq="h", tz="UTC")
    return pd.DataFrame({"close": values}, index=index)


def _single_transform_genome(kind: str, params: dict[str, object]) -> dict:
    return {
        "version": 1,
        "genome_id": f"norm-{kind}",
        "nodes": [
            {"id": "src", "kind": "source.close", "params": {}, "inputs": []},
            {"id": "tx", "kind": kind, "params": params, "inputs": ["src"]},
            {
                "id": "never",
                "kind": "cmp.lt",
                "params": {},
                "inputs": ["src", "src"],
            },
        ],
        "entry_long": {"ref": "never"},
        "entry_short": {"ref": "never"},
        "exit_long": {"ref": "never"},
        "exit_short": {"ref": "never"},
    }


def _genome_series(kind: str, params: dict[str, object], values: list[float]) -> pd.Series:
    ohlcv = _series_frame(values)
    strategy = CompositeStrategy(
        genome=_single_transform_genome(kind, params),
        params={},
        symbol="TEST",
    )
    result = strategy.compute_indicators(ohlcv)
    return result["g_tx"].reset_index(drop=True)


def test_zscore_matches_hand_computed_values_and_warmup():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    window = 3
    expected = compute_rolling_zscore(pd.Series(values), window)
    actual = _genome_series("transform.zscore", {"window": window}, values)
    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_rank_matches_hand_computed_values_and_warmup():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    window = 3
    expected = compute_rolling_rank(pd.Series(values), window)
    actual = _genome_series("transform.rank", {"window": window}, values)
    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_pct_change_matches_hand_computed_values_and_warmup():
    values = [10.0, 11.0, 13.2, 12.0]
    change_bars = 2
    expected = compute_pct_change(pd.Series(values), change_bars)
    actual = _genome_series(
        "transform.pct_change",
        {"change_bars": change_bars},
        values,
    )
    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_clip_matches_hand_computed_values():
    values = [-2.0, 0.0, 2.0]
    expected = compute_clip(pd.Series(values), -1.0, 1.0)
    actual = _genome_series(
        "transform.clip",
        {"clip_low": -1.0, "clip_high": 1.0},
        values,
    )
    pd.testing.assert_series_equal(actual, expected, check_names=False)


@pytest.mark.parametrize("kind", TRANSFORM_KINDS)
def test_prefix_causality(kind: str):
    values = np.linspace(100.0, 130.0, 40).tolist()
    full = _genome_series(kind, _default_params_for_kind(kind), values)
    prefix = _genome_series(kind, _default_params_for_kind(kind), values[:25])
    compare_len = 20
    pd.testing.assert_series_equal(
        full.iloc[:compare_len].reset_index(drop=True),
        prefix.iloc[:compare_len].reset_index(drop=True),
        check_names=False,
    )


def _default_params_for_kind(kind: str) -> dict[str, object]:
    if kind == "transform.zscore" or kind == "transform.rank":
        return {"window": 5}
    if kind == "transform.pct_change":
        return {"change_bars": 3}
    return {"clip_low": -2.0, "clip_high": 2.0}


def test_pct_change_rejects_non_positive_change_bars():
    genome = Genome.model_validate(
        _single_transform_genome("transform.pct_change", {"change_bars": 0})
    )
    with pytest.raises(GenomeValidationError, match="change_bars must be >= 1"):
        validate_genome(genome)


def test_clip_rejects_invalid_bounds():
    genome = Genome.model_validate(
        _single_transform_genome(
            "transform.clip",
            {"clip_low": 2.0, "clip_high": -1.0},
        )
    )
    with pytest.raises(GenomeValidationError, match="clip_low must be <= clip_high"):
        validate_genome(genome)


def test_positional_node_spec_defaults_preserve_indicator_metadata():
    ma_spec = NodeSpec(
        "ind.ma",
        1,
        1,
        ("price_series",),
        ("out",),
        {"out": "price_series"},
        frozenset({"period", "ma_type"}),
    )
    diff_spec = NodeSpec(
        "ind.diff",
        2,
        2,
        ("price_series", "price_series"),
        ("out",),
        {"out": "price_series"},
        frozenset(),
    )
    ma_meta = resolve_node_gen_metadata(ma_spec)
    diff_meta = resolve_node_gen_metadata(diff_spec)
    assert ma_meta.category == "indicator"
    assert ma_meta.add_node is True
    assert ma_meta.swap is True
    assert diff_meta.swap is False
    assert diff_meta.add_node is False
    assert tuple(INDICATOR_KINDS) == base_indicator_kinds()


def test_transform_kinds_are_registered_with_generation_metadata():
    for kind in TRANSFORM_KINDS:
        spec = NODE_SPECS[kind]
        meta = resolve_node_gen_metadata(spec)
        assert meta.category == "transform"
        assert meta.random_init is True
        assert meta.add_node is True
        assert meta.swap is True
        assert kind in add_node_kinds()
        assert kind in random_init_transform_kinds()


def test_all_transform_kinds_reachable_from_add_node_and_random_population():
    rng = random.Random(158)
    add_hits: set[str] = set()
    random_hits: set[str] = set()

    base = Genome.model_validate(
        {
            "version": 1,
            "genome_id": "seed",
            "nodes": [
                {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
                {
                    "id": "n2",
                    "kind": "ind.ma",
                    "params": {"period": 20},
                    "inputs": ["n1"],
                },
                {
                    "id": "n3",
                    "kind": "cmp.cross_above",
                    "params": {"threshold": 0.0},
                    "inputs": ["n2"],
                },
            ],
            "entry_long": {"ref": "n3"},
            "entry_short": {"ref": "n3"},
            "exit_long": {"ref": "n3"},
            "exit_short": {"ref": "n3"},
        }
    )

    for _ in range(400):
        mutated = _mutate_add_node(rng, base, max_nodes=24, max_depth=12)
        for node in mutated.nodes:
            if node.kind in TRANSFORM_KINDS:
                add_hits.add(node.kind)

    for index in range(400):
        genome = build_random_genome(
            rng,
            genome_id=f"g-{index}",
            generation=0,
            max_nodes=24,
            max_depth=12,
        )
        for node in genome.nodes:
            if node.kind in TRANSFORM_KINDS:
                random_hits.add(node.kind)

    assert add_hits == set(TRANSFORM_KINDS)
    assert random_hits == set(TRANSFORM_KINDS)
    assert set(TRANSFORM_KINDS).issubset(ADD_NODE_KINDS)


def test_seeded_random_population_is_deterministic_with_fixed_seed():
    def draw_population(seed: int) -> list[str]:
        rng = random.Random(seed)
        kinds: list[str] = []
        for index in range(20):
            genome = build_random_genome(
                rng,
                genome_id=f"g-{index}",
                generation=0,
                max_nodes=24,
                max_depth=12,
            )
            kinds.extend(node.kind for node in genome.nodes)
        return kinds

    assert draw_population(1580) == draw_population(1580)


@pytest.mark.parametrize(
    ("feature_name", "params"),
    [
        ("zscore", {"window": 21}),
        ("rank", {"window": 21}),
        ("pct_change", {"change_bars": 5}),
        ("clip", {"clip_low": -2.0, "clip_high": 2.0}),
    ],
)
def test_feature_store_parity(feature_name: str, params: dict[str, object]) -> None:
    bars = pd.DataFrame(
        {
            "time": pd.date_range("2023-01-01", periods=120, freq="h", tz="UTC"),
            "open": np.linspace(100, 110, 120),
            "high": np.linspace(101, 111, 120),
            "low": np.linspace(99, 109, 120),
            "close": np.linspace(100, 110, 120),
            "volume": np.full(120, 1000.0),
        }
    )
    spec = get_feature_spec(feature_name)
    computed = compute_feature(bars, spec, params)
    genome_vals = _genome_series(
        spec.node_kind,
        params,
        bars["close"].tolist(),
    )
    warmup = computed.warmup_bars
    pd.testing.assert_series_equal(
        computed.series.iloc[warmup:].reset_index(drop=True),
        genome_vals.iloc[warmup:].reset_index(drop=True),
        check_names=False,
        rtol=1e-9,
        atol=1e-9,
    )


def test_mutate_genome_can_insert_transform_via_add_node_operator():
    rng = random.Random(42)
    base = Genome(
        version=1,
        genome_id="base",
        nodes=[
            GenomeNode(id="n1", kind="source.close", params={}, inputs=[]),
            GenomeNode(
                id="n2",
                kind="ind.rsi",
                params={"period": 14},
                inputs=["n1"],
            ),
            GenomeNode(
                id="n3",
                kind="cmp.cross_above",
                params={"threshold": 30.0},
                inputs=["n2"],
            ),
        ],
        entry_long=NodeRef(ref="n3"),
        entry_short=NodeRef(ref="n3"),
        exit_long=NodeRef(ref="n3"),
        exit_short=NodeRef(ref="n3"),
    )
    hits: set[str] = set()
    for _ in range(200):
        mutated = _mutate_add_node(rng, base, max_nodes=24, max_depth=12)
        hits.update(node.kind for node in mutated.nodes if node.kind in TRANSFORM_KINDS)
    assert hits == set(TRANSFORM_KINDS)
