"""Single source of truth for causality-invariant exemptions (WO181).

Every computable feature source in the system is covered by a causality invariant
(``assert_causal`` for feature specs; prefix-frame vs full-frame agreement for genome
node kinds). Historically the harnesses carried *silent* filters inside the test files,
so each new feature source (neural latents, exogenous specs, new node families) quietly
fell outside the invariant. A lookahead bug in any uncovered path produces beautiful
backtests and dead paper strategies — the exact failure mode the evaluation-realism work
exists to prevent.

This module makes exemptions **explicit and reviewed**. An entry may live here only with
a stated reason. Hygiene meta-tests fail the suite if:

* an exemption names a spec/node kind that no longer exists (stale drift), or
* a spec/node kind is neither tested nor exempted (a new source slipped through).

Two kinds of exemption exist:

* ``EXEMPT_SPECS`` — ``FeatureSpec`` names (``list_feature_specs()``).
* ``EXEMPT_NODE_KINDS`` — genome ``NODE_SPECS`` kinds.

If a real leak is ever found, fix it when unambiguous; otherwise exempt it here with a
``LEAK-CONFIRMED:`` prefix and report it prominently. Do **not** silently re-filter it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Feature-spec exemptions (tests/features/test_leakage.py)
# ---------------------------------------------------------------------------
# Currently empty by design:
#   * classical / session / regime / htf specs are covered directly by assert_causal;
#   * exogenous specs are brought *under* the invariant (the harness re-aligns them on
#     each prefix through the publish-time as-of join), so they are not exempt;
#   * neural specs are not in the static catalog at collection time — they are covered
#     by the fixture-backed test_neural_spec_is_causal (PCA encoder path).
EXEMPT_SPECS: dict[str, str] = {
    # "spec_name": "reason this cannot leak / why exempt (reviewed WO181)",
}


# ---------------------------------------------------------------------------
# Genome node-kind exemptions (tests/backtesting/genome/test_node_causality.py)
# ---------------------------------------------------------------------------
_EXOG_NODE_REASON = (
    "Reads a pre-aligned exogenous column rather than deriving one from bare OHLCV; "
    "the alignment (backward as-of on publish/availability time) and its causality are "
    "covered by the exogenous feature-spec invariant — test_feature_spec_is_causal "
    "re-aligns exogenous specs on every prefix — plus "
    "test_exogenous_alignment_uses_publish_time (WO181 Task 3)."
)

EXEMPT_NODE_KINDS: dict[str, str] = {
    "ind.latent": (
        "Emits no data-derived series on bare OHLCV — it requires a registered "
        "production neural model. Its causality is covered by "
        "test_latent_node_prefix_causality through the torch-free PCA encoder path "
        "(WO181 Task 2)."
    ),
    "source.exog.close": _EXOG_NODE_REASON,
    "source.exog.return": _EXOG_NODE_REASON,
    "source.exog.return_zscore": _EXOG_NODE_REASON,
    "source.exog.rolling_corr": _EXOG_NODE_REASON,
    "source.exog.relative_strength": _EXOG_NODE_REASON,
    "source.exog.vol_regime": _EXOG_NODE_REASON,
    "source.exog.direction_regime": _EXOG_NODE_REASON,
    "exit.fixed_holding": (
        "Exit-policy primitive: produces no data-derived feature column (resolved "
        "structurally in compile as a bar-index holding rule). Exit semantics are "
        "pinned field-for-field by the golden backtests (WO180)."
    ),
    "exit.opposite_signal": (
        "Exit-policy primitive: produces no data-derived feature column (mirrors the "
        "opposite entry signal structurally). Exit semantics pinned by golden "
        "backtests (WO180)."
    ),
    "exit.rebalance": (
        "Exit-policy primitive: produces no data-derived feature column (time-based "
        "rebalance resolved in the engine). Exit semantics pinned by golden "
        "backtests (WO180)."
    ),
}
