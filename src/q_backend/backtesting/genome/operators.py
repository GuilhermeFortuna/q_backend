"""Genetic operators for genome DSL evolution (design §4.2, §4.4)."""

from __future__ import annotations

import copy
import random
from collections.abc import Sequence
from typing import Any

import pandas as pd

from q_backend.backtesting.genome.exit_rule_policy import (
    attach_exit_rule_policy,
    build_single_stop_exit_policy,
    drop_exit_rule_policy,
    get_exit_rule_policy,
    genome_exit_param_ref,
    selected_exit_policy_presets,
)
from q_backend.backtesting.genome.activity import (
    genome_signal_activity,
    repair_genome,
)
from q_backend.backtesting.genome.node_specs import (
    NODE_SPECS,
    OutputType,
    add_node_kinds,
    base_indicator_kinds,
    parse_input_ref,
    port_output_type,
    random_init_transform_kinds,
    resolve_node_gen_metadata,
    swap_kinds,
)
from q_backend.backtesting.genome.latent_universe import sample_latent_index
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome, GenomeNode, NodeRef
from q_backend.backtesting.genome.validate import (
    GenomeValidationError,
    collect_genome_param_keys,
    validate_genome,
)

INDICATOR_KINDS = base_indicator_kinds()
ADD_NODE_KINDS = add_node_kinds()
CMP_KINDS = sorted(kind for kind in NODE_SPECS if kind.startswith("cmp."))
EXIT_BOOL_KINDS = ("exit.opposite_signal",)
SOURCE_KIND = "source.close"

EXIT_MUTATION_OPERATORS = (
    "swap_exit_policy",
    "add_exit_stop",
    "replace_exit_with_preset",
    "drop_exit_policy",
    "nudge_exit_param_ref",
)
_FEATURE_LITERAL_DEFAULTS = {
    "session_open": "09:00",
    "session_close": "18:00",
    "window_from": "10:00",
    "window_to": "12:00",
}
MUTATION_OPERATORS = (
    "rewire",
    "swap_indicator",
    "nudge_param",
    "swap_exit",
    "add_node",
    "remove_node",
) + EXIT_MUTATION_OPERATORS
DEFAULT_MUTATION_OPERATOR_WEIGHTS: dict[str, float] = {
    op: 1.0 for op in MUTATION_OPERATORS
}


def genome_node_count(genome: Genome) -> int:
    return len(genome.nodes)


def genome_param_count(genome: Genome) -> int:
    return len(collect_genome_param_keys(genome))


def genome_to_dict(genome: Genome) -> dict[str, Any]:
    return genome.model_dump()


def clone_genome(
    genome: Genome,
    *,
    genome_id: str | None = None,
    generation: int | None = None,
    parent_ids: list[str] | None = None,
) -> Genome:
    data = genome.model_dump()
    if genome_id is not None:
        data["genome_id"] = genome_id
    metadata = dict(data.get("metadata") or {})
    if generation is not None:
        metadata["generation"] = generation
    if parent_ids is not None:
        metadata["parent_ids"] = parent_ids
    data["metadata"] = metadata
    return Genome.model_validate(data)


def _primary_output_type(node: GenomeNode) -> OutputType:
    spec = NODE_SPECS[node.kind]
    if node.kind == "exit.middle_band":
        return "exit_policy"
    port = spec.output_ports[0]
    return spec.port_types[port]


def genome_structural_fingerprint(genome: Genome) -> tuple[str, ...]:
    """Sorted node-kind multiset used for cheap structural diversity."""
    return tuple(sorted(node.kind for node in genome.nodes))


def population_structural_diversity(population: list[Genome]) -> float:
    """Share of unique structural fingerprints in the population (no backtests)."""
    if not population:
        return 1.0
    fingerprints = {genome_structural_fingerprint(genome) for genome in population}
    return len(fingerprints) / len(population)


def adapt_mutation_rate(
    *,
    base_rate: float,
    min_rate: float,
    max_rate: float,
    stagnation_generations: int,
    stagnation_patience: int,
    structural_diversity: float,
) -> float:
    """Raise mutation toward max when stagnating or structurally homogeneous."""
    if min_rate >= max_rate:
        return base_rate
    if stagnation_generations == 0:
        return base_rate

    pressure = 0.0
    if stagnation_generations >= stagnation_patience:
        overshoot = stagnation_generations - stagnation_patience + 1
        pressure += min(1.0, overshoot / max(stagnation_patience, 1))
    if structural_diversity < 0.5:
        pressure += 0.5 - structural_diversity
    pressure = min(1.0, pressure)

    adapted = base_rate + pressure * (max_rate - base_rate)
    return min(max_rate, max(min_rate, adapted))


def _weighted_choice(
    rng: random.Random,
    choices: tuple[str, ...],
    weights: dict[str, float],
) -> str:
    total = sum(max(weights.get(choice, 1.0), 0.0) for choice in choices)
    if total <= 0:
        return rng.choice(choices)
    pick = rng.uniform(0.0, total)
    cumulative = 0.0
    for choice in choices:
        cumulative += max(weights.get(choice, 1.0), 0.0)
        if pick <= cumulative:
            return choice
    return choices[-1]


def _choose_seeding(
    rng: random.Random,
    choices: tuple[str, ...],
    weights: dict[str, float],
) -> str:
    """Uniform ``rng.choice`` when weights are empty; else score-biased draw (WO152)."""
    if not weights:
        return rng.choice(choices)
    return _weighted_choice(rng, choices, weights)


_CROSSOVER_TREND_KINDS = ("ind.ma", "ind.ema")
# Reversion entries fire on cross_above(oversold)/cross_below(overbought), and those
# thresholds are hard-wired to the RSI 0–100 scale (see param_bounds: oversold 15–40,
# overbought 60–85). Only a bounded, centered oscillator like RSI actually crosses those
# bands. momentum (fractional return ~±0.1), macd (price-scale diff), atr and realized_vol
# (positive volatility, never in 15–85) would build valid genomes that essentially never
# trade — dead weight in the GA population. Keep this set to oscillators whose native scale
# matches the oversold/overbought bands.
_REVERSION_OSC_KINDS = ("ind.rsi",)
_BREAKOUT_STYLES = ("donchian", "bollinger", "trb")
_BREAKOUT_STYLE_KIND = {
    "donchian": "ind.donchian",
    "bollinger": "ind.bollinger",
    "trb": "ind.trb_channel",
}


