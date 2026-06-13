"""Compile validated genomes into topologically sorted execution plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from q_backend.backtesting.genome.node_specs import NODE_SPECS, parse_input_ref
from q_backend.backtesting.genome.schema import Genome, GenomeNode
from q_backend.backtesting.genome.validate import (
    GenomeValidationError,
    resolve_node_params,
    validate_genome,
)

ExitPolicyKind = Literal["bool", "fixed_holding", "middle_band", "rebalance", "opposite"]


@dataclass
class InputBinding:
    node_id: str
    port: str
    column: str


@dataclass
class CompiledNode:
    node: GenomeNode
    resolved_params: dict[str, Any]
    column_by_port: dict[str, str]
    input_bindings: list[InputBinding]
    cache_key: str


@dataclass
class ExecutionPlan:
    genome: Genome
    trial_params: dict[str, Any]
    sorted_nodes: list[CompiledNode]
    entry_long_column: str
    entry_short_column: str
    exit_long_policy: ExitPolicyKind
    exit_short_policy: ExitPolicyKind
    exit_long_column: str | None
    exit_short_column: str | None
    exit_long_middle_columns: tuple[str, str] | None = None
    fixed_holding_period: int | None = None


def _column_name(node_id: str, port: str) -> str:
    if port == "out":
        return f"g_{node_id}"
    return f"g_{node_id}__{port}"


def _cache_key(kind: str, resolved_params: dict[str, Any], input_keys: tuple[str, ...]) -> str:
    param_items = tuple(sorted(resolved_params.items()))
    return f"{kind}|{param_items}|{input_keys}"


def compile_genome(
    genome: Genome | dict[str, Any],
    trial_params: dict[str, Any] | None = None,
    *,
    max_depth: int = 12,
    max_node_count: int = 24,
) -> ExecutionPlan:
    if isinstance(genome, dict):
        genome = Genome.model_validate(genome)

    validate_genome(genome, max_depth=max_depth, max_node_count=max_node_count)
    params = dict(trial_params or {})

    nodes_by_id = {node.id: node for node in genome.nodes}
    compiled_by_id: dict[str, CompiledNode] = {}

    def compile_node(node_id: str) -> CompiledNode:
        if node_id in compiled_by_id:
            return compiled_by_id[node_id]

        node = nodes_by_id[node_id]
        bindings: list[InputBinding] = []
        input_cache_keys: list[str] = []

        for raw_input in node.inputs:
            parent_id, port = parse_input_ref(raw_input)
            parent = compile_node(parent_id)
            column = parent.column_by_port[port]
            bindings.append(InputBinding(node_id=parent_id, port=port, column=column))
            input_cache_keys.append(parent.cache_key)

        resolved = resolve_node_params(node, params)
        spec = NODE_SPECS[node.kind]
        column_by_port = {port: _column_name(node.id, port) for port in spec.output_ports}
        cache_key = _cache_key(node.kind, resolved, tuple(input_cache_keys))

        compiled = CompiledNode(
            node=node,
            resolved_params=resolved,
            column_by_port=column_by_port,
            input_bindings=bindings,
            cache_key=cache_key,
        )
        compiled_by_id[node_id] = compiled
        return compiled

    for node in genome.nodes:
        compile_node(node.id)

    sorted_nodes = _topological_sort(genome.nodes, nodes_by_id)

    entry_long = _resolve_signal_column(compiled_by_id, genome.entry_long.ref)
    entry_short = _resolve_signal_column(compiled_by_id, genome.entry_short.ref)
    exit_long_node_id, _ = parse_input_ref(genome.exit_long.ref)
    exit_short_node_id, _ = parse_input_ref(genome.exit_short.ref)
    exit_long_node = compiled_by_id[exit_long_node_id]
    exit_short_node = compiled_by_id[exit_short_node_id]

    exit_long_policy, exit_long_column, exit_long_middle = _resolve_exit(
        exit_long_node, entry_long, entry_short, genome.exit_long.ref, side="long"
    )
    exit_short_policy, exit_short_column, exit_short_middle = _resolve_exit(
        exit_short_node, entry_long, entry_short, genome.exit_short.ref, side="short"
    )

    holding_period: int | None = None
    if exit_long_node.node.kind == "exit.fixed_holding":
        holding_period = int(exit_long_node.resolved_params["holding_period"])
    elif exit_short_node.node.kind == "exit.fixed_holding":
        holding_period = int(exit_short_node.resolved_params["holding_period"])

    return ExecutionPlan(
        genome=genome,
        trial_params=params,
        sorted_nodes=[compiled_by_id[n.id] for n in sorted_nodes],
        entry_long_column=entry_long,
        entry_short_column=entry_short,
        exit_long_policy=exit_long_policy,
        exit_short_policy=exit_short_policy,
        exit_long_column=exit_long_column,
        exit_short_column=exit_short_column,
        exit_long_middle_columns=exit_long_middle,
        fixed_holding_period=holding_period,
    )


def _topological_sort(
    nodes: list[GenomeNode],
    nodes_by_id: dict[str, GenomeNode],
) -> list[GenomeNode]:
    indegree = {node.id: 0 for node in nodes}
    adjacency: dict[str, list[str]] = {node.id: [] for node in nodes}

    for node in nodes:
        for raw_input in node.inputs:
            parent_id, _ = parse_input_ref(raw_input)
            adjacency[parent_id].append(node.id)
            indegree[node.id] += 1

    queue = [node for node in nodes if indegree[node.id] == 0]
    ordered: list[GenomeNode] = []
    while queue:
        current = queue.pop(0)
        ordered.append(nodes_by_id[current.id])
        for child_id in adjacency[current.id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                queue.append(nodes_by_id[child_id])

    if len(ordered) != len(nodes):
        raise GenomeValidationError("Failed to topologically sort genome DAG.")
    return ordered


def _resolve_signal_column(
    compiled_by_id: dict[str, CompiledNode],
    ref: str,
) -> str:
    node_id, port = parse_input_ref(ref)
    return compiled_by_id[node_id].column_by_port[port]


def _resolve_exit(
    exit_node: CompiledNode,
    entry_long_col: str,
    entry_short_col: str,
    ref: str,
    *,
    side: Literal["long", "short"],
) -> tuple[ExitPolicyKind, str | None, tuple[str, str] | None]:
    kind = exit_node.node.kind
    if kind == "exit.fixed_holding":
        return "fixed_holding", None, None
    if kind == "exit.rebalance":
        return "rebalance", None, None
    if kind == "exit.opposite_signal":
        if side == "long":
            return "bool", entry_short_col, None
        return "bool", entry_long_col, None
    if kind == "exit.middle_band":
        return (
            "middle_band",
            None,
            (
                exit_node.column_by_port["exit_long"],
                exit_node.column_by_port["exit_short"],
            ),
        )
    _, port = parse_input_ref(ref)
    return "bool", exit_node.column_by_port[port], None
