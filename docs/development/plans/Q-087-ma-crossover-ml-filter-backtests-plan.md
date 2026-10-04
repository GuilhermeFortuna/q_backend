# Q-087 implementation plan: MA Crossover ML filter backtests

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-087 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** Deliver the behavior and acceptance criteria in the linked specification.
**Architecture:** Worker-backed causal datasets and saved classifier pipelines feed a post-combination entry gate using existing q_core execution.
**Tech stack:** Python, pandas, sklearn, LightGBM, SQLAlchemy/Postgres, Dramatiq, Parquet and q_core.
**Spec:** [Specification](../specs/Q-087-ma-crossover-ml-filter-backtests-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Batch 14 is research-only; original MACrossover remains compatible. No live, GPU, automatic retraining or multi-entry ML support.
- Use Q-085 generated contracts, canonical repo tooling and existing execution semantics; never hand-edit vendored/generated consumer types.
- Tests use frozen/fake sources and small CPU models; production evaluation uses actual engine reruns and immutable data.
- Follow the linked spec's exact defaults, timing, split, feature, threshold, compatibility and error rules.
- Commit only focused task changes; no push, merge or protected-branch checkout. Missing prerequisites use the documented board workflow.

## Review focus

- Rejected opposite entries still close open long and short positions.
- Combination logic must not recreate a rejected signal.
- Model/date/config mismatch fails before misleading output.
- Probability must not change sizing or original exit behavior.
- History restore and unsupported workflows retain explicit pinned identities.

## Ordered implementation

### 1. Register the separate research variant and hydrate its gate

**Files:** Create src/q_backend/backtesting/strategies/ma_crossover_ml_filter.py; modify strategies/__init__.py, strategy_registry.py, factory.py and api/backtest_jobs.py; tests/backtesting/test_ma_crossover_ml_filter.py and tests/api/test_ml_filter_backtest.py.
**Interfaces:** MACrossoverMLFilter reuses original crossover strategy params and math; generated ml_filter config resolves Q-086 ready model once and wraps final CompositeEntryStrategy. No filtering inside the per-slot stance calculation.

- [x] Add focused failing tests: Test original goldens unchanged, identical raw crossover columns, accepted/rejected long/short reversal at next open, repeated score changes without crossover, stop/target/day close with rejected entry, and matching chart/engine decisions. Assert probability never alters sizing strength.
- [x] Run `uv run pytest tests/backtesting/test_ma_crossover_ml_filter.py tests/api/test_ml_filter_backtest.py tests/backtesting/test_engine_registry_baseline.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [x] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [x] Run `uv run pytest tests/backtesting/test_ma_crossover_ml_filter.py tests/api/test_ml_filter_backtest.py tests/backtesting/test_engine_registry_baseline.py` and confirm the focused suite passes.
- [x] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 2. Enforce compatibility and supported workflows

**Files:** Modify ml_filters/service.py, api/schemas/backtest.py integration, execution/strategy_build.py and existing optimization/discovery request validation; tests/ml_filters/test_compatibility.py and tests/backtesting/test_ml_filter_capabilities.py.
**Interfaces:** Central validate_filter_compatibility(config, model_manifest); generated registry capability fields exclude the new variant from unsupported workflows while retaining original capabilities.

- [ ] Add focused failing tests: Reject original+filter, missing/unknown model, multiple entries, non-or/nonempty manager params, tick/optimization/walkforward/discovery/live use, mismatched MA/exit/cost/sizing/day settings and training-overlap trading dates. Permit pre-train warm-up only with suppressed entries; artifact loading or invalid probability fails the run.
- [ ] Run `uv run pytest tests/ml_filters/test_compatibility.py tests/backtesting/test_ml_filter_capabilities.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/ml_filters/test_compatibility.py tests/backtesting/test_ml_filter_capabilities.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 3. Persist pinned references and diagnostics

**Files:** Modify api/backtest_jobs.py, backtesting/run_service.py and chart_data.py additive diagnostics as needed; tests/api/test_ml_filter_backtest_history.py; update README.md.
**Interfaces:** Persist generated ml_filter and ml_filter_summary; score/acceptance diagnostics alongside raw indicators; run reload retains exact version/threshold regardless of subsequent training.

- [ ] Add focused failing tests: Save/reload model+threshold/config; unavailable reference does not substitute a version. Summaries distinguish scored/accepted/rejected/not-ready candidates from executed trades. Existing CSV/market_data columns remain valid and original strategy has no new mandatory fields.
- [ ] Run `uv run pytest tests/api/test_ml_filter_backtest_history.py tests/backtesting/test_chart_data.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/api/test_ml_filter_backtest_history.py tests/backtesting/test_chart_data.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

## Verification and handoff

- [ ] Review spec coverage and all five review-focus conditions against the focused tests above; fill any gaps before completion.
- [ ] Run `make contracts-check` and `./scripts/ci.sh` once after the final change. Do not wrap canonical CI in resource-slice commands. No Wine, GPU or desktop run is required.
- [ ] Update task documentation with actual checks/results and any blocked prerequisites; do not claim unrun checks passed.
- [ ] Commit final docs/code and use `./work board set Q-087 in-review -m "<changes; checks/results; follow-ups>"`. Human review/finish owns integration and publication.
