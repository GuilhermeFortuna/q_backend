# Q-113 implementation plan: Quote features outside the genome

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-113 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** `quote_spread` and `spread_vs_volatility` are tick-store features outside the genome registry, return values where ticks exist, and the unit suite passes.
**Architecture:** The feature registry owns the two recipes under a `quote` source. `compute_feature` routes that source to the tick store, resolved by the function the chart service uses. Callers pass the symbol.
**Tech stack:** Python 3.12, pandas, pytest.
**Spec:** [Specification](../specs/Q-113-quote-features-outside-the-genome-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Each step starts from the failing tests it is meant to fix. Do not delete, skip or exempt a test to make it pass, and do not edit the latent gate or encoder ablation tests.
- Feature names, versions, parameter keys and defaults do not change; feature identifiers stay stable.
- No change to `q_contracts`, `q_frontend`, the database schema or vendored contract code.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- No path lets a genome reference a quote recipe: validation, the add-node pool, the capability registry, the strategy-builder prompt and the score-bias map.
- The tick-backed causality harness would fail on a real lookahead: spreads differ per bar, and the planted-leak test proves it.
- The chart service and the feature store open the same tick store for the same symbol.
- A missing or empty tick store produces missing values, never an exception, in the matrix and in evaluation.
- The latent gate's baseline feature set is the same as before Q-112.

## Ordered implementation

### 1. Registry

**Files:** `src/q_backend/backtesting/genome/node_specs.py`, `src/q_backend/features/registry.py`, `tests/features/test_feature_registry.py`, `tests/backtesting/genome/`.

- [x] Add failing tests for spec acceptance items 1 and 2, including the two feature identifiers pinned as literals taken from `5a71e95`.
- [x] Remove the two `NodeSpec` entries. Declare the quote recipes' parameter keys and defaults in the feature registry and build both specs with source `quote`.
- [x] Extend `assert_catalog_consistent` with the `quote` rules from the spec.
- [x] Run `uv run pytest tests/features/test_feature_registry.py tests/backtesting/genome tests/api/test_strategy_builder_prompt.py -q`. The four node-causality failures, the pool failure and the prompt budget failure are gone. If the budget still fails, apply acceptance item 7. Commit.

### 2. Computation

**Files:** `src/q_backend/features/compute.py`, `src/q_backend/market_data/indicator_series.py`, `src/q_backend/features/evaluation.py`, `src/q_backend/features/evaluation_service.py`, `src/q_backend/features/evidence_service.py`, `tests/features/`.

- [x] Add failing tests for spec acceptance items 3 and 4, writing ticks with the tick store's own day writer as `tests/api/test_market_indicators.py` does.
- [x] Move the "open the tick store or return none" logic into one function used by the chart service and by `_compute_quote_feature`; dispatch on source.
- [x] Pass the symbol from the evaluation run into the leakage check and from the evidence segment into its matrix builder.
- [x] Run `uv run pytest tests/features tests/neural/test_latent_gate.py tests/api/test_encoder_ablation_jobs.py tests/api/test_market_indicators.py -q`; commit.

### 3. Causality coverage

**Files:** `tests/features/test_leakage.py`, a tick-store fixture in `tests/features/conftest.py`.

- [x] Add a fixture that writes varying quotes across the harness's synthetic bars and points `Q_RESEARCH_TICK_STORE` at them.
- [x] Route `quote` specs in `test_feature_spec_is_causal` through `compute_feature` with the fixture's symbol; add the planted-lookahead test.
- [x] Run `uv run pytest tests/features/test_leakage.py tests/features/test_leakage_invariants.py -q`; commit.

### 4. Documents

- [ ] Update any repository document that describes the quote features as genome nodes or as always unavailable in the feature store. At authoring time `README.md` and `docs/research-library.md` name neither feature.
- [ ] Commit if anything changed.

## Verification

- `scripts/ci.sh` through the unit stage, with the 14 failures listed in the spec gone and no new failure, skip or exemption.
- `ruff` and `black` on touched files; `make contracts-check` to confirm vendored code is untouched.
- Review the whole change, then `./work board set Q-113 in-review` with the test totals before and after and the measured prompt length.
