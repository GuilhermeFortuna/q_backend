# Q-086 implementation plan: ML filter training and comparison

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Implement natively; delegation requires separate authorization.

**Goal:** Deliver the behavior and acceptance criteria in the linked specification.
**Architecture:** Worker-backed causal datasets and saved classifier pipelines feed a post-combination entry gate using existing q_core execution.
**Tech stack:** Python, pandas, sklearn, LightGBM, SQLAlchemy/Postgres, Dramatiq, Parquet and q_core.
**Spec:** [Specification](../specs/Q-086-ml-filter-training-and-comparison-spec.md)
**Status:** implementation and canonical CI complete; awaiting human review.

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

- [x] Add focused tests for previous-bar features, alias/order preservation, source eligibility, UTC mapping, and partition rejection attribution.
- [x] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [x] Run focused dataset and feature tests successfully.
- [x] Commit the dataset and feature unit on the task branch with a conventional, focused message.

### 2. Fit and persist interchangeable model versions

**Files:** Create ml_filters/adapters.py and artifacts.py; modify pyproject.toml/uv.lock; tests/ml_filters/test_adapters.py and test_artifacts.py. Use a focused adapter package split if needed rather than grow one implementation file.
**Interfaces:** EntryClassifier fit/predict_good_entry_probability/dump/load; create_classifier(algorithm, hyperparams, seed); FittedEntryModel; load_model_version(model_version_id). Model manifest and version include fitted checksum and exact ordered pipeline.

- [x] Add focused CPU tests for all three classifiers, training-only scaling, serialization, feature order, parameter validation, and artifact checksums.
- [x] Implement the specified interfaces and behavior, keeping public types aligned with Q-085 and preserving the existing patterns named above.
- [x] Run focused adapter and artifact tests successfully.
- [x] Commit the classifier and artifact unit on the task branch with a conventional, focused message.

### 3. Implement reusable gate and actual validation reruns

**Files:** Create ml_filters/filter.py and evaluation.py; tests/ml_filters/test_filter.py and test_ml_filter_evaluation.py. Reuse CompositeEntryStrategy and BacktestEngine/q_core signal arrays.
**Interfaces:** EntryFilteredStrategy(base_strategy, fitted_model, threshold); compare_filters(request, job_id); evaluate_filter(request, job_id). Wrapper preserves exits/strength and only masks final entry columns.

- [x] Add focused gate tests for entry masking, preserved exits/strength, readiness, threshold validation, and partition windows.
- [x] Implement the wrapper and actual baseline/filtered engine reruns, including validation-only comparison and lockbox evaluation.
- [x] Run focused gate, evaluation, and candle-kernel tests successfully.
- [x] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 4. Persist jobs, models and one-time evaluations

**Files:** Modify storage/db/models.py/repositories.py; create normal Alembic migration and tests/ml_filters/test_persistence.py; add training.py/service.py for orchestration.
**Interfaces:** MLFilterRun, MLFilterModelVersion, MLFilterEvaluation; train_filters(request, job_id); transactional dataset lockbox reservation; immutable completed result/artifact references.

- [x] Add focused persistence tests for durable job/model records and unique per-dataset lockbox reservations.
- [x] Implement DB models, repositories, migration, atomic training publication, and retry-safe lockbox reservation.
- [x] Run persistence tests successfully.
- [x] Commit this independently reviewable unit on the task branch with a conventional, focused message.

### 5. Expose worker-backed API and hand off

**Files:** Create api/ml_filter_jobs.py and api/routers/ml_filters.py; extend tasks/actors.py and normal router registration; tests/api/test_ml_filters.py and tests/ml_filters/test_jobs.py; update README.md.
**Interfaces:** All Q-085 source/training/model/comparison/evaluation routes and durable job states; compatibility service used by Q-087; source summaries with suggested explicit cutoffs and readiness reasons.

- [x] Add focused API/source tests for frozen-artifact reads, source readiness, typed request validation, and model-response secrecy.
- [x] Implement the worker-backed routes and document defaults, bounds, and workflow behavior.
- [x] Run the focused ML and API test suites successfully.
- [x] Commit this independently reviewable unit on the task branch with a conventional, focused message.

## Verification and handoff

- [x] Review spec coverage and all five review-focus conditions against the implementation and focused tests.
- [x] Run `make contracts-check` and `./scripts/ci.sh` after the final changes; both pass.
- [x] Update task documentation with actual check results; do not claim unrun checks passed.
- [x] Commit final docs/code on the task branch with focused messages.
- [ ] Use `./work board set Q-086 in-review -m "<changes; checks/results; follow-ups>"`. Human review/finish owns integration and publication.

## Implementation verification record

- Focused suite: `uv run --no-sync pytest tests/storage/test_execution_repositories.py::test_execution_migration_revision_chain tests/features/test_feature_store_db.py::test_feature_store_migration_revision_chain tests/neural/test_model_registry_db.py::test_neural_model_migration_revision_chain tests/streaming/test_status_mapping.py::test_status_mapping_covers_all_job_manager_literals tests/storage/test_db_models.py tests/ml_filters tests/api/test_ml_filters.py tests/backtesting/test_candle_kernel_bridge.py` — 40 passed.
- Canonical CI: contracts, Postgres migrations, Ruff, and Black passed; unit suite 2,194 passed and 15 skipped; integration suite 56 passed.
- Static checks: Ruff passed across the repository; Black passed across all 702 Python files; `git diff --check` passed.
- Contract check: `PATH="$PWD/.venv/bin:$PATH" CONTRACTS_REPO=/home/gui/projects/q/q_contracts make contracts-check` — passed. The host `python3` lacks PyYAML, so the worktree's installed Python was placed first on `PATH` to use its already installed PyYAML instead of attempting a network download.
