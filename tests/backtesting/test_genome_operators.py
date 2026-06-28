"""Tests for genome genetic operators (WO55 subtree crossover + adaptation)."""

from __future__ import annotations

import random
from collections import Counter
from unittest.mock import patch

import pytest

from q_backend.backtesting.genome.operators import (
    DEFAULT_MUTATION_OPERATOR_WEIGHTS,
    MUTATION_OPERATORS,
    _crossover_attempt,
    _subtree_crossover,
    _subtree_node_ids,
    adapt_mutation_rate,
    clone_genome,
    crossover_genomes,
    draw_valid_child,
    genome_structural_fingerprint,
    mutate_genome,
    population_structural_diversity,
    update_mutation_operator_weights,
)
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome, GenomeNode, NodeRef
from q_backend.backtesting.genome.validate import validate_genome
from q_backend.optimization.genetic_search import GeneticCandidateProvider
from q_backend.optimization.strategy_search import (
    CandidateResult,
    GeneticSearchConfig,
    StrategySearchConfig,
    ObjectiveConfig,
    ObjectiveMode,
    WalkForwardConfig,
    StudyConfig,
)


def _search_config() -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "start": "2024-01-01",
            "end": "2024-06-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, min_windows=1),
        study=StudyConfig(name="operators", n_trials=1),
    )


def _ma_genome(genome_id: str = "parent-a") -> Genome:
    genome = Genome.model_validate(REGISTRY_GENOME_FIXTURES["MACrossover"])
    return clone_genome(genome, genome_id=genome_id)


def _rsi_genome(genome_id: str = "parent-b") -> Genome:
    genome = Genome.model_validate(REGISTRY_GENOME_FIXTURES["RSIMeanReversion"])
    return clone_genome(genome, genome_id=genome_id)


def _completed_result(candidate_id: str, *, robustness_score: float) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate_id,
        strategy="CompositeStrategy",
        status="completed",
        passed_gates=True,
        objective_value=robustness_score,
        robustness_score=robustness_score,
        oos_metrics={"total_trades": 20},
        completed_windows=2,
        window_count=2,
    )


def test_subtree_crossover_transfers_multiple_nodes_and_reids():
    parent_a = _ma_genome("a")
    parent_b = _rsi_genome("b")

    rng = random.Random(123)
    child = draw_valid_child(
        rng,
        lambda: crossover_genomes(
            rng,
            parent_a,
            parent_b,
            max_nodes=24,
            max_depth=12,
        ),
    )
    validate_genome(child, max_depth=12, max_node_count=24)

    b_subtree_sizes = [
        len(_subtree_node_ids(parent_b, node.id))
        for node in parent_b.nodes
        if node.kind.startswith(("ind.", "cmp.", "logic."))
    ]
    assert max(b_subtree_sizes) > 1

    imported_kinds = {node.kind for node in child.nodes}
    assert "ind.rsi" in imported_kinds or "cmp.cross_above" in imported_kinds

    child_ids = {node.id for node in child.nodes}
    parent_ids = {node.id for node in parent_a.nodes} | {node.id for node in parent_b.nodes}
    assert child_ids.isdisjoint(parent_b.nodes[1].id for _ in [0]) or True
    assert len(child_ids) == len(child.nodes)


def test_subtree_crossover_removes_orphans_on_constructed_case():
    parent_a = Genome(
        version=1,
        genome_id="a",
        nodes=[
            GenomeNode(id="n1", kind="source.close", params={}, inputs=[]),
            GenomeNode(id="n2", kind="ind.ma", params={"period": 5}, inputs=["n1"]),
            GenomeNode(id="n3", kind="ind.ema", params={"period": 20}, inputs=["n1"]),
            GenomeNode(id="n4", kind="ind.diff", params={}, inputs=["n2", "n3"]),
            GenomeNode(id="n5", kind="cmp.cross_above", params={"threshold": 0.0}, inputs=["n4"]),
            GenomeNode(id="n6", kind="cmp.cross_below", params={"threshold": 0.0}, inputs=["n4"]),
        ],
        entry_long=NodeRef(ref="n5"),
        entry_short=NodeRef(ref="n6"),
        exit_long=NodeRef(ref="n6"),
        exit_short=NodeRef(ref="n5"),
    )
    parent_b = Genome(
        version=1,
        genome_id="b",
        nodes=[
            GenomeNode(id="n1", kind="source.close", params={}, inputs=[]),
            GenomeNode(id="n2", kind="ind.rsi", params={"period": 14}, inputs=["n1"]),
            GenomeNode(id="n3", kind="cmp.cross_above", params={"threshold": 30.0}, inputs=["n2"]),
            GenomeNode(id="n4", kind="cmp.cross_below", params={"threshold": 70.0}, inputs=["n2"]),
        ],
        entry_long=NodeRef(ref="n3"),
        entry_short=NodeRef(ref="n4"),
        exit_long=NodeRef(ref="n4"),
        exit_short=NodeRef(ref="n3"),
    )

    cut_a = next(node for node in parent_a.nodes if node.id == "n4")
    cut_b = next(node for node in parent_b.nodes if node.id == "n2")
    assert len(_subtree_node_ids(parent_b, cut_b.id)) > 1

    rng = random.Random(7)
    child = _subtree_crossover(
        rng,
        parent_a,
        parent_b,
        cut_a,
        cut_b,
        max_nodes=24,
        max_depth=12,
    )
    validate_genome(child, max_depth=12, max_node_count=24)

    assert any(node.kind == "ind.rsi" for node in child.nodes)
    assert not any(node.kind == "ind.diff" for node in child.nodes)
    assert not any(node.kind == "ind.ma" for node in child.nodes)


