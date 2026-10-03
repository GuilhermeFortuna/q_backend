# Q-086 implementation plan: ML filter training and comparison

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-086 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** Deliver the behavior and acceptance criteria in the linked specification.
**Architecture:** Worker-backed causal datasets and saved classifier pipelines feed a post-combination entry gate using existing q_core execution.
**Tech stack:** Python, pandas, sklearn, LightGBM, SQLAlchemy/Postgres, Dramatiq, Parquet and q_core.
**Spec:** [Specification](../specs/Q-086-ml-filter-training-and-comparison-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Batch 14 is research-only; original MACrossover remains compatible. No live, GPU, automatic retraining or multi-entry ML support.
- Use Q-085 generated contracts, canonical repo tooling and existing execution semantics; never hand-edit vendored/generated consumer types.
- Tests use frozen/fake sources and small CPU models; production evaluation uses actual engine reruns and immutable data.
- Follow the linked spec's exact defaults, timing, split, feature, threshold, compatibility and error rules.
- Commit only focused task changes; no push, merge or protected-branch checkout. Missing prerequisites use the documented board workflow.

## Review focus

- Entry candle data must never enter signal-time features.
- Trades crossing cutoffs and missing volume/labels are counted honestly.
- One-class data and train-only preprocessing do not produce misleading metrics.
- Atomic publication and duplicate worker deliveries preserve immutable versions.
- Concurrent final evaluations consume the dataset tail only once.

## Ordered implementation

### 1. Freeze eligible sources and causal samples

**Files:** Create src/q_backend/ml_filters/{config,dataset,features}.py; tests/ml_filters/test_dataset.py and test_features.py. Read api/backtest_jobs.py, backtesting/chart_data.py, strategy.py, candle_kernel.py and storage/lake/artifacts.py first. If task base lacks market_data persistence, implement it in the existing artifact path as part of this unit; a baseline must retain complete OHLCV and e0 indicators without a provider refetch.
**Interfaces:** MLFilterDataset and EntryFeatureConfig domain types; build_source_dataset(source_run_id, config); build_entry_features(frame, sides, feature_names). Consume Q-085 generated request/manifest types.

- [ ] Add focused failing tests: Entry bar has extreme OHLCV differing from signal bar: assert only previous-bar inputs. Verify gaps, UTC/Brazil conversion, side, prefix invariance, e0 names, 20-sample/two-per-class minima, volume omissions, unclosed/cross-boundary rejection counts, missing legacy artifacts and incompatible source configurations.
- [ ] Run `uv run pytest tests/ml_filters/test_dataset.py tests/ml_filters/test_features.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/ml_filters/test_dataset.py tests/ml_filters/test_features.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 2. Fit and persist interchangeable model versions

**Files:** Create ml_filters/adapters.py and artifacts.py; modify pyproject.toml/uv.lock; tests/ml_filters/test_adapters.py and test_artifacts.py. Use a focused adapter package split if needed rather than grow one implementation file.
**Interfaces:** EntryClassifier fit/predict_good_entry_probability/dump/load; create_classifier(algorithm, hyperparams, seed); FittedEntryModel; load_model_version(model_version_id). Model manifest and version include fitted checksum and exact ordered pipeline.

- [ ] Add focused failing tests: Fit all three tiny CPU datasets; positive probability maps by classes_ label 1; scaler training statistics exclude extreme validation rows; serialization retains scores; changed feature order/data/state changes identity; corrupt checksum/version and invalid algorithm parameters reject; atomic incomplete artifacts are never selectable.
- [ ] Run `uv run pytest tests/ml_filters/test_adapters.py tests/ml_filters/test_artifacts.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/ml_filters/test_adapters.py tests/ml_filters/test_artifacts.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 3. Implement reusable gate and actual validation reruns

**Files:** Create ml_filters/filter.py and evaluation.py; tests/ml_filters/test_filter.py and test_evaluation.py. Reuse CompositeEntryStrategy and BacktestEngine/q_core signal arrays.
**Interfaces:** EntryFilteredStrategy(base_strategy, fitted_model, threshold); compare_filters(request, job_id); evaluate_filter(request, job_id). Wrapper preserves exits/strength and only masks final entry columns.

- [ ] Add focused failing tests: Reject reverse entry but still close existing long/short at next open; accepted reversal opens normally. Append future bars with frozen model and assert scores unchanged. Validate threshold endpoints, finite scores, not-ready rejection, unchanged strength/sizing, partition flat starts/warm-up and equal end-close convention; contrast engine results with a misleading retained-PnL sum.
- [ ] Run `uv run pytest tests/ml_filters/test_filter.py tests/ml_filters/test_evaluation.py tests/backtesting/test_candle_kernel_bridge.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/ml_filters/test_filter.py tests/ml_filters/test_evaluation.py tests/backtesting/test_candle_kernel_bridge.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 4. Persist jobs, models and one-time evaluations

**Files:** Modify storage/db/models.py/repositories.py; create normal Alembic migration and tests/ml_filters/test_persistence.py; add training.py/service.py for orchestration.
**Interfaces:** MLFilterRun, MLFilterModelVersion, MLFilterEvaluation; train_filters(request, job_id); transactional dataset lockbox reservation; immutable completed result/artifact references.

- [ ] Add focused failing tests: Test rollback/all-or-nothing multi-algorithm failure, duplicate delivery, concurrent conflicting lockbox selection, identical retry after failure, concurrent source-file replacement, source deletion after frozen dataset publication and results after Redis expiry. Unique lockbox reservation keys dataset_id, not merely request id.
- [ ] Run `uv run pytest tests/ml_filters/test_persistence.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/ml_filters/test_persistence.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 5. Expose worker-backed API and hand off

**Files:** Create api/ml_filter_jobs.py and api/routers/ml_filters.py; extend tasks/actors.py and normal router registration; tests/api/test_ml_filters.py and tests/ml_filters/test_jobs.py; update README.md.
**Interfaces:** All Q-085 source/training/model/comparison/evaluation routes and durable job states; compatibility service used by Q-087; source summaries with suggested explicit cutoffs and readiness reasons.

- [ ] Add focused failing tests: Mock provider to raise if called: source preparation/comparison must read frozen artifacts only. Assert 202 jobs, typed 404/409/422, progress, unavailable artifacts, failure of one algorithm, persisted completed results and no serialized pipeline in API responses. Document sample/partition/model defaults and limits.
- [ ] Run `uv run pytest tests/ml_filters tests/api/test_ml_filters.py` and confirm the new behavior is missing before implementation; do not count import/setup failures as behavioral evidence.
- [ ] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [ ] Run `uv run pytest tests/ml_filters tests/api/test_ml_filters.py` and confirm the focused suite passes.
- [ ] Commit this independently reviewable unit on the task branch with a conventional, focused message.

## Verification and handoff

- [ ] Review spec coverage and all five review-focus conditions against the focused tests above; fill any gaps before completion.
- [ ] Run `make contracts-check` and `./scripts/ci.sh` once after the final change. Do not wrap canonical CI in resource-slice commands. No Wine, GPU or desktop run is required.
- [ ] Update task documentation with actual checks/results and any blocked prerequisites; do not claim unrun checks passed.
- [ ] Commit final docs/code and use `./work board set Q-086 in-review -m "<changes; checks/results; follow-ups>"`. Human review/finish owns integration and publication.