def _filter_indicator_kinds(
    candidates: tuple[str, ...],
    indicator_kinds: Sequence[str],
) -> tuple[str, ...]:
    filtered = tuple(kind for kind in candidates if kind in indicator_kinds)
    return filtered or candidates


def _registry_primary_kind(fixture: dict[str, Any]) -> str:
    for node in fixture["nodes"]:
        kind = node["kind"]
        if kind.startswith("ind.") and kind not in {"ind.diff", "ind.ratio"}:
            return kind
    return "ind.ma"


def _random_archetypes(
    *,
    indicator_kinds: Sequence[str],
    kind_weights: dict[str, float],
    n_latents: int,
) -> tuple[str, ...]:
    archetypes = ["crossover", "reversion", "breakout"]
    # Latents are seedable whenever a PRODUCTION model supplies them — independent of
    # feature scores. Scores only bias *how often* (via _archetype_choice_weights), they
    # are not a precondition. When no production model exists, ``ind.latent`` is absent
    # from ``indicator_kinds`` and ``n_latents == 0``, so the no-model path is unchanged.
    if "ind.latent" in indicator_kinds and n_latents > 0:
        archetypes.append("latent")
    return tuple(archetypes)


def _archetype_choice_weights(
    archetypes: tuple[str, ...],
    *,
    indicator_kinds: Sequence[str],
    kind_weights: dict[str, float],
) -> dict[str, float]:
    archetype_kinds = {
        "crossover": _CROSSOVER_TREND_KINDS,
        "reversion": _REVERSION_OSC_KINDS,
        "breakout": tuple(_BREAKOUT_STYLE_KIND.values()),
        "latent": ("ind.latent",),
    }
    return {
        archetype: max(
            kind_weights.get(kind, 1.0)
            for kind in _filter_indicator_kinds(archetype_kinds[archetype], indicator_kinds)
        )
        for archetype in archetypes
    }


def update_mutation_operator_weights(
    weights: dict[str, float],
    *,
    operator: str,
    fitness_delta: float,
    learning_rate: float = 0.15,
    min_weight: float = 0.05,
    max_weight: float = 5.0,
) -> dict[str, float]:
    """Nudge operator weights toward ops that recently improved fitness."""
    updated = dict(weights)
    if operator not in updated:
        return updated
    if fitness_delta > 0:
        updated[operator] = min(max_weight, updated[operator] * (1.0 + learning_rate))
    elif fitness_delta < 0:
        updated[operator] = max(min_weight, updated[operator] * (1.0 - learning_rate * 0.5))
    return updated


def _cuttable_nodes(genome: Genome) -> list[GenomeNode]:
    return [
        node
        for node in genome.nodes
        if node.kind.startswith(("ind.", "cmp.", "logic."))
    ]


def _compatible_cut_nodes(cut_a: GenomeNode, cuts_b: list[GenomeNode]) -> list[GenomeNode]:
    output_type = _primary_output_type(cut_a)
    spec_a = NODE_SPECS[cut_a.kind]
    return [
        node
        for node in cuts_b
        if _primary_output_type(node) == output_type
        and NODE_SPECS[node.kind].min_inputs == spec_a.min_inputs
        and NODE_SPECS[node.kind].max_inputs == spec_a.max_inputs
    ]


def _subtree_node_ids(genome: Genome, root_id: str) -> set[str]:
    nodes_by_id = {node.id: node for node in genome.nodes}
    collected: set[str] = set()
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        if node_id in collected or node_id not in nodes_by_id:
            continue
        collected.add(node_id)
        for raw_input in nodes_by_id[node_id].inputs:
            parent_id, _ = parse_input_ref(raw_input)
            stack.append(parent_id)
    return collected


def _remap_input_ref(raw_input: str, id_map: dict[str, str]) -> str:
    node_id, port = parse_input_ref(raw_input)
    mapped_id = id_map.get(node_id, node_id)
    if ":" in raw_input:
        return f"{mapped_id}:{port}"
    return mapped_id


def _next_unique_node_id(existing_ids: set[str], start: int) -> tuple[str, int]:
    candidate = start
    while True:
        node_id = f"n{candidate}"
        if node_id not in existing_ids:
            return node_id, candidate + 1
        candidate += 1


def _copy_subtree_with_new_ids(
    genome: Genome,
    subtree_ids: set[str],
    *,
    existing_ids: set[str],
    start_counter: int,
) -> tuple[list[GenomeNode], dict[str, str], int]:
    nodes_by_id = {node.id: node for node in genome.nodes}
    id_map: dict[str, str] = {}
    counter = start_counter
    for node_id in sorted(subtree_ids):
        new_id, counter = _next_unique_node_id(existing_ids | set(id_map.values()), counter)
        id_map[node_id] = new_id

    copied: list[GenomeNode] = []
    for node_id in sorted(subtree_ids):
        source = nodes_by_id[node_id]
        copied.append(
            GenomeNode(
                id=id_map[node_id],
                kind=source.kind,
                params=copy.deepcopy(source.params),
                inputs=[_remap_input_ref(raw_input, id_map) for raw_input in source.inputs],
            )
        )
    return copied, id_map, counter



def _reachable_node_ids(genome: Genome) -> set[str]:
    nodes_by_id = {node.id: node for node in genome.nodes}
    seeds: list[str] = []
    for ref in (
        genome.entry_long.ref,
        genome.entry_short.ref,
        genome.exit_long.ref,
        genome.exit_short.ref,
    ):
        node_id, _ = parse_input_ref(ref)
        seeds.append(node_id)

    reachable: set[str] = set()
    stack = list(seeds)
    while stack:
        node_id = stack.pop()
        if node_id in reachable or node_id not in nodes_by_id:
            continue
        reachable.add(node_id)
        for raw_input in nodes_by_id[node_id].inputs:
            parent_id, _ = parse_input_ref(raw_input)
            stack.append(parent_id)
    return reachable


def _prune_unreachable_nodes(genome: Genome) -> Genome:
    reachable = _reachable_node_ids(genome)
    child = clone_genome(genome)
    child.nodes = [node for node in child.nodes if node.id in reachable]
    return child


