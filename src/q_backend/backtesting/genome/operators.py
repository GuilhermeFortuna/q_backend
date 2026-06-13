"""Genetic operators for genome DSL evolution (design §4.2, §4.4)."""

from __future__ import annotations

import copy
import random
from typing import Any

from q_backend.backtesting.genome.node_specs import (
    NODE_SPECS,
    OutputType,
    port_output_type,
)
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome, GenomeNode, NodeRef
from q_backend.backtesting.genome.validate import (
    GenomeValidationError,
    collect_genome_param_keys,
    validate_genome,
)

INDICATOR_KINDS = sorted(
    kind
    for kind in NODE_SPECS
    if kind.startswith("ind.") and kind not in {"ind.diff", "ind.ratio", "ind.tsmom"}
)
CMP_KINDS = sorted(kind for kind in NODE_SPECS if kind.startswith("cmp."))
EXIT_BOOL_KINDS = ("exit.opposite_signal",)
SOURCE_KIND = "source.close"


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


def _light_mutate_params(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
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

    if bounds.type == "categorical" and bounds.choices:
        node.params[key] = rng.choice(list(bounds.choices))
    elif bounds.type == "int" and bounds.min is not None and bounds.max is not None:
        node.params[key] = rng.randint(int(bounds.min), int(bounds.max))
    elif bounds.type == "float" and bounds.min is not None and bounds.max is not None:
        node.params[key] = round(rng.uniform(bounds.min, bounds.max), 4)
    else:
        node.params[key] = _random_param_ref(rng, key)

    return _validate_or_raise(mutated, max_nodes=max_nodes, max_depth=max_depth)


def mutate_genome(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    op = rng.choice(
        ["rewire", "swap_indicator", "nudge_param", "swap_exit", "add_node", "remove_node"]
    )
    if op == "rewire":
        return _mutate_rewire(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    if op == "swap_indicator":
        return _mutate_swap_indicator(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    if op == "nudge_param":
        return _mutate_nudge_param(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    if op == "swap_exit":
        return _mutate_swap_exit(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    if op == "add_node":
        return _mutate_add_node(rng, genome, max_nodes=max_nodes, max_depth=max_depth)
    return _mutate_remove_node(rng, genome, max_nodes=max_nodes, max_depth=max_depth)


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
) -> Genome:
    child = clone_genome(genome)
    indicators = [node for node in child.nodes if node.kind.startswith("ind.")]
    if not indicators:
        return child
    node = rng.choice(indicators)
    old_type = _primary_output_type(node)
    compatible = [
        kind
        for kind in INDICATOR_KINDS
        if _indicator_output_type(kind) == old_type
        and NODE_SPECS[kind].min_inputs == NODE_SPECS[node.kind].min_inputs
    ]
    if not compatible:
        return child
    new_kind = rng.choice(compatible)
    node.kind = new_kind
    spec = NODE_SPECS[new_kind]
    node.params = {
        key: node.params.get(key, _random_param_ref(rng, key))
        for key in sorted(spec.allowed_param_keys)
    }
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _indicator_output_type(kind: str) -> OutputType:
    spec = NODE_SPECS[kind]
    return spec.port_types[spec.output_ports[0]]


def _mutate_nudge_param(
    rng: random.Random,
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    return _light_mutate_params(rng, genome, max_nodes=max_nodes, max_depth=max_depth)


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
    kind = rng.choice(["ind.ma", "ind.ema", "ind.rsi"])
    spec = NODE_SPECS[kind]
    params = {key: _random_param_ref(rng, key) for key in sorted(spec.allowed_param_keys)}
    child.nodes.append(
        GenomeNode(id=new_id, kind=kind, params=params, inputs=[close_id])
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
    child = clone_genome(parent_a)
    cuts_a = [node for node in child.nodes if node.kind.startswith(("ind.", "cmp.", "logic."))]
    cuts_b = [node for node in parent_b.nodes if node.kind.startswith(("ind.", "cmp.", "logic."))]
    if not cuts_a or not cuts_b:
        return child

    cut_a = rng.choice(cuts_a)
    output_type = _primary_output_type(cut_a)
    compatible_b = [
        node
        for node in cuts_b
        if _primary_output_type(node) == output_type
        and NODE_SPECS[node.kind].min_inputs == NODE_SPECS[cut_a.kind].min_inputs
        and NODE_SPECS[node.kind].max_inputs == NODE_SPECS[cut_a.kind].max_inputs
    ]
    if not compatible_b:
        return child
    cut_b = rng.choice(compatible_b)

    cut_a.kind = cut_b.kind
    cut_a.params = copy.deepcopy(cut_b.params)
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def build_random_genome(
    rng: random.Random,
    *,
    genome_id: str,
    generation: int,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    close_id = "n1"
    short_id = "n2"
    long_id = "n3"
    diff_id = "n4"
    buy_id = "n5"
    sell_id = "n6"

    short_kind = rng.choice(["ind.ma", "ind.ema"])
    long_kind = rng.choice(["ind.ma", "ind.ema"])
    short_spec = NODE_SPECS[short_kind]
    long_spec = NODE_SPECS[long_kind]

    nodes = [
        GenomeNode(id=close_id, kind=SOURCE_KIND, params={}, inputs=[]),
        GenomeNode(
            id=short_id,
            kind=short_kind,
            params={
                key: _random_param_ref(rng, key) for key in sorted(short_spec.allowed_param_keys)
            },
            inputs=[close_id],
        ),
        GenomeNode(
            id=long_id,
            kind=long_kind,
            params={
                key: _random_param_ref(rng, key) for key in sorted(long_spec.allowed_param_keys)
            },
            inputs=[close_id],
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

    genome = Genome(
        version=1,
        genome_id=genome_id,
        nodes=nodes,
        entry_long=NodeRef(ref=buy_id),
        entry_short=NodeRef(ref=sell_id),
        exit_long=NodeRef(ref=sell_id),
        exit_short=NodeRef(ref=buy_id),
        metadata={"generation": generation, "origin": "random"},
    )
    return _validate_or_raise(genome, max_nodes=max_nodes, max_depth=max_depth)


def build_initial_population(
    rng: random.Random,
    *,
    population_size: int,
    max_nodes: int,
    max_depth: int,
) -> list[Genome]:
    population: list[Genome] = []
    registry_templates = list(REGISTRY_GENOME_FIXTURES.values())
    seed_count = population_size // 2
    random_count = population_size - seed_count

    for index in range(seed_count):
        template = Genome.model_validate(copy.deepcopy(rng.choice(registry_templates)))
        template = clone_genome(
            template,
            genome_id=f"gen0-seed-{index}",
            generation=0,
        )
        try:
            genome = _light_mutate_params(
                rng, template, max_nodes=max_nodes, max_depth=max_depth
            )
        except GenomeValidationError:
            genome = clone_genome(template)
        population.append(genome)

    for index in range(random_count):
        genome = _draw_valid(
            rng,
            builder=lambda: build_random_genome(
                rng,
                genome_id=f"gen0-rand-{index}",
                generation=0,
                max_nodes=max_nodes,
                max_depth=max_depth,
            ),
        )
        population.append(genome)

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
