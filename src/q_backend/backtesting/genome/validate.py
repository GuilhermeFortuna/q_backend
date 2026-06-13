"""Parse-time genome validation (design §2.4)."""

from __future__ import annotations

from collections import deque
from typing import Any

from q_backend.backtesting.genome.node_specs import (
    ALLOWED_NODE_KINDS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODE_COUNT,
    NODE_SPECS,
    OutputType,
    SeriesType,
    parse_input_ref,
    port_output_type,
)
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.schema import Genome, GenomeNode


class GenomeValidationError(ValueError):
    """Raised when a genome document fails parse-time validation."""


def _is_param_ref(value: Any) -> bool:
    return isinstance(value, dict) and "param" in value and len(value) <= 2


def _literal_param_keys(params: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in params.values():
        if _is_param_ref(value):
            keys.add(str(value["param"]))
    return keys


def _node_depth(node_id: str, children: dict[str, list[str]]) -> int:
    memo: dict[str, int] = {}

    def depth(nid: str) -> int:
        if nid in memo:
            return memo[nid]
        if not children.get(nid):
            memo[nid] = 1
            return 1
        memo[nid] = 1 + max(depth(child.split(":")[0]) for child in children[nid])
        return memo[nid]

    return depth(node_id)


def validate_genome(
    genome: Genome,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_node_count: int = DEFAULT_MAX_NODE_COUNT,
) -> None:
    if len(genome.nodes) > max_node_count:
        raise GenomeValidationError(
            f"Genome has {len(genome.nodes)} nodes; maximum is {max_node_count}."
        )

    nodes_by_id: dict[str, GenomeNode] = {}
    for node in genome.nodes:
        if node.id in nodes_by_id:
            raise GenomeValidationError(f"Duplicate node id '{node.id}'.")
        nodes_by_id[node.id] = node

    for node in genome.nodes:
        if node.kind not in ALLOWED_NODE_KINDS:
            raise GenomeValidationError(f"Unknown node kind '{node.kind}' on node '{node.id}'.")

        spec = NODE_SPECS[node.kind]
        unknown_keys = set(node.params) - spec.allowed_param_keys
        if unknown_keys:
            raise GenomeValidationError(
                f"Node '{node.id}' ({node.kind}) has unknown params: {sorted(unknown_keys)}."
            )

        for key, value in node.params.items():
            if _is_param_ref(value):
                param_key = str(value["param"])
                if param_key not in GENOME_PARAM_BOUNDS:
                    raise GenomeValidationError(
                        f"Node '{node.id}' param ref '{param_key}' is not in GENOME_PARAM_BOUNDS."
                    )
            elif key == "bars" and node.kind == "transform.shift":
                if value != 1:
                    raise GenomeValidationError(
                        f"Node '{node.id}': transform.shift.bars must be 1 (got {value!r})."
                    )

        input_count = len(node.inputs)
        if input_count < spec.min_inputs or input_count > spec.max_inputs:
            raise GenomeValidationError(
                f"Node '{node.id}' ({node.kind}) expects "
                f"{spec.min_inputs}–{spec.max_inputs} inputs, got {input_count}."
            )

        for raw_input in node.inputs:
            ref_node_id, port = parse_input_ref(raw_input)
            if ref_node_id not in nodes_by_id:
                raise GenomeValidationError(
                    f"Node '{node.id}' input '{raw_input}' references unknown node '{ref_node_id}'."
                )
            ref_kind = nodes_by_id[ref_node_id].kind
            try:
                ref_type = port_output_type(ref_kind, port)
            except KeyError as exc:
                raise GenomeValidationError(
                    f"Node '{node.id}' input '{raw_input}' references invalid port "
                    f"'{port}' on node '{ref_node_id}' ({ref_kind})."
                ) from exc

            if node.kind.startswith("logic."):
                if ref_type != "bool_series":
                    raise GenomeValidationError(
                        f"Node '{node.id}' ({node.kind}) requires bool_series inputs; "
                        f"'{raw_input}' is {ref_type}."
                    )
            elif node.kind.startswith("cmp."):
                if ref_type == "bool_series":
                    raise GenomeValidationError(
                        f"Node '{node.id}' ({node.kind}) cannot compare bool_series input "
                        f"'{raw_input}'."
                    )
            elif node.kind == "exit.middle_band":
                if ref_type != "price_series":
                    raise GenomeValidationError(
                        f"Node '{node.id}' (exit.middle_band) requires price_series inputs."
                    )

            if spec.input_series_types is not None:
                idx = node.inputs.index(raw_input)
                expected = spec.input_series_types[idx]
                if ref_type not in (expected, "price_series", "oscillator"):
                    if ref_type != expected:
                        raise GenomeValidationError(
                            f"Node '{node.id}' input '{raw_input}' type {ref_type} "
                            f"does not match expected {expected}."
                        )

    _assert_acyclic(nodes_by_id)

    children: dict[str, list[str]] = {node.id: list(node.inputs) for node in genome.nodes}
    for node in genome.nodes:
        depth = _node_depth(node.id, children)
        if depth > max_depth:
            raise GenomeValidationError(
                f"Node '{node.id}' exceeds max depth {max_depth} (depth={depth})."
            )

    _validate_signal_refs(genome, nodes_by_id)


def _assert_acyclic(nodes_by_id: dict[str, GenomeNode]) -> None:
    indegree: dict[str, int] = {node_id: 0 for node_id in nodes_by_id}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes_by_id}

    for node in nodes_by_id.values():
        for raw_input in node.inputs:
            parent_id, _ = parse_input_ref(raw_input)
            adjacency[parent_id].append(node.id)
            indegree[node.id] += 1

    queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in adjacency[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if visited != len(nodes_by_id):
        raise GenomeValidationError("Genome DAG contains a cycle.")


def _validate_signal_refs(genome: Genome, nodes_by_id: dict[str, GenomeNode]) -> None:
    for label, ref in (
        ("entry_long", genome.entry_long),
        ("entry_short", genome.entry_short),
    ):
        _require_bool_ref(label, ref.ref, nodes_by_id)

    for label, ref in (
        ("exit_long", genome.exit_long),
        ("exit_short", genome.exit_short),
    ):
        node_id, port = parse_input_ref(ref.ref)
        node = nodes_by_id.get(node_id)
        if node is None:
            raise GenomeValidationError(f"{label} references unknown node '{ref.ref}'.")
        if node.kind.startswith("exit."):
            continue
        try:
            out_type = port_output_type(node.kind, port)
        except KeyError as exc:
            raise GenomeValidationError(
                f"{label} references invalid port on node '{ref.ref}'."
            ) from exc
        if out_type != "bool_series":
            raise GenomeValidationError(
                f"{label} must reference a bool_series or exit_policy node; "
                f"'{ref.ref}' is {out_type}."
            )


def _node_primary_output_type(node: GenomeNode) -> OutputType:
    spec = NODE_SPECS[node.kind]
    if node.kind == "exit.middle_band":
        return "exit_policy"
    if len(spec.output_ports) == 1:
        return spec.port_types[spec.output_ports[0]]
    if node.kind == "ind.tsmom":
        return "bool_series"
    return spec.port_types[spec.output_ports[0]]


def _require_bool_ref(label: str, ref_id: str, nodes_by_id: dict[str, GenomeNode]) -> None:
    node_id, port = parse_input_ref(ref_id)
    node = nodes_by_id.get(node_id)
    if node is None:
        raise GenomeValidationError(f"{label} references unknown node '{ref_id}'.")
    try:
        out_type = port_output_type(node.kind, port)
    except KeyError as exc:
        raise GenomeValidationError(
            f"{label} references invalid port on node '{ref_id}'."
        ) from exc
    if out_type != "bool_series":
        raise GenomeValidationError(
            f"{label} must reference a bool_series node; '{ref_id}' ({node.kind}) "
            f"port '{port}' is {out_type}."
        )


def resolve_node_params(
    node: GenomeNode,
    trial_params: dict[str, Any],
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    if node.kind == "transform.shift" and "bars" not in node.params:
        resolved["bars"] = 1
    for key, value in node.params.items():
        if _is_param_ref(value):
            param_key = str(value["param"])
            if param_key in trial_params:
                resolved[key] = trial_params[param_key]
            else:
                resolved[key] = GENOME_PARAM_BOUNDS[param_key].default
            if value.get("negate") and isinstance(resolved[key], (int, float)):
                resolved[key] = -resolved[key]
        else:
            resolved[key] = value
    return resolved


def collect_genome_param_keys(genome: Genome) -> set[str]:
    keys: set[str] = set()
    for node in genome.nodes:
        keys.update(_literal_param_keys(node.params))
    return keys