def _rewire_consumers(
    genome: Genome,
    *,
    old_root_id: str,
    new_root_id: str,
) -> None:
    for node in genome.nodes:
        rewired_inputs: list[str] = []
        for raw_input in node.inputs:
            parent_id, port = parse_input_ref(raw_input)
            if parent_id == old_root_id:
                rewired_inputs.append(
                    f"{new_root_id}:{port}" if ":" in raw_input else new_root_id
                )
            else:
                rewired_inputs.append(raw_input)
        node.inputs = rewired_inputs
    for attr in ("entry_long", "entry_short", "exit_long", "exit_short"):
        ref = getattr(genome, attr)
        node_id, port = parse_input_ref(ref.ref)
        if node_id == old_root_id:
            mapped = f"{new_root_id}:{port}" if ":" in ref.ref else new_root_id
            setattr(genome, attr, NodeRef(ref=mapped))


def _max_numeric_node_suffix(node_ids: set[str]) -> int:
    suffixes = [
        int(node_id[1:])
        for node_id in node_ids
        if node_id.startswith("n") and node_id[1:].isdigit()
    ]
    return max(suffixes, default=0)


def _prune_subtree_leaves(
    subtree_ids: set[str],
    genome: Genome,
    *,
    keep_root: str,
) -> set[str]:
    nodes_by_id = {node.id: node for node in genome.nodes}
    dependents: dict[str, int] = {node_id: 0 for node_id in subtree_ids}
    for node_id in subtree_ids:
        for raw_input in nodes_by_id[node_id].inputs:
            parent_id, _ = parse_input_ref(raw_input)
            if parent_id in subtree_ids:
                dependents[parent_id] = dependents.get(parent_id, 0) + 1

    leaves = [
        node_id
        for node_id in subtree_ids
        if node_id != keep_root and dependents.get(node_id, 0) == 0
    ]
    if not leaves:
        return subtree_ids
    pruned = set(subtree_ids)
    pruned.remove(leaves[0])
    return pruned