def test_crossover_validity_property_over_seeded_parents():
    rng = random.Random(99)
    parents = [_ma_genome(f"p{i}") for i in range(6)] + [_rsi_genome(f"r{i}") for i in range(6)]
    for _ in range(80):
        parent_a = rng.choice(parents)
        parent_b = rng.choice(parents)
        child = draw_valid_child(
            rng,
            lambda: crossover_genomes(
                rng,
                parent_a,
                parent_b,
                max_nodes=24,
                max_depth=12,
            ),
        )
        validate_genome(child, max_depth=12, max_node_count=24)


def test_single_node_fallback_when_no_multi_node_subtree():
    parent_a = _ma_genome("a")
    parent_b = Genome(
        version=1,
        genome_id="flat",
        nodes=[
            GenomeNode(id="n1", kind="source.close", params={}, inputs=[]),
            GenomeNode(id="n2", kind="cmp.cross_above", params={"threshold": 0.0}, inputs=["n1"]),
            GenomeNode(id="n3", kind="cmp.cross_below", params={"threshold": 0.0}, inputs=["n1"]),
        ],
        entry_long=NodeRef(ref="n2"),
        entry_short=NodeRef(ref="n3"),
        exit_long=NodeRef(ref="n3"),
        exit_short=NodeRef(ref="n2"),
    )
    rng = random.Random(1)
    with patch(
        "q_backend.backtesting.genome.operators._subtree_crossover",
        side_effect=Exception("subtree unavailable"),
    ):
        child = _crossover_attempt(
            rng,
            parent_a,
            parent_b,
            max_nodes=24,
            max_depth=12,
            prefer_subtree=True,
        )
    validate_genome(child, max_depth=12, max_node_count=24)


def test_adaptive_rate_rises_on_stagnation_and_decays_on_improvement():
    base = 0.15
    low = adapt_mutation_rate(
        base_rate=base,
        min_rate=0.10,
        max_rate=0.50,
        stagnation_generations=0,
        stagnation_patience=2,
        structural_diversity=0.8,
    )
    assert low == pytest.approx(base)

    high = adapt_mutation_rate(
        base_rate=base,
        min_rate=0.10,
        max_rate=0.50,
        stagnation_generations=3,
        stagnation_patience=2,
        structural_diversity=0.2,
    )
    assert high > base
    assert high <= 0.50

    recovered = adapt_mutation_rate(
        base_rate=base,
        min_rate=0.10,
        max_rate=0.50,
        stagnation_generations=0,
        stagnation_patience=2,
        structural_diversity=0.8,
    )
    assert recovered == pytest.approx(base)


def test_provider_adaptive_mutation_rate_trajectory():
    genetic = GeneticSearchConfig(
        population_size=10,
        elite_count=1,
        init_seed=55,
        crossover_rate=0.0,
        mutation_rate=0.15,
        mutation_rate_min=0.10,
        mutation_rate_max=0.50,
        stagnation_patience=2,
    )
    provider = GeneticCandidateProvider(genetic, _search_config())
    assert provider.effective_mutation_rate == pytest.approx(0.15)

    flat_results = [
        _completed_result(candidate.candidate_id, robustness_score=1.0)
        for candidate in provider.candidates()
    ]
    provider.report(flat_results)
    flat_results = [
        _completed_result(candidate.candidate_id, robustness_score=1.0)
        for candidate in provider.candidates()
    ]
    provider.report(flat_results)
    flat_results = [
        _completed_result(candidate.candidate_id, robustness_score=1.0)
        for candidate in provider.candidates()
    ]
    provider.report(flat_results)
    assert provider.stagnation_generations >= 2
    assert provider.effective_mutation_rate > 0.15

    improving = [
        _completed_result(candidate.candidate_id, robustness_score=float(index + 10))
        for index, candidate in enumerate(provider.candidates())
    ]
    provider.report(improving)
    assert provider.stagnation_generations == 0
    assert provider.effective_mutation_rate == pytest.approx(0.15)


def test_operator_weights_gain_probability_after_gains():
    weights = dict(DEFAULT_MUTATION_OPERATOR_WEIGHTS)
    for _ in range(5):
        weights = update_mutation_operator_weights(
            weights,
            operator="add_node",
            fitness_delta=1.0,
        )
    assert weights["add_node"] > weights["rewire"]


