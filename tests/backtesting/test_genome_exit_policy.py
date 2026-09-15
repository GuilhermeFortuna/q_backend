"""Tests for genome exit-rule policy support (WO80)."""

from __future__ import annotations

import random

import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.exit_rule_policy import (
    attach_exit_rule_policy,
    build_exit_rule_policy,
    exit_policy_metadata_for_genome,
)
from q_backend.backtesting.genome.operators import (
    EXIT_MUTATION_OPERATORS,
    MUTATION_OPERATORS,
    build_initial_population,
    mutate_genome,
)
from q_backend.backtesting.genome.registry_fixtures import MA_CROSSOVER_GENOME
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.search_space import derive_genome_search_space
from q_backend.backtesting.genome.validate import validate_genome
from q_backend.backtesting.models import SignalAction, Trade
from q_backend.optimization.models import FloatParam
from q_backend.optimization.strategy_search import GeneticSearchConfig


def _ma_genome() -> Genome:
    return Genome.model_validate(MA_CROSSOVER_GENOME)


def test_legacy_genomes_validate_without_exit_policy():
    validate_genome(_ma_genome(), max_depth=12, max_node_count=24)


@pytest.mark.parametrize("preset_id", [preset.id for preset in EXIT_PRESETS])
def test_genomes_with_each_exit_preset_validate(preset_id: str):
    preset = next(item for item in EXIT_PRESETS if item.id == preset_id)
    genome = attach_exit_rule_policy(_ma_genome(), preset)
    validate_genome(genome, max_depth=12, max_node_count=24)


def test_exit_policy_search_space_enable_params_have_low_zero():
    preset = next(item for item in EXIT_PRESETS if item.id == "fixed_pct_bracket")
    genome = attach_exit_rule_policy(_ma_genome(), preset)
    search_space, _fixed = derive_genome_search_space(genome)
    assert search_space.strategy_params["exit_stop_loss_pct"].low == 0.0
    assert search_space.strategy_params["exit_take_profit_pct"].low == 0.0
    assert isinstance(search_space.strategy_params["exit_stop_loss_pct"], FloatParam)


def test_composite_strategy_fixed_stop_exits_with_reason():
    genome = _ma_genome()
    genome.metadata["exit_rule_policy"] = build_exit_rule_policy(
        next(item for item in EXIT_PRESETS if item.id == "fixed_pct_bracket")
    )
    strategy = CompositeStrategy(
        genome=genome,
        params={"exit_stop_loss_pct": 0.02, "exit_take_profit_pct": 0.0},
        symbol="TEST",
    )
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0, 97.0],
            "high": [100.0, 100.0, 98.0],
            "low": [100.0, 100.0, 96.0],
            "close": [100.0, 100.0, 96.5],
            "volume": [1000, 1000, 1000],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )
    enriched = strategy.compute_indicators(df)
    open_trade = Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=enriched.index[0],
        entry_price=100.0,
        point_value=1.0,
    )
    signals = strategy.exit_strategy.check_exits([open_trade], enriched.iloc[-1])
    assert signals
    assert signals[0].exit_reason == "fixed_sl"


def test_genome_signal_exits_still_work_without_exit_policy():
    genome = _ma_genome()
    strategy = CompositeStrategy(genome=genome, params={}, symbol="TEST")
    df = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [100.0, 101.0, 102.0],
            "low": [100.0, 101.0, 102.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [1000, 1000, 1000],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )
    enriched = strategy.compute_indicators(df)
    assert "exit_long_signal" in enriched.columns


def test_exit_rule_precedence_before_genome_signal():
    genome = _ma_genome()
    genome.metadata["exit_rule_policy"] = {
        "preset_id": "fixed_stop_only",
        "params": {"stop_loss_pct": {"param": "exit_stop_loss_pct"}},
    }
    strategy = CompositeStrategy(
        genome=genome,
        params={"exit_stop_loss_pct": 0.01},
        symbol="TEST",
    )
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0, 98.0],
            "high": [100.0, 100.0, 99.0],
            "low": [100.0, 100.0, 97.0],
            "close": [100.0, 100.0, 97.5],
            "volume": [1000, 1000, 1000],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )
    enriched = strategy.compute_indicators(df)
    enriched.loc[enriched.index[-1], "exit_long_signal"] = False
    open_trade = Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=enriched.index[0],
        entry_price=100.0,
        point_value=1.0,
    )
    rule_exits = strategy.exit_strategy.check_exits([open_trade], enriched.iloc[-1])
    assert rule_exits
    # Genome exit column stays False on the last bar (rules fire; genome does not).
    assert not bool(enriched.iloc[-1]["q_signal_exit_long"])