def _single_node_crossover(
    child: Genome,
    cut_a: GenomeNode,
    cut_b: GenomeNode,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    cut_a.kind = cut_b.kind
    cut_a.params = copy.deepcopy(cut_b.params)
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _subtree_crossover(
    rng: random.Random,
    parent_a: Genome,
    parent_b: Genome,
    cut_a: GenomeNode,
    cut_b: GenomeNode,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    child = clone_genome(parent_a)
    nodes_by_id = {node.id: node for node in child.nodes}
    if cut_a.id not in nodes_by_id:
        return child

    subtree_b_ids = _subtree_node_ids(parent_b, cut_b.id)
    if len(subtree_b_ids) <= 1:
        raise GenomeValidationError("subtree too small for crossover")

    pruned_ids = set(subtree_b_ids)
    while True:
        existing_ids = {node.id for node in child.nodes if node.id != cut_a.id}
        imported, id_map, _counter = _copy_subtree_with_new_ids(
            parent_b,
            pruned_ids,
            existing_ids=existing_ids,
            start_counter=_max_numeric_node_suffix(existing_ids) + 1,
        )
        new_root_id = id_map[cut_b.id]

        child.nodes = [node for node in child.nodes if node.id != cut_a.id]
        child.nodes.extend(imported)
        _rewire_consumers(child, old_root_id=cut_a.id, new_root_id=new_root_id)
        child = _prune_unreachable_nodes(child)

        try:
            return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)
        except GenomeValidationError:
            if len(pruned_ids) <= 1:
                raise
            next_pruned = _prune_subtree_leaves(
                pruned_ids,
                parent_b,
                keep_root=cut_b.id,
            )
            if next_pruned == pruned_ids:
                raise
            pruned_ids = next_pruned
            child = clone_genome(parent_a)


def _crossover_attempt(
    rng: random.Random,
    parent_a: Genome,
    parent_b: Genome,
    *,
    max_nodes: int,
    max_depth: int,
    prefer_subtree: bool,
) -> Genome:
    child = clone_genome(parent_a)
    cuts_a = _cuttable_nodes(child)
    cuts_b = _cuttable_nodes(parent_b)
    if not cuts_a or not cuts_b:
        return child

    cut_a = rng.choice(cuts_a)
    compatible_b = _compatible_cut_nodes(cut_a, cuts_b)
    if not compatible_b:
        return child

    multi_node = [
        node
        for node in compatible_b
        if len(_subtree_node_ids(parent_b, node.id)) > 1
    ]
    if prefer_subtree and multi_node:
        cut_b = rng.choice(multi_node)
        try:
            return _subtree_crossover(
                rng,
                parent_a,
                parent_b,
                cut_a,
                cut_b,
                max_nodes=max_nodes,
                max_depth=max_depth,
            )
        except GenomeValidationError:
            pass

    cut_b = rng.choice(compatible_b)
    return _single_node_crossover(
        child,
        cut_a,
        cut_b,
        max_nodes=max_nodes,
        max_depth=max_depth,
    )


def _validate_or_raise(
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    validate_genome(genome, max_depth=max_depth, max_node_count=max_nodes)
    return genome


def _default_param_value(key: str) -> Any:
    return GENOME_PARAM_BOUNDS[key].default


def _random_param_ref(rng: random.Random, key: str) -> dict[str, str]:
    return {"param": key}


def _random_node_params(
    rng: random.Random,
    kind: str,
    *,
    n_latents: int = 0,
) -> dict[str, Any]:
    spec = NODE_SPECS[kind]
    params: dict[str, Any] = {}
    for key in sorted(spec.allowed_param_keys):
        if key in _FEATURE_LITERAL_DEFAULTS:
            params[key] = _FEATURE_LITERAL_DEFAULTS[key]
        elif kind == "ind.latent" and key == "latent_index":
            params[key] = sample_latent_index(rng, n_latents)
        else:
            params[key] = _random_param_ref(rng, key)
    return params


def _light_mutate_params(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
    n_latents: int = 0,
) -> Genome:
    mutated = clone_genome(genome)
    if not mutated.nodes:
        return mutated

    node = rng.choice(mutated.nodes)
    spec = NODE_SPECS[node.kind]
    if not spec.allowed_param_keys:
        return mutated

    key = rng.choice(sorted(spec.allowed_param_keys))
    bounds = GENOME_PARAM_BOUNDS.get(key)
    if bounds is None:
        return mutated

    if node.kind == "ind.latent" and key == "latent_index":
        node.params[key] = sample_latent_index(rng, n_latents)
    elif bounds.type == "categorical" and bounds.choices:
        node.params[key] = rng.choice(list(bounds.choices))
    elif bounds.type == "int" and bounds.min is not None and bounds.max is not None:
        if key == "latent_index" and n_latents > 0:
            node.params[key] = sample_latent_index(rng, n_latents)
        else:
            node.params[key] = rng.randint(int(bounds.min), int(bounds.max))
    elif bounds.type == "float" and bounds.min is not None and bounds.max is not None:
        node.params[key] = round(rng.uniform(bounds.min, bounds.max), 4)
    else:
        node.params[key] = _random_param_ref(rng, key)

    return _validate_or_raise(mutated, max_nodes=max_nodes, max_depth=max_depth)


def _entry_refs(genome: Genome) -> tuple[str, str]:
    return genome.entry_long.ref, genome.entry_short.ref


def _mutate_swap_exit_policy(
    rng: random.Random,
    genome: Genome,
    *,
    preset_ids: list[str] | None,
) -> Genome:
    presets = selected_exit_policy_presets(preset_ids)
    if not presets:
        return clone_genome(genome)
    child = clone_genome(genome)
    current = get_exit_rule_policy(child)
    current_id = current.get("preset_id") if current else None
    choices = [preset for preset in presets if preset.id != current_id] or presets
    return attach_exit_rule_policy(child, rng.choice(choices))


def _mutate_add_exit_stop(rng: random.Random, genome: Genome) -> Genome:
    if get_exit_rule_policy(genome) is not None:
        return clone_genome(genome)
    child = clone_genome(genome)
    policy = build_single_stop_exit_policy(atr=rng.random() < 0.5)
    metadata = dict(child.metadata or {})
    metadata["exit_rule_policy"] = policy
    child.metadata = metadata
    return child


def _mutate_replace_exit_with_preset(
    rng: random.Random,
    genome: Genome,
    *,
    preset_ids: list[str] | None,
) -> Genome:
    presets = selected_exit_policy_presets(preset_ids)
    if not presets:
        return clone_genome(genome)
    return attach_exit_rule_policy(clone_genome(genome), rng.choice(presets))


def _mutate_drop_exit_policy(genome: Genome) -> Genome:
    if get_exit_rule_policy(genome) is None:
        return clone_genome(genome)
    return drop_exit_rule_policy(genome)


def _mutate_nudge_exit_param_ref(rng: random.Random, genome: Genome) -> Genome:
    policy = get_exit_rule_policy(genome)
    if policy is None:
        return clone_genome(genome)
    child = clone_genome(genome)
    params = dict(policy.get("params") or {})
    if not params:
        return child
    exit_name = rng.choice(sorted(params.keys()))
    params[exit_name] = {"param": genome_exit_param_ref(exit_name)}
    metadata = dict(child.metadata or {})
    metadata["exit_rule_policy"] = {**policy, "params": params}
    child.metadata = metadata
    return child


def mutate_genome(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
    probe_df: pd.DataFrame | None = None,
    min_signals: int = 0,
    repair_max_attempts: int = 8,
    operator_weights: dict[str, float] | None = None,
    exit_policy_preset_ids: list[str] | None = None,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    n_latents: int = 0,
) -> Genome:
    weights = operator_weights or DEFAULT_MUTATION_OPERATOR_WEIGHTS
    op = _weighted_choice(rng, MUTATION_OPERATORS, weights)
    before_entries = _entry_refs(genome)
    if op == "rewire":
        mutated = _mutate_rewire(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    elif op == "swap_indicator":
        mutated = _mutate_swap_indicator(
            rng,
            genome,
            max_nodes=max_nodes,
            max_depth=max_depth,
            indicator_kinds=indicator_kinds,
            n_latents=n_latents,
        )
    elif op == "nudge_param":
        mutated = _mutate_nudge_param(
            rng,
            genome,
            max_nodes=max_nodes,
            max_depth=max_depth,
            probe_df=probe_df,
            min_signals=min_signals,
            repair_max_attempts=repair_max_attempts,
            n_latents=n_latents,
        )
    elif op == "swap_exit":
        mutated = _mutate_swap_exit(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    elif op == "add_node":
        mutated = _mutate_add_node(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    elif op == "remove_node":
        mutated = _mutate_remove_node(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    elif op == "swap_exit_policy":
        mutated = _mutate_swap_exit_policy(
            rng, genome, preset_ids=exit_policy_preset_ids
        )
    elif op == "add_exit_stop":
        mutated = _mutate_add_exit_stop(rng, genome)
    elif op == "replace_exit_with_preset":
        mutated = _mutate_replace_exit_with_preset(
            rng, genome, preset_ids=exit_policy_preset_ids
        )
    elif op == "drop_exit_policy":
        mutated = _mutate_drop_exit_policy(genome)
    else:
        mutated = _mutate_nudge_exit_param_ref(rng, genome)

    metadata = dict(mutated.metadata)
    metadata["last_mutation_op"] = op
    if op in EXIT_MUTATION_OPERATORS:
        metadata["last_exit_mutation_op"] = op
        assert _entry_refs(mutated) == before_entries
    mutated.metadata = metadata
    return mutated


def _mutate_rewire(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    child = clone_genome(genome)
    candidates = [node for node in child.nodes if node.inputs]
    if not candidates:
        return child
    target = rng.choice(candidates)
    idx = rng.randrange(len(target.inputs))
    producers = [node for node in child.nodes if node.id != target.id]
    if not producers:
        return child
    target.inputs[idx] = rng.choice(producers).id
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _mutate_swap_indicator(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    n_latents: int = 0,
) -> Genome:
    child = clone_genome(genome)
    swappable = [
        node
        for node in child.nodes
        if node.kind.startswith(("ind.", "transform.", "feature."))
        and resolve_node_gen_metadata(NODE_SPECS[node.kind]).swap
    ]
    if not swappable:
        return child
    node = rng.choice(swappable)
    old_type = _primary_output_type(node)
    compatible = [
        kind
        for kind in swap_kinds(tuple(indicator_kinds))
        if _node_output_type(kind) == old_type
        and NODE_SPECS[kind].min_inputs == NODE_SPECS[node.kind].min_inputs
    ]
    if not compatible:
        return child
    new_kind = rng.choice(compatible)
    node.kind = new_kind
    node.params = _random_node_params(rng, new_kind, n_latents=n_latents)
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _node_output_type(kind: str) -> OutputType:
    spec = NODE_SPECS[kind]
    return spec.port_types[spec.output_ports[0]]


def _indicator_output_type(kind: str) -> OutputType:
    return _node_output_type(kind)


def _maybe_wrap_with_transform(
    rng: random.Random,
    *,
    nodes: list[GenomeNode],
    source_id: str,
    next_node_index: int,
) -> tuple[str, int]:
    pool = random_init_transform_kinds()
    if not pool or rng.random() >= 0.35:
        return source_id, next_node_index
    kind = rng.choice(pool)
    spec = NODE_SPECS[kind]
    transform_id = f"n{next_node_index}"
    next_node_index += 1
    params = {key: _random_param_ref(rng, key) for key in sorted(spec.allowed_param_keys)}
    nodes.append(
        GenomeNode(
            id=transform_id,
            kind=kind,
            params=params,
            inputs=[source_id],
        )
    )
    return transform_id, next_node_index


def _mutate_nudge_param(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
    probe_df: pd.DataFrame | None = None,
    min_signals: int = 0,
    repair_max_attempts: int = 8,
    n_latents: int = 0,
) -> Genome:
    if (
        probe_df is not None
        and len(probe_df) > 0
        and min_signals > 0
        and not genome_signal_activity(
            genome,
            probe_df,
            min_signals=min_signals,
            max_depth=max_depth,
            max_node_count=max_nodes,
        ).is_tradeable
    ):
        repaired = repair_genome(
            rng,
            genome,
            probe_df,
            min_signals=min_signals,
            max_nodes=max_nodes,
            max_depth=max_depth,
            max_attempts=repair_max_attempts,
        )
        if genome_signal_activity(
            repaired,
            probe_df,
            min_signals=min_signals,
            max_depth=max_depth,
            max_node_count=max_nodes,
        ).is_tradeable:
            return repaired
    return _light_mutate_params(
        rng,
        genome,
        max_nodes=max_nodes,
        max_depth=max_depth,
        n_latents=n_latents,
    )


def _mutate_swap_exit(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    child = clone_genome(genome)
    bool_nodes = [
        node.id
        for node in child.nodes
        if _primary_output_type(node) == "bool_series"
    ]
    if len(bool_nodes) < 2:
        return child
    child.exit_long = NodeRef(ref=rng.choice(bool_nodes))
    child.exit_short = NodeRef(ref=rng.choice(bool_nodes))
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _mutate_add_node(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    if len(genome.nodes) >= max_nodes:
        return clone_genome(genome)
    child = clone_genome(genome)
    close_nodes = [node for node in child.nodes if node.kind == SOURCE_KIND]
    if not close_nodes:
        close_id = "n1"
        child.nodes.insert(
            0,
            GenomeNode(id=close_id, kind=SOURCE_KIND, params={}, inputs=[]),
        )
    else:
        close_id = close_nodes[0].id

    new_id = f"n{len(child.nodes) + 1}"
    kind = rng.choice(ADD_NODE_KINDS)
    spec = NODE_SPECS[kind]
    params = _random_node_params(rng, kind)
    child.nodes.append(
        GenomeNode(id=new_id, kind=kind, params=params, inputs=[close_id] if spec.min_inputs > 0 else [])
    )

    bool_nodes = [
        node.id
        for node in child.nodes
        if _primary_output_type(node) == "bool_series"
    ]
    if bool_nodes:
        if rng.random() < 0.5:
            child.entry_long = NodeRef(ref=rng.choice(bool_nodes))
        else:
            child.entry_short = NodeRef(ref=rng.choice(bool_nodes))
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _mutate_remove_node(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    child = clone_genome(genome)
    removable = [
        node
        for node in child.nodes
        if not node.kind.startswith("source.") and not node.kind.startswith("exit.")
    ]
    if len(removable) <= 2:
        return child
    target = rng.choice(removable)
    fallback = next(
        (node.id for node in child.nodes if node.kind == SOURCE_KIND),
        child.nodes[0].id,
    )
    for node in child.nodes:
        node.inputs = [fallback if inp.split(":")[0] == target.id else inp for inp in node.inputs]
    child.nodes = [node for node in child.nodes if node.id != target.id]

    for attr in ("entry_long", "entry_short", "exit_long", "exit_short"):
        ref = getattr(child, attr)
        if ref.ref.split(":")[0] == target.id:
            bool_nodes = [
                node.id
                for node in child.nodes
                if _primary_output_type(node) == "bool_series"
            ]
            if not bool_nodes:
                raise GenomeValidationError("no bool nodes after removal")
            setattr(child, attr, NodeRef(ref=bool_nodes[0]))
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def crossover_genomes(
    rng: random.Random,
    parent_a: Genome,
    parent_b: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    return _crossover_attempt(
        rng,
        parent_a,
        parent_b,
        max_nodes=max_nodes,
        max_depth=max_depth,
        prefer_subtree=True,
    )


def _build_random_crossover(
    rng: random.Random,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
    *,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    kind_weights: dict[str, float] | None = None,
) -> Genome:
    weights = kind_weights or {}
    trend_kinds = _filter_indicator_kinds(_CROSSOVER_TREND_KINDS, indicator_kinds)
    close_id = "n1"
    short_id = "n2"
    long_id = "n3"
    diff_id = "n4"
    buy_id = "n5"
    sell_id = "n6"

    short_kind = _choose_seeding(rng, trend_kinds, weights)
    long_kind = _choose_seeding(rng, trend_kinds, weights)
    short_spec = NODE_SPECS[short_kind]
    long_spec = NODE_SPECS[long_kind]

    nodes = [
        GenomeNode(id=close_id, kind=SOURCE_KIND, params={}, inputs=[]),
    ]
    short_input, next_idx = _maybe_wrap_with_transform(
        rng, nodes=nodes, source_id=close_id, next_node_index=7
    )
    long_input, next_idx = _maybe_wrap_with_transform(
        rng, nodes=nodes, source_id=close_id, next_node_index=next_idx
    )
    nodes.extend(
        [
        GenomeNode(
            id=short_id,
            kind=short_kind,
            params={
                key: _random_param_ref(rng, key) for key in sorted(short_spec.allowed_param_keys)
            },
            inputs=[short_input],
        ),
        GenomeNode(
            id=long_id,
            kind=long_kind,
            params={
                key: _random_param_ref(rng, key) for key in sorted(long_spec.allowed_param_keys)
            },
            inputs=[long_input],
        ),
        GenomeNode(id=diff_id, kind="ind.diff", params={}, inputs=[short_id, long_id]),
        GenomeNode(
            id=buy_id,
            kind="cmp.cross_above",
            params={"threshold": _random_param_ref(rng, "threshold")},
            inputs=[diff_id],
        ),
        GenomeNode(
            id=sell_id,
            kind="cmp.cross_below",
            params={"threshold": {"param": "threshold", "negate": True}},
            inputs=[diff_id],
        ),
        ]
    )

    genome = Genome(
        version=1,
        genome_id=genome_id,
        nodes=nodes,
        entry_long=NodeRef(ref=buy_id),
        entry_short=NodeRef(ref=sell_id),
        exit_long=NodeRef(ref=sell_id),
        exit_short=NodeRef(ref=buy_id),
        metadata={"generation": generation, "origin": "random_crossover"},
    )
    return _validate_or_raise(genome, max_nodes=max_nodes, max_depth=max_depth)


def _build_random_reversion(
    rng: random.Random,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
    *,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    kind_weights: dict[str, float] | None = None,
) -> Genome:
    weights = kind_weights or {}
    close_id = "n1"
    osc_id = "n2"
    buy_id = "n3"
    sell_id = "n4"

    osc_kinds = _filter_indicator_kinds(_REVERSION_OSC_KINDS, indicator_kinds)
    if not weights:
        osc_kind = "ind.rsi" if "ind.rsi" in indicator_kinds else osc_kinds[0]
    else:
        osc_kind = _choose_seeding(rng, osc_kinds, weights)
    osc_spec = NODE_SPECS[osc_kind]
    nodes = [
        GenomeNode(id=close_id, kind=SOURCE_KIND, params={}, inputs=[]),
    ]
    osc_input, _next_idx = _maybe_wrap_with_transform(
        rng, nodes=nodes, source_id=close_id, next_node_index=5
    )
    nodes.extend(
        [
        GenomeNode(
            id=osc_id,
            kind=osc_kind,
            params={
                key: _random_param_ref(rng, key) for key in sorted(osc_spec.allowed_param_keys)
            },
            inputs=[osc_input] if osc_spec.max_inputs > 0 else [],
        ),
        GenomeNode(
            id=buy_id,
            kind="cmp.cross_above",
            params={"threshold": {"param": "oversold"}},
            inputs=[osc_id],
        ),
        GenomeNode(
            id=sell_id,
            kind="cmp.cross_below",
            params={"threshold": {"param": "overbought"}},
            inputs=[osc_id],
        ),
        ]
    )

    genome = Genome(
        version=1,
        genome_id=genome_id,
        nodes=nodes,
        entry_long=NodeRef(ref=buy_id),
        entry_short=NodeRef(ref=sell_id),
        exit_long=NodeRef(ref=sell_id),
        exit_short=NodeRef(ref=buy_id),
        metadata={"generation": generation, "origin": "random_reversion"},
    )
    return _validate_or_raise(genome, max_nodes=max_nodes, max_depth=max_depth)


def _build_random_breakout(
    rng: random.Random,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
    *,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    kind_weights: dict[str, float] | None = None,
) -> Genome:
    weights = kind_weights or {}
    close_id = "n1"
    ind_id = "n2"
    buy_id = "n3"
    sell_id = "n4"

    available_styles = tuple(
        style
        for style in _BREAKOUT_STYLES
        if _BREAKOUT_STYLE_KIND[style] in indicator_kinds
    ) or _BREAKOUT_STYLES
    if not weights:
        style = rng.choice(available_styles)
    else:
        style_weights = {
            style: weights.get(_BREAKOUT_STYLE_KIND[style], 1.0)
            for style in available_styles
        }
        style = _choose_seeding(rng, available_styles, style_weights)
    nodes = [GenomeNode(id=close_id, kind=SOURCE_KIND, params={}, inputs=[])]

    if style == "donchian":
        spec = NODE_SPECS["ind.donchian"]
        nodes.append(
            GenomeNode(
                id=ind_id,
                kind="ind.donchian",
                params={
                    key: _random_param_ref(rng, key) for key in sorted(spec.allowed_param_keys)
                },
                inputs=[],
            )
        )
        nodes.append(
            GenomeNode(
                id=buy_id,
                kind="cmp.cross_above",
                params={},
                inputs=[close_id, f"{ind_id}:donchian_upper"],
            )
        )
        nodes.append(
            GenomeNode(
                id=sell_id,
                kind="cmp.cross_below",
                params={},
                inputs=[close_id, f"{ind_id}:donchian_lower"],
            )
        )
        genome = Genome(
            version=1,
            genome_id=genome_id,
            nodes=nodes,
            entry_long=NodeRef(ref=buy_id),
            entry_short=NodeRef(ref=sell_id),
            exit_long=NodeRef(ref=sell_id),
            exit_short=NodeRef(ref=buy_id),
            metadata={"generation": generation, "origin": "random_donchian"},
        )

    elif style == "bollinger":
        spec = NODE_SPECS["ind.bollinger"]
        exit_id = "n5"
        nodes.append(
            GenomeNode(
                id=ind_id,
                kind="ind.bollinger",
                params={
                    key: _random_param_ref(rng, key) for key in sorted(spec.allowed_param_keys)
                },
                inputs=[close_id],
            )
        )
        nodes.append(
            GenomeNode(
                id=buy_id,
                kind="cmp.touch_below",
                params={},
                inputs=[close_id, f"{ind_id}:bb_lower"],
            )
        )
        nodes.append(
            GenomeNode(
                id=sell_id,
                kind="cmp.touch_above",
                params={},
                inputs=[close_id, f"{ind_id}:bb_upper"],
            )
        )
        nodes.append(
            GenomeNode(
                id=exit_id,
                kind="exit.middle_band",
                params={},
                inputs=[close_id, f"{ind_id}:bb_middle"],
            )
        )
        genome = Genome(
            version=1,
            genome_id=genome_id,
            nodes=nodes,
            entry_long=NodeRef(ref=buy_id),
            entry_short=NodeRef(ref=sell_id),
            exit_long=NodeRef(ref=f"{exit_id}:exit_long"),
            exit_short=NodeRef(ref=f"{exit_id}:exit_short"),
            metadata={"generation": generation, "origin": "random_bollinger"},
        )

    else:  # trb
        spec = NODE_SPECS["ind.trb_channel"]
        nodes.append(
            GenomeNode(
                id=ind_id,
                kind="ind.trb_channel",
                params={
                    key: _random_param_ref(rng, key) for key in sorted(spec.allowed_param_keys)
                },
                inputs=[close_id],
            )
        )
        nodes.append(
            GenomeNode(
                id=buy_id,
                kind="cmp.trb_breakout_above",
                params={},
                inputs=[close_id, f"{ind_id}:trb_upper", f"{ind_id}:channel_high"],
            )
        )
        nodes.append(
            GenomeNode(
                id=sell_id,
                kind="cmp.trb_breakout_below",
                params={},
                inputs=[close_id, f"{ind_id}:trb_lower", f"{ind_id}:channel_low"],
            )
        )
        genome = Genome(
            version=1,
            genome_id=genome_id,
            nodes=nodes,
            entry_long=NodeRef(ref=buy_id),
            entry_short=NodeRef(ref=sell_id),
            exit_long=NodeRef(ref=sell_id),
            exit_short=NodeRef(ref=buy_id),
            metadata={"generation": generation, "origin": "random_trb"},
        )

    return _validate_or_raise(genome, max_nodes=max_nodes, max_depth=max_depth)


def _build_random_latent(
    rng: random.Random,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
    *,
    n_latents: int,
) -> Genome:
    latent_id = "n1"
    buy_id = "n2"
    sell_id = "n3"
    latent_index = sample_latent_index(rng, n_latents)
    nodes = [
        GenomeNode(
            id=latent_id,
            kind="ind.latent",
            params={"latent_index": latent_index},
            inputs=[],
        ),
        GenomeNode(
            id=buy_id,
            kind="cmp.cross_above",
            params={"threshold": 0.0},
            inputs=[latent_id],
        ),
        GenomeNode(
            id=sell_id,
            kind="cmp.cross_below",
            params={"threshold": 0.0},
            inputs=[latent_id],
        ),
    ]
    genome = Genome(
        version=1,
        genome_id=genome_id,
        nodes=nodes,
        entry_long=NodeRef(ref=buy_id),
        entry_short=NodeRef(ref=sell_id),
        exit_long=NodeRef(ref=sell_id),
        exit_short=NodeRef(ref=buy_id),
        metadata={"generation": generation, "origin": "random_latent"},
    )
    return _validate_or_raise(genome, max_nodes=max_nodes, max_depth=max_depth)


def build_random_genome(
    rng: random.Random,
    *,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    kind_weights: dict[str, float] | None = None,
    n_latents: int = 0,
) -> Genome:
    weights = kind_weights or {}
    archetypes = _random_archetypes(
        indicator_kinds=indicator_kinds,
        kind_weights=weights,
        n_latents=n_latents,
    )
    if not weights:
        archetype = rng.choice(archetypes)
    else:
        archetype_weights = _archetype_choice_weights(
            archetypes,
            indicator_kinds=indicator_kinds,
            kind_weights=weights,
        )
        archetype = _choose_seeding(rng, archetypes, archetype_weights)
    if archetype == "crossover":
        return _build_random_crossover(
            rng,
            genome_id,
            generation,
            max_nodes,
            max_depth,
            indicator_kinds=indicator_kinds,
            kind_weights=weights,
        )
    if archetype == "reversion":
        return _build_random_reversion(
            rng,
            genome_id,
            generation,
            max_nodes,
            max_depth,
            indicator_kinds=indicator_kinds,
            kind_weights=weights,
        )
    if archetype == "latent":
        return _build_random_latent(
            rng,
            genome_id,
            generation,
            max_nodes,
            max_depth,
            n_latents=n_latents,
        )
    return _build_random_breakout(
        rng,
        genome_id,
        generation,
        max_nodes,
        max_depth,
        indicator_kinds=indicator_kinds,
        kind_weights=weights,
    )


def _tradeable_registry_fallback(
    rng: random.Random,
    *,
    genome_id: str,
    generation: int,
) -> Genome:
    template = Genome.model_validate(
        copy.deepcopy(REGISTRY_GENOME_FIXTURES["MACrossover"])
    )
    return clone_genome(template, genome_id=genome_id, generation=generation)


def _ensure_tradeable_genome(
    rng: random.Random,
    genome: Genome,
    *,
    ohlcv: pd.DataFrame,
    min_signals: int,
    max_nodes: int,
    max_depth: int,
    repair_max_attempts: int,
    genome_id: str,
    generation: int,
) -> Genome:
    if genome_signal_activity(
        genome,
        ohlcv,
        min_signals=min_signals,
        max_depth=max_depth,
        max_node_count=max_nodes,
    ).is_tradeable:
        return genome

    repaired = repair_genome(
        rng,
        genome,
        ohlcv,
        min_signals=min_signals,
        max_nodes=max_nodes,
        max_depth=max_depth,
        max_attempts=repair_max_attempts,
    )
    if genome_signal_activity(
        repaired,
        ohlcv,
        min_signals=min_signals,
        max_depth=max_depth,
        max_node_count=max_nodes,
    ).is_tradeable:
        return repaired

    return _tradeable_registry_fallback(
        rng,
        genome_id=genome_id,
        generation=generation,
    )


def _draw_tradeable_random_genome(
    rng: random.Random,
    *,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
    ohlcv: pd.DataFrame,
    min_signals: int,
    repair_max_attempts: int,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    kind_weights: dict[str, float] | None = None,
    n_latents: int = 0,
) -> Genome:
    weights = kind_weights or {}
    redraw_cap = repair_max_attempts + 1
    for _ in range(redraw_cap):
        genome = build_random_genome(
            rng,
            genome_id=genome_id,
            generation=generation,
            max_nodes=max_nodes,
            max_depth=max_depth,
            indicator_kinds=indicator_kinds,
            kind_weights=weights,
            n_latents=n_latents,
        )
        genome = _ensure_tradeable_genome(
            rng,
            genome,
            ohlcv=ohlcv,
            min_signals=min_signals,
            max_nodes=max_nodes,
            max_depth=max_depth,
            repair_max_attempts=repair_max_attempts,
            genome_id=genome_id,
            generation=generation,
        )
        if genome_signal_activity(
            genome,
            ohlcv,
            min_signals=min_signals,
            max_depth=max_depth,
            max_node_count=max_nodes,
        ).is_tradeable:
            return genome
    return _tradeable_registry_fallback(
        rng,
        genome_id=genome_id,
        generation=generation,
    )


def _seed_exit_policy_variants(
    rng: random.Random,
    population: list[Genome],
    *,
    seed_exit_policies: bool,
    exit_policy_preset_ids: list[str] | None,
    exit_policy_seed_fraction: float,
) -> None:
    if not seed_exit_policies or exit_policy_seed_fraction <= 0:
        return
    presets = selected_exit_policy_presets(exit_policy_preset_ids)
    if not presets:
        return
    seed_count = max(1, int(round(len(population) * exit_policy_seed_fraction)))
    seed_count = min(seed_count, len(population))
    indices = rng.sample(range(len(population)), seed_count)
    for index in indices:
        preset = presets[index % len(presets)]
        population[index] = attach_exit_rule_policy(
            clone_genome(population[index]),
            preset,
        )


def build_initial_population(
    rng: random.Random,
    *,
    population_size: int,
    max_nodes: int,
    max_depth: int,
    ohlcv: pd.DataFrame | None = None,
    min_seed_signals: int = 0,
    repair_max_attempts: int = 8,
    seed_exit_policies: bool = False,
    exit_policy_preset_ids: list[str] | None = None,
    exit_policy_seed_fraction: float = 0.25,
    indicator_kinds: Sequence[str] = INDICATOR_KINDS,
    n_latents: int = 0,
    kind_weights: dict[str, float] | None = None,
    seed_genomes: list[Genome] | None = None,
) -> list[Genome]:
    weights = kind_weights or {}
    population: list[Genome] = []
    
    # Hypothesis templates seeding
    seed_genomes = seed_genomes or []
    seed_count = population_size // 2
    
    # 1. Inject up to seed_count hypothesis seed genomes first
    num_hyp_seeds = min(len(seed_genomes), seed_count)
    probe_enabled = ohlcv is not None and len(ohlcv) > 0 and min_seed_signals > 0
    
    for index in range(num_hyp_seeds):
        genome = clone_genome(
            seed_genomes[index],
            genome_id=f"gen0-seed-hyp-{index}",
            generation=0,
        )
        try:
            genome = _light_mutate_params(
                rng,
                genome,
                max_nodes=max_nodes,
                max_depth=max_depth,
                n_latents=n_latents,
            )
        except GenomeValidationError:
            pass
        
        if probe_enabled:
            genome = _ensure_tradeable_genome(
                rng,
                genome,
                ohlcv=ohlcv,
                min_signals=min_seed_signals,
                max_nodes=max_nodes,
                max_depth=max_depth,
                repair_max_attempts=repair_max_attempts,
                genome_id=f"gen0-seed-hyp-{index}",
                generation=0,
            )
        population.append(genome)

    # 2. Fill the remaining seed count using registry fixtures
    remaining_seeds = seed_count - len(population)
    random_count = population_size - seed_count
    
    registry_items = list(REGISTRY_GENOME_FIXTURES.items())
    registry_names = tuple(name for name, _ in registry_items)

    for index in range(remaining_seeds):
        if weights:
            registry_name_weights = {
                name: weights.get(_registry_primary_kind(fixture), 1.0)
                for name, fixture in registry_items
            }
            chosen_name = _choose_seeding(rng, registry_names, registry_name_weights)
        else:
            chosen_name = rng.choice(registry_names)
        template = Genome.model_validate(
            copy.deepcopy(REGISTRY_GENOME_FIXTURES[chosen_name])
        )
        template = clone_genome(
            template,
            genome_id=f"gen0-seed-{index}",
            generation=0,
        )
        try:
            genome = _light_mutate_params(
                rng,
                template,
                max_nodes=max_nodes,
                max_depth=max_depth,
                n_latents=n_latents,
            )
        except GenomeValidationError:
            genome = clone_genome(template)
        if probe_enabled:
            genome = _ensure_tradeable_genome(
                rng,
                genome,
                ohlcv=ohlcv,
                min_signals=min_seed_signals,
                max_nodes=max_nodes,
                max_depth=max_depth,
                repair_max_attempts=repair_max_attempts,
                genome_id=f"gen0-seed-{index}",
                generation=0,
            )
        population.append(genome)

    for index in range(random_count):
        genome_id = f"gen0-rand-{index}"
        if probe_enabled:
            genome = _draw_tradeable_random_genome(
                rng,
                genome_id=genome_id,
                generation=0,
                max_nodes=max_nodes,
                max_depth=max_depth,
                ohlcv=ohlcv,
                min_signals=min_seed_signals,
                repair_max_attempts=repair_max_attempts,
                indicator_kinds=indicator_kinds,
                kind_weights=weights,
                n_latents=n_latents,
            )
        else:
            genome = _draw_valid(
                rng,
                builder=lambda: build_random_genome(
                    rng,
                    genome_id=genome_id,
                    generation=0,
                    max_nodes=max_nodes,
                    max_depth=max_depth,
                    indicator_kinds=indicator_kinds,
                    kind_weights=weights,
                    n_latents=n_latents,
                ),
            )
        population.append(genome)

    _seed_exit_policy_variants(
        rng,
        population,
        seed_exit_policies=seed_exit_policies,
        exit_policy_preset_ids=exit_policy_preset_ids,
        exit_policy_seed_fraction=exit_policy_seed_fraction,
    )
    return population


def _draw_valid(rng: random.Random, builder: Any, *, max_attempts: int = 64) -> Genome:
    last_error: Exception | None = None
    for _ in range(max_attempts):
        try:
            return builder()
        except GenomeValidationError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise GenomeValidationError("failed to draw a valid genome")


def draw_valid_child(
    rng: random.Random,
    builder: Any,
    *,
    max_attempts: int = 64,
) -> Genome:
    return _draw_valid(rng, builder, max_attempts=max_attempts)
