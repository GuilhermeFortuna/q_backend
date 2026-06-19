"""Cheap in-sample signal activity probes for genome trade-viability (WO53)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import pandas as pd

from q_backend.backtesting.genome.compile import _column_name
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.node_specs import NODE_SPECS, parse_input_ref
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.schema import Genome, GenomeNode
from q_backend.backtesting.genome.validate import (
    GenomeValidationError,
    collect_genome_param_keys,
    validate_genome,
)

_PERIOD_PARAM_KEYS = (
    "period",
    "lookback_bars",
    "lookback_1",
    "lookback_2",
    "lookback_3",
    "fast_period",
    "slow_period",
    "vol_window",
)


@dataclass(frozen=True)
class ActivityStats:
    n_entries: int
    n_exits: int
    min_signals: int

    @property
    def is_tradeable(self) -> bool:
        return self.n_entries >= self.min_signals


def default_trial_params(genome: Genome) -> dict[str, Any]:
    return {
        key: GENOME_PARAM_BOUNDS[key].default
        for key in sorted(collect_genome_param_keys(genome))
    }


def _count_rising_edges(series: pd.Series) -> int:
    values = series.fillna(False).astype(int)
    if len(values) < 2:
        return int(values.sum())
    return int((values.diff().fillna(0) > 0).sum())


def genome_signal_activity(
    genome: Genome,
    df: pd.DataFrame,
    *,
    min_signals: int = 1,
    trial_params: dict[str, Any] | None = None,
    max_depth: int = 12,
    max_node_count: int = 24,
) -> ActivityStats:
    """Compile a genome on one OHLCV slice and count entry/exit signal crossings."""
    if df is None or len(df) == 0:
        return ActivityStats(n_entries=0, n_exits=0, min_signals=min_signals)

    params = dict(trial_params or default_trial_params(genome))
    strategy = CompositeStrategy(genome=genome, params=params)
    indicators = strategy.compute_indicators(df.copy())
    plan = strategy.plan

    n_entries = _count_rising_edges(indicators["entry_long_signal"]) + _count_rising_edges(
        indicators["entry_short_signal"]
    )
    n_exits = 0
    if "exit_long_signal" in indicators.columns:
        n_exits += _count_rising_edges(indicators["exit_long_signal"])
    if "exit_short_signal" in indicators.columns:
        n_exits += _count_rising_edges(indicators["exit_short_signal"])

    if n_exits == 0 and plan.exit_long_policy == "bool":
        exit_col = plan.exit_long_column or plan.exit_short_column
        if exit_col and exit_col in indicators.columns:
            n_exits = _count_rising_edges(indicators[exit_col])

    return ActivityStats(
        n_entries=n_entries,
        n_exits=n_exits,
        min_signals=min_signals,
    )


def _clone_genome(genome: Genome) -> Genome:
    return Genome.model_validate(genome.model_dump())


def _validate_or_raise(
    genome: Genome,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    validate_genome(genome, max_depth=max_depth, max_node_count=max_nodes)
    return genome


def _parent_series(
    genome: Genome,
    df: pd.DataFrame,
    cmp_node: GenomeNode,
    *,
    trial_params: dict[str, Any],
) -> pd.Series | None:
    if not cmp_node.inputs:
        return None
    parent_id, port = parse_input_ref(cmp_node.inputs[0])
    column = _column_name(parent_id, port)
    strategy = CompositeStrategy(genome=genome, params=trial_params)
    indicators = strategy.compute_indicators(df.copy())
    if column not in indicators.columns:
        return None
    series = indicators[column].dropna()
    if series.empty:
        return None
    return series


def _repair_recenter_thresholds(
    rng: random.Random,
    genome: Genome,
    df: pd.DataFrame,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    child = _clone_genome(genome)
    trial_params = default_trial_params(child)
    nodes_by_id = {node.id: node for node in child.nodes}
    changed = False

    for node in child.nodes:
        if node.kind not in {"cmp.cross_above", "cmp.cross_below"}:
            continue
        if "threshold" not in node.params:
            continue
        series = _parent_series(child, df, node, trial_params=trial_params)
        if series is None:
            continue
        parent_id, port = parse_input_ref(node.inputs[0])
        parent_kind = nodes_by_id[parent_id].kind
        parent_spec = NODE_SPECS[parent_kind]
        output_type = parent_spec.port_types.get(port, parent_spec.port_types["out"])

        low = float(series.quantile(0.25))
        high = float(series.quantile(0.75))
        if output_type == "oscillator":
            low = max(0.0, low)
            high = min(100.0, high)
        if low == high:
            target = float(series.median())
        else:
            target = rng.uniform(low, high)

        if node.kind == "cmp.cross_below":
            node.params["threshold"] = target
        else:
            node.params["threshold"] = target
        changed = True

    if not changed:
        return child
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _repair_shorten_lookbacks(
    rng: random.Random,
    genome: Genome,
    df: pd.DataFrame,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    del rng, df
    child = _clone_genome(genome)
    changed = False

    for node in child.nodes:
        if not node.kind.startswith("ind."):
            continue
        for key in _PERIOD_PARAM_KEYS:
            if key not in node.params:
                continue
            value = node.params[key]
            if isinstance(value, dict) and "param" in value:
                bounds = GENOME_PARAM_BOUNDS.get(str(value["param"]))
                current = bounds.default if bounds is not None else 10
            elif isinstance(value, int):
                current = value
            else:
                continue
            shortened = max(2, int(current * 0.6))
            node.params[key] = shortened
            changed = True

    if not changed:
        return child
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


def _repair_relax_logic_and(
    rng: random.Random,
    genome: Genome,
    df: pd.DataFrame,
    *,
    max_nodes: int,
    max_depth: int,
) -> Genome:
    del df
    child = _clone_genome(genome)
    nodes_by_id = {node.id: node for node in child.nodes}
    changed = False

    for attr in ("entry_long", "entry_short"):
        ref = getattr(child, attr).ref
        node_id, _ = parse_input_ref(ref)
        node = nodes_by_id.get(node_id)
        if node is None or node.kind != "logic.and":
            continue
        candidates = [parse_input_ref(raw)[0] for raw in node.inputs]
        replacement = rng.choice(candidates)
        setattr(child, attr, type(getattr(child, attr))(ref=replacement))
        changed = True

    if not changed:
        return child
    return _validate_or_raise(child, max_nodes=max_nodes, max_depth=max_depth)


_REPAIR_OPERATIONS = (
    _repair_recenter_thresholds,
    _repair_shorten_lookbacks,
    _repair_relax_logic_and,
)


def repair_genome(
    rng: random.Random,
    genome: Genome,
    df: pd.DataFrame,
    *,
    min_signals: int,
    max_nodes: int,
    max_depth: int,
    max_attempts: int = 8,
) -> Genome:
    """Try viability repairs before giving up on a dead genome."""
    current = _clone_genome(genome)
    if genome_signal_activity(
        current,
        df,
        min_signals=min_signals,
        max_depth=max_depth,
        max_node_count=max_nodes,
    ).is_tradeable:
        return current

    for attempt in range(max_attempts):
        operation = _REPAIR_OPERATIONS[attempt % len(_REPAIR_OPERATIONS)]
        try:
            candidate = operation(
                rng,
                current,
                df,
                max_nodes=max_nodes,
                max_depth=max_depth,
            )
            if genome_signal_activity(
                candidate,
                df,
                min_signals=min_signals,
                max_depth=max_depth,
                max_node_count=max_nodes,
            ).is_tradeable:
                return candidate
            current = candidate
        except GenomeValidationError:
            continue

    return current


def population_tradeable_fraction(
    population: list[Genome],
    df: pd.DataFrame,
    *,
    min_signals: int,
    max_depth: int = 12,
    max_nodes: int = 24,
) -> float:
    if not population:
        return 0.0
    tradeable = sum(
        1
        for genome in population
        if genome_signal_activity(
            genome,
            df,
            min_signals=min_signals,
            max_depth=max_depth,
            max_node_count=max_nodes,
        ).is_tradeable
    )
    return tradeable / len(population)