@pytest.mark.parametrize("op", EXIT_MUTATION_OPERATORS)
def test_exit_only_mutations_preserve_entries(op: str):
    genome = attach_exit_rule_policy(
        _ma_genome(),
        next(item for item in EXIT_PRESETS if item.id == "fixed_pct_bracket"),
    )
    before = (genome.entry_long.ref, genome.entry_short.ref)
    rng = random.Random(0)
    weights = {name: 0.0 for name in MUTATION_OPERATORS}
    weights[op] = 1.0
    mutated = mutate_genome(
        rng,
        genome,
        max_nodes=24,
        max_depth=12,
        operator_weights=weights,
    )
    assert (mutated.entry_long.ref, mutated.entry_short.ref) == before
    assert mutated.metadata.get("last_exit_mutation_op") == op


def test_swap_exit_policy_moves_between_preset_families():
    genome = attach_exit_rule_policy(
        _ma_genome(),
        next(item for item in EXIT_PRESETS if item.id == "fixed_pct_bracket"),
    )
    seen: set[str] = set()
    rng = random.Random(11)
    weights = {name: 0.0 for name in MUTATION_OPERATORS}
    weights["swap_exit_policy"] = 1.0
    current = genome
    for _ in range(12):
        current = mutate_genome(
            rng,
            current,
            max_nodes=24,
            max_depth=12,
            operator_weights=weights,
        )
        policy = current.metadata.get("exit_rule_policy")
        if policy:
            seen.add(policy["preset_id"])
    assert len(seen) >= 3


def test_seed_exit_policies_inserts_variants():
    rng = random.Random(42)
    population = build_initial_population(
        rng,
        population_size=20,
        max_nodes=24,
        max_depth=12,
        seed_exit_policies=True,
        exit_policy_preset_ids=["fixed_pct_bracket", "atr_stop_chandelier"],
        exit_policy_seed_fraction=0.25,
    )
    with_policy = [genome for genome in population if genome.metadata.get("exit_rule_policy") is not None]
    assert len(with_policy) == 5


def test_seed_exit_policies_is_deterministic():
    kwargs = dict(
        population_size=16,
        max_nodes=24,
        max_depth=12,
        seed_exit_policies=True,
        exit_policy_preset_ids=["fixed_pct_bracket", "donchian_channel_trail"],
        exit_policy_seed_fraction=0.25,
    )
    first = build_initial_population(random.Random(7), **kwargs)
    second = build_initial_population(random.Random(7), **kwargs)
    first_ids = [genome.metadata.get("exit_rule_policy", {}).get("preset_id") for genome in first]
    second_ids = [genome.metadata.get("exit_rule_policy", {}).get("preset_id") for genome in second]
    assert first_ids == second_ids


def test_exit_policy_metadata_payload():
    preset = next(item for item in EXIT_PRESETS if item.id == "atr_stop_chandelier")
    genome = attach_exit_rule_policy(_ma_genome(), preset)
    genome.metadata["last_exit_mutation_op"] = "replace_exit_with_preset"
    metadata = exit_policy_metadata_for_genome(genome)
    assert metadata == {
        "exit_policy_id": "atr_stop_chandelier",
        "exit_policy_label": "ATR stop + Chandelier trail",
        "exit_param_names": [
            "exit_atr_period",
            "exit_chandelier_atr_mult",
            "exit_stop_loss_atr",
        ],
        "last_exit_mutation_op": "replace_exit_with_preset",
    }


def test_genetic_config_accepts_exit_policy_fields():
    config = GeneticSearchConfig(
        population_size=12,
        generations=3,
        elite_count=2,
        seed_exit_policies=True,
        exit_policy_preset_ids=["fixed_pct_bracket"],
        exit_policy_seed_fraction=0.2,
    )
    assert config.seed_exit_policies is True