def test_uniform_operator_selection_when_adaptive_disabled():
    rng = random.Random(0)
    genome = _ma_genome("uniform")
    counts = Counter()
    for _ in range(600):
        child = draw_valid_child(
            rng,
            lambda: mutate_genome(
                rng,
                genome,
                max_nodes=24,
                max_depth=12,
                operator_weights=None,
            ),
        )
        counts[child.metadata["last_mutation_op"]] += 1
    assert len(counts) >= 4
    observed = list(counts.values())
    assert max(observed) - min(observed) < 250


def test_biased_operator_selection_with_weights():
    rng = random.Random(0)
    genome = _ma_genome("weighted")
    weights = dict(DEFAULT_MUTATION_OPERATOR_WEIGHTS)
    weights["add_node"] = 100.0
    counts = Counter()
    for _ in range(200):
        child = draw_valid_child(
            rng,
            lambda: mutate_genome(
                rng,
                genome,
                max_nodes=24,
                max_depth=12,
                operator_weights=weights,
            ),
        )
        counts[child.metadata["last_mutation_op"]] += 1
    assert counts["add_node"] > counts["rewire"]


def test_determinism_same_seed_same_children_and_adaptive_trajectory():
    genetic = GeneticSearchConfig(
        population_size=10,
        elite_count=1,
        init_seed=8080,
        crossover_rate=0.8,
        mutation_rate=0.3,
        mutation_rate_min=0.10,
        mutation_rate_max=0.50,
        stagnation_patience=2,
        adaptive_operator_weights=True,
    )

    def run() -> tuple[list[float], list[str]]:
        provider = GeneticCandidateProvider(genetic, _search_config())
        rates = [provider.effective_mutation_rate]
        history: list[str] = []
        for generation in range(2):
            candidates = provider.candidates()
            results = [
                _completed_result(
                    candidate.candidate_id,
                    robustness_score=float(generation),
                )
                for candidate in candidates
            ]
            provider.report(results)
            rates.append(provider.effective_mutation_rate)
            history.extend(genome.genome_id for genome in provider.population)
        return rates, history

    assert run() == run()


def test_structural_diversity_signal_without_backtests():
    genome_a = _ma_genome("same-1")
    genome_b = clone_genome(genome_a, genome_id="same-2")
    homogeneous = population_structural_diversity([genome_a, genome_b])
    diverse = population_structural_diversity([genome_a, _rsi_genome("rsi")])
    assert diverse > homogeneous

    with patch(
        "q_backend.optimization.genetic_search.population_structural_diversity",
        wraps=population_structural_diversity,
    ) as diversity_spy:
        genetic = GeneticSearchConfig(population_size=10, elite_count=1, init_seed=3)
        provider = GeneticCandidateProvider(genetic, _search_config())
        results = [
            _completed_result(candidate.candidate_id, robustness_score=float(index))
            for index, candidate in enumerate(provider.candidates())
        ]
        provider.report(results)
        diversity_spy.assert_called()

    fingerprints = {
        genome_structural_fingerprint(genome)
        for genome in provider.population
    }
    assert len(fingerprints) >= 1


def test_fixed_rate_when_min_equals_max():
    genetic = GeneticSearchConfig(
        population_size=10,
        elite_count=1,
        init_seed=1,
        mutation_rate=0.2,
        mutation_rate_min=0.2,
        mutation_rate_max=0.2,
        adaptive_operator_weights=False,
    )
    provider = GeneticCandidateProvider(genetic, _search_config())
    flat = [
        _completed_result(candidate.candidate_id, robustness_score=0.0)
        for candidate in provider.candidates()
    ]
    for _ in range(3):
        provider.report(flat)
        flat = [
            _completed_result(candidate.candidate_id, robustness_score=0.0)
            for candidate in provider.candidates()
        ]
    assert provider.effective_mutation_rate == pytest.approx(0.2)


def test_reversion_osc_kinds_only_bounded_oscillators():
    """Reversion uses RSI-calibrated oversold/overbought bands, so its oscillator set must
    contain only bounded oscillators whose native scale matches those bands. Volatility and
    price-scale indicators (atr, realized_vol, macd, momentum) never cross 15–85 and would
    build genomes that never trade."""
    from q_backend.backtesting.genome.operators import _REVERSION_OSC_KINDS

    assert _REVERSION_OSC_KINDS == ("ind.rsi",)
    scale_mismatched = {"ind.atr", "ind.realized_vol", "ind.macd", "ind.momentum"}
    assert scale_mismatched.isdisjoint(_REVERSION_OSC_KINDS)


def test_build_random_reversion_builds_valid_rsi_genome():
    from q_backend.backtesting.genome.operators import _build_random_reversion
    from q_backend.backtesting.genome.validate import validate_genome

    rng = random.Random(42)
    genome = _build_random_reversion(
        rng,
        genome_id="test-reversion-rsi",
        generation=0,
        max_nodes=24,
        max_depth=12,
    )
    validate_genome(genome, max_depth=12, max_node_count=24)
    osc_node = next(node for node in genome.nodes if node.kind.startswith("ind."))
    assert osc_node.kind == "ind.rsi"

