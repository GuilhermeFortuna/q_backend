"""Prefix-causality invariant over every genome node kind (WO181 Task 4).

The pre-existing prefix-causality tests covered only ``feature.*`` (22 of the 74 node
kinds) and the four WO158 transforms. Everything else — indicator primitives, comparison
and logic operators, sources, the ``exit.middle_band`` policy — silently fell outside any
causality invariant. This module enumerates **all** ``NODE_SPECS`` kinds and asserts that,
for every kind, each output port's value at bar ``t`` is identical whether it is computed
on the full frame or on the ``[:t+1]`` prefix (i.e. removing future bars cannot change a
present value). The check reuses ``assert_causal`` — the same engine the feature-spec
invariant uses — by wrapping ``CompositeStrategy.compute_indicators`` per node/port.

Kinds that cannot be exercised on bare OHLCV (``ind.latent`` needs a neural model; the
``source.exog.*`` family needs pre-aligned exogenous columns; the ``exit.*`` policy nodes
emit no data-derived series) are skipped **only** via the reviewed
``leakage_exemptions.EXEMPT_NODE_KINDS`` dict; a companion hygiene test fails if that dict
drifts or if any kind is neither tested nor exempted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from leakage_exemptions import EXEMPT_NODE_KINDS

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.features.compute import FeatureSeries
from q_backend.features.leakage import LeakageError, assert_causal

ALL_NODE_KINDS = tuple(sorted(NODE_SPECS))
TESTED_NODE_KINDS = tuple(k for k in ALL_NODE_KINDS if k not in EXEMPT_NODE_KINDS)

_TIME_PARAMS = {
    "session_open": "09:00",
    "session_close": "18:00",
    "window_from": "10:00",
    "window_to": "12:00",
}


def _default_params(kind: str) -> dict:
    """A valid parameter set for ``kind`` drawn from GENOME_PARAM_BOUNDS defaults."""
    params: dict = {}
    for key in sorted(NODE_SPECS[kind].allowed_param_keys):
        if key == "bars":
            continue  # transform.shift.bars must be 1; resolve_node_params defaults it.
        if key in _TIME_PARAMS:
            params[key] = _TIME_PARAMS[key]
        elif key == "clip_low":
            params[key] = -2.0
        elif key == "clip_high":
            params[key] = 2.0
        elif key == "negate":
            params[key] = False
        elif key == "estimator":
            params[key] = "close_to_close"
        elif key in GENOME_PARAM_BOUNDS:
            params[key] = GENOME_PARAM_BOUNDS[key].default
        else:  # pragma: no cover - guards against an unhandled new param key
            raise KeyError(f"No test default for param '{key}' (node '{kind}').")
    return params


def _node_under_test_genome(kind: str) -> dict:
    """A minimal valid genome that wires ``kind`` to appropriate inputs.

    A constant-false ``cmp.lt(src, src)`` ("never") supplies all four entry/exit refs so
    the genome validates regardless of the node-under-test's output type; the node is still
    evaluated (``compute_indicators`` materializes every plan node as a ``g_<id>`` column).
    """
    spec = NODE_SPECS[kind]
    nodes = [
        {"id": "src", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "srch", "kind": "source.high", "params": {}, "inputs": []},
        {"id": "srcl", "kind": "source.low", "params": {}, "inputs": []},
        {"id": "never", "kind": "cmp.lt", "params": {}, "inputs": ["src", "src"]},
    ]
    n_inputs = spec.min_inputs
    if kind.startswith("logic."):  # logic gates require bool_series inputs
        nodes.append({"id": "b1", "kind": "cmp.gt", "params": {}, "inputs": ["src", "srch"]})
        nodes.append({"id": "b2", "kind": "cmp.lt", "params": {}, "inputs": ["src", "srch"]})
        inputs = ["b1", "b2"][:n_inputs]
    else:  # numeric (price/oscillator) inputs; cmp.* reject bool inputs
        inputs = ["src", "srch", "srcl"][:n_inputs]
    nodes.append({"id": "nut", "kind": kind, "params": _default_params(kind), "inputs": inputs})
    return {
        "version": 1,
        "genome_id": f"node-causality-{kind}",
        "nodes": nodes,
        "entry_long": {"ref": "never"},
        "entry_short": {"ref": "never"},
        "exit_long": {"ref": "never"},
        "exit_short": {"ref": "never"},
    }


def _node_output_columns(kind: str) -> list[str]:
    return ["g_nut" if port == "out" else f"g_nut__{port}" for port in NODE_SPECS[kind].output_ports]


def _synthetic_frame(periods: int = 120) -> pd.DataFrame:
    """Deterministic intraday OHLCV indexed by datetime (session features need it)."""
    index = pd.date_range("2024-01-02 00:00", periods=periods, freq="h")
    close = 100.0 + np.cumsum(np.sin(np.arange(periods) / 3.0))
    open_ = close - 0.2
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 0.5,
            "low": np.minimum(open_, close) - 0.5,
            "close": close,
            "volume": np.full(periods, 1000.0),
        },
        index=index,
    )


def _node_compute_fn(kind: str, column: str):
    def _fn(bars: pd.DataFrame) -> FeatureSeries:
        strategy = CompositeStrategy(genome=_node_under_test_genome(kind), params={}, symbol="TEST")
        result = strategy.compute_indicators(bars)
        return FeatureSeries(
            feature_id=f"{kind}:{column}",
            series=result[column].reset_index(drop=True),
            warmup_bars=0,
            leakage_status="clean",
        )

    return _fn


@pytest.mark.parametrize("kind", ALL_NODE_KINDS)
def test_node_kind_is_causal(kind: str) -> None:
    if kind in EXEMPT_NODE_KINDS:
        pytest.skip(f"exempt: {EXEMPT_NODE_KINDS[kind]}")
    frame = _synthetic_frame()
    sample = [30, 60, 90, len(frame) - 1]
    for column in _node_output_columns(kind):
        assert_causal(_node_compute_fn(kind, column), frame, sample_indices=sample)


def test_node_exemptions_are_not_stale() -> None:
    """Every exempted node kind must still exist — a renamed/removed kind must fail here."""
    unknown = sorted(set(EXEMPT_NODE_KINDS) - set(NODE_SPECS))
    assert not unknown, f"EXEMPT_NODE_KINDS names non-existent node kinds: {unknown}"


def test_every_node_kind_is_tested_or_exempt() -> None:
    """No node kind may silently escape the invariant (drift guard for new kinds)."""
    covered = set(TESTED_NODE_KINDS) | set(EXEMPT_NODE_KINDS)
    missing = sorted(set(NODE_SPECS) - covered)
    assert not missing, (
        f"Node kinds neither tested nor exempted: {missing}. Add a prefix-causality "
        "case or an explicit reviewed entry to leakage_exemptions.EXEMPT_NODE_KINDS."
    )


def test_node_harness_catches_a_forward_looking_leak() -> None:
    """End-to-end proof: the node comparator flags a deliberately shifted(-1) series."""
    frame = _synthetic_frame()

    def _leaky(bars: pd.DataFrame) -> FeatureSeries:
        # shift(-1) peeks one bar into the future — the canonical leakage.
        leaked = bars["close"].shift(-1).reset_index(drop=True)
        return FeatureSeries(feature_id="leaky_node", series=leaked, warmup_bars=0, leakage_status="suspect")

    with pytest.raises(LeakageError):
        assert_causal(_leaky, frame, sample_indices=[30, 60, 90])
