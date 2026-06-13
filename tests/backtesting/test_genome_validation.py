"""Genome DSL validation tests."""

import pytest

from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import GenomeValidationError, validate_genome
from q_backend.backtesting.genome.registry_fixtures import MA_CROSSOVER_GENOME


def _genome(data: dict) -> Genome:
    return Genome.model_validate(data)


def test_ma_crossover_genome_validates():
    validate_genome(_genome(MA_CROSSOVER_GENOME))


def test_rejects_cycle():
    genome = _genome(
        {
            "version": 1,
            "genome_id": "bad-cycle",
            "nodes": [
                {"id": "a", "kind": "source.close", "params": {}, "inputs": []},
                {"id": "b", "kind": "transform.abs", "params": {}, "inputs": ["c"]},
                {"id": "c", "kind": "transform.abs", "params": {}, "inputs": ["b"]},
            ],
            "entry_long": {"ref": "a"},
            "entry_short": {"ref": "a"},
            "exit_long": {"ref": "a"},
            "exit_short": {"ref": "a"},
        }
    )
    with pytest.raises(GenomeValidationError, match="cycle"):
        validate_genome(genome)


def test_rejects_bool_comparison():
    genome = _genome(
        {
            "version": 1,
            "genome_id": "bad-bool-cmp",
            "nodes": [
                {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
                {
                    "id": "n2",
                    "kind": "ind.diff",
                    "params": {},
                    "inputs": ["n1", "n1"],
                },
                {
                    "id": "n3",
                    "kind": "cmp.cross_above",
                    "params": {"threshold": 0.0},
                    "inputs": ["n2"],
                },
                {
                    "id": "n4",
                    "kind": "cmp.cross_below",
                    "params": {"threshold": 0.0},
                    "inputs": ["n2"],
                },
                {
                    "id": "n5",
                    "kind": "cmp.gt",
                    "params": {},
                    "inputs": ["n3", "n4"],
                },
            ],
            "entry_long": {"ref": "n3"},
            "entry_short": {"ref": "n4"},
            "exit_long": {"ref": "n4"},
            "exit_short": {"ref": "n3"},
        }
    )
    with pytest.raises(GenomeValidationError, match="cannot compare bool_series"):
        validate_genome(genome)


def test_rejects_unknown_kind():
    genome = _genome(
        {
            "version": 1,
            "genome_id": "bad-kind",
            "nodes": [{"id": "n1", "kind": "ind.unknown", "params": {}, "inputs": []}],
            "entry_long": {"ref": "n1"},
            "entry_short": {"ref": "n1"},
            "exit_long": {"ref": "n1"},
            "exit_short": {"ref": "n1"},
        }
    )
    with pytest.raises(GenomeValidationError, match="Unknown node kind"):
        validate_genome(genome)


def test_rejects_unknown_param_key():
    genome = _genome(
        {
            "version": 1,
            "genome_id": "bad-param",
            "nodes": [
                {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
                {
                    "id": "n2",
                    "kind": "ind.ma",
                    "params": {"period": {"param": "not_a_real_param"}},
                    "inputs": ["n1"],
                },
            ],
            "entry_long": {"ref": "n1"},
            "entry_short": {"ref": "n1"},
            "exit_long": {"ref": "n1"},
            "exit_short": {"ref": "n1"},
        }
    )
    with pytest.raises(GenomeValidationError, match="GENOME_PARAM_BOUNDS"):
        validate_genome(genome)


def test_rejects_shift_bars_not_one():
    genome = _genome(
        {
            "version": 1,
            "genome_id": "bad-shift",
            "nodes": [
                {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
                {
                    "id": "n2",
                    "kind": "transform.shift",
                    "params": {"bars": 2},
                    "inputs": ["n1"],
                },
            ],
            "entry_long": {"ref": "n1"},
            "entry_short": {"ref": "n1"},
            "exit_long": {"ref": "n1"},
            "exit_short": {"ref": "n1"},
        }
    )
    with pytest.raises(GenomeValidationError, match="transform.shift.bars must be 1"):
        validate_genome(genome)


def test_rejects_node_count_cap():
    nodes = [
        {"id": f"n{i}", "kind": "source.close", "params": {}, "inputs": []}
        for i in range(25)
    ]
    genome = _genome(
        {
            "version": 1,
            "genome_id": "too-many",
            "nodes": nodes,
            "entry_long": {"ref": "n0"},
            "entry_short": {"ref": "n0"},
            "exit_long": {"ref": "n0"},
            "exit_short": {"ref": "n0"},
        }
    )
    with pytest.raises(GenomeValidationError, match="maximum is 24"):
        validate_genome(genome)


def test_rejects_depth_cap():
    nodes = [{"id": "n0", "kind": "source.close", "params": {}, "inputs": []}]
    for i in range(1, 14):
        nodes.append(
            {
                "id": f"n{i}",
                "kind": "transform.abs",
                "params": {},
                "inputs": [f"n{i - 1}"],
            }
        )
    genome = _genome(
        {
            "version": 1,
            "genome_id": "too-deep",
            "nodes": nodes,
            "entry_long": {"ref": "n13"},
            "entry_short": {"ref": "n13"},
            "exit_long": {"ref": "n13"},
            "exit_short": {"ref": "n13"},
        }
    )
    with pytest.raises(GenomeValidationError, match="max depth"):
        validate_genome(genome)
