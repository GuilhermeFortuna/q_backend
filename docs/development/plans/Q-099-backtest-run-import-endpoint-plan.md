# Q-099 implementation plan: Backtest run import endpoint

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-099 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** A finished backtest posted to the control API becomes a completed run that every existing read endpoint serves.
**Architecture:** A synchronous endpoint over one import service function that writes the ledger rows and the lake files together; origin is a column on the run.
**Tech stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, pandas, pytest.
**Spec:** [Specification](../specs/Q-099-backtest-run-import-endpoint-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Follow the Q-097 contract exactly; never hand-edit vendored contract code, use `make contracts`.
- Reuse `write_backtest_artifacts`, `write_backtest_result`, `build_equity_curve` and `delete_backtest_lake_artifacts`; add no second lake layout.
- Existing runs and every existing endpoint keep their behaviour; `origin` defaults to `stack` everywhere.
- Tests use the API test client, the test database and a temporary lake root. No worker, Redis, gateway, Docker, GPU or desktop run.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- No path leaves a row without a result or a lake directory without a row.
- Validation failures are reported before anything is written.
- The migration is reversible and backfills existing rows through the server default.
- A job never reuses a script run through `find_backtest_run_by_config`.
- Imported trades and equity round-trip through the existing artifact readers, including timezone handling.

## Ordered implementation

### 1. Pin the Q-097 contract

**Files:** Modify `CONTRACTS_REV`; run `make contracts`.

- [ ] Set `CONTRACTS_REV` to the merged Q-097 commit, run `make contracts` then `make contracts-check`.
- [ ] If the commit cannot be fetched from the remote, stop and use the blocked workflow; do not point the Makefile at a local path in a committed change.
- [ ] Commit the pin and the regenerated vendored tree.

### 2. Add origin and provenance to the ledger

**Files:** Create an Alembic revision under `alembic/versions/`; modify `src/q_backend/storage/db/models.py`, the run repository functions, `src/q_backend/api/schemas/backtest.py` and `src/q_backend/backtesting/run_service.py`; extend the run service and router tests under `tests/api` and `tests/storage`.
**Interfaces:** `BacktestRun.origin`, `BacktestRun.provenance`; `origin` on list item and detail; `provenance` on detail; `origin` filter on `list_runs`; `find_backtest_run_by_config` restricted to stack runs.

- [ ] Add failing tests: existing runs report `stack`; the list filter partitions runs; a job does not reuse a script run with an equal configuration.
- [ ] Run the focused tests and confirm they fail on the missing column and fields.
- [ ] Implement the migration, model, schemas, filter and the reuse restriction.
- [ ] Run the focused tests and the migration upgrade and downgrade against the test database.
- [ ] Commit this unit.

### 3. Implement the import service and endpoint

**Files:** Create `src/q_backend/backtesting/run_import.py`; modify `src/q_backend/api/routers/backtest.py` and `src/q_backend/api/schemas/backtest.py`; create `tests/api/test_backtest_import.py`.
**Interfaces:** `import_backtest_run(session, request) -> str`; `POST /api/v1/backtests/import`.

- [ ] Add failing tests for spec acceptance items 1 to 4: the round trip through list, detail, result, both artifacts and both exports; each validation rule; and a forced lake write failure.
- [ ] Run `uv run pytest tests/api/test_backtest_import.py -q` and confirm the cases fail because the route is missing.
- [ ] Implement validation first, then the write with cleanup on failure.
- [ ] Run the file again and confirm it passes.
- [ ] Commit this unit.

### 4. Keep script runs review-only and document

**Files:** Modify the ML filter source eligibility in `src/q_backend/ml_filters/` and its test; modify `README.md`.

- [ ] Add a failing test that a script run is an ineligible ML filter source with a reason naming its origin.
- [ ] Implement, run `uv run pytest tests/ml_filters -q -k source`, and update the README section the spec names.
- [ ] Commit this unit.

## Verification and handoff

- [ ] Run `uv run pytest tests/api tests/storage tests/backtesting -q -m "not integration" -k "backtest or run"`, `uv run ruff check src/q_backend/api src/q_backend/backtesting src/q_backend/storage tests/api` and `uv run black --check` on the same paths.
- [ ] Run `make contracts-check`.
- [ ] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-099 in-review -m "<changes; checks and results; follow-ups>"`. State that the operator must apply the migration to the Research database after merge.
