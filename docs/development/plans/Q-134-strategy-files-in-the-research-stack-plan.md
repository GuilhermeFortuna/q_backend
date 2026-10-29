# Q-134 implementation plan: Strategy files in the Research stack

> **For implementation agents:** Read the linked specification and the instructions of each repository you
> change. Launch only through `./work start Q-134 --agent <agent> --worktree` after written-plan approval.

**Goal:** The Research stack lists `ResearchStrategy` files from `Q_STRATEGY_DIR`, backtests them from the
Backtests workspace, and freezes each run's code into an immutable strategy version.
**Spec:** [Specification](../specs/Q-134-strategy-files-in-the-research-stack-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `./work start` creates the task branch in `q_backend`. Create the same-named branch from `development` in
  `q_contracts` and `q_frontend`, in worktrees under `.worktrees/`.
- Contracts first. Vendor the unpushed `q_contracts` commit into consumers with
  `make contracts CONTRACTS_REPO=<q_contracts worktree>`. Never hand-edit vendored code.
- Run strategy code only through the research library's existing `backtest()`. Do not add a second execution
  path for research strategies.
- Do not edit expected values in existing tests to make them pass. A changed existing test is a regression to
  report.
- Tests use `tmp_path` strategy directories, hand-built bars, the API test client and MSW. No MT5, worker
  process, Redis, Docker, GPU, Wine or desktop run.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing. Frontend: link
  `node_modules` from the main checkout. Frontend tests run with `TZ=America/Sao_Paulo`.

## Ordered implementation

### 1. Contract (`q_contracts`)

**Files:** `schema/api/openapi.yaml`, `schema/api/examples/`, generated outputs.

- [ ] Add `bool` and `time` to `StrategyParamSpec.type`, and allow a boolean default.
- [ ] Add `StrategyFile`, `StrategyFileListResponse` and `StrategyVersion` (with `source_files`: a list of
  `path` and `content`), `GET /api/v1/strategy-files` and `GET /api/v1/strategy-versions/{version_id}`.
- [ ] Add `strategy_file` and `strategy_version_id` to `BacktestRequest`, and `strategy_version_id` to the run
  list and detail schemas. Add examples for each.
- [ ] Regenerate, run `make check`, and commit (`feat(api): ...`).

### 2. Parameter declarations (`q_backend`)

**Files:** `src/q_backend/research/params.py` (new), `src/q_backend/research/__init__.py`,
`tests/research/test_strategy_params.py` (new).

- [ ] Write failing tests:
  - complete declarations pass;
  - missing, extra and undefaulted arguments, wrong default types and out-of-bound defaults each raise a
    `ValueError` naming the argument;
  - specs convert to `StrategyParamSpec`, with `time` as `"HH:MM"`;
  - submitted values are coerced and bound-checked.
- [ ] Implement `IntParam`, `FloatParam`, `BoolParam`, `ChoiceParam` and `TimeParam` as frozen dataclasses. Add
  `declared_params(cls)`, which reads the `__init__` signature and returns validated specs, and
  `coerce_params(specs, submitted)`.
- [ ] Export them from `q_backend.research`. Run tests and `ruff`/`black`. Commit.

### 3. Strategy directory, bundles and validation (`q_backend`)

**Files:** `src/q_backend/storage/settings.py`, `src/q_backend/research/strategy_files.py` (new),
`tests/research/test_strategy_files.py` (new).

- [ ] Write failing tests for acceptance 1, 2 and 7, on a `tmp_path` directory with a `_lib` helper.
- [ ] Add the `Q_STRATEGY_DIR` setting (default `~/q-strategies`, expanded).
- [ ] Implement:
  - discovery of strategy files and helper modules;
  - `bundle(strategy_id)`: sorted relative paths and contents, and their SHA-256;
  - `load_bundle(bundle)`, which imports a bundle as package `q_strategies`. It writes the bundle to
    `data/strategy_bundles/<hash>/q_strategies/`, puts `data/strategy_bundles/<hash>/` on the import path for the
    import only, and evicts `q_strategies*` from `sys.modules` before and after, so two bundles never mix;
  - `load_strategy(strategy_id)`, the current file's class.
- [ ] Implement `validate(bundle)`. It runs in a `multiprocessing` spawn child with a 10-second timeout, applies
  the spec's checks, and returns label, description, class name, specs, `uses_tick_hooks` (via
  `research.hooks.detect_hook_call`) or the first error. Cache results in memory by bundle hash.
- [ ] Export `load_strategy`. Run tests and `ruff`/`black`. Commit.

### 4. Strategy versions (`q_backend`)

**Files:** `src/q_backend/storage/db/models.py`, `alembic/versions/` (new revision),
`src/q_backend/research/strategy_versions.py` (new), `tests/storage/test_strategy_versions.py` (new).

- [ ] Add a `strategy_versions` table:
  - `id` (UUID), `strategy_id`, `class_name`;
  - `bundle_hash` (unique), `source_files` (JSONB), `param_specs` (JSONB), `created_at`.

  Add a nullable `strategy_version_id` foreign key on the backtest runs table.
- [ ] Implement `freeze(session, strategy_id) -> StrategyVersion`. It validates, inserts when the hash is new,
  returns the existing row otherwise, and is safe under concurrent freezes (unique-constraint retry).
- [ ] Test the migration upgrade and downgrade, idempotent freezing, and that versions survive file edits and
  deletions. Commit.

### 5. API and job (`q_backend`)

**Files:** `make contracts CONTRACTS_REPO=...` output, `src/q_backend/api/routers/strategy_files.py` (new),
`src/q_backend/api/schemas/backtest.py`, `src/q_backend/api/backtest_jobs.py`, the optimization, walk-forward,
discovery, ML-filter and execution request validators, `tests/api/test_strategy_files_api.py` (new),
`tests/api/test_strategy_file_backtests.py` (new).

- [ ] Vendor the contract and run `make contracts-check`.
- [ ] Write failing tests for acceptance 3–6.
- [ ] Add the two `GET` routes. Extend `BacktestRequest` with the mutually exclusive fields and validate their
  parameters with `coerce_params`. Reject both fields in the other workflows.
- [ ] In the job, freeze (or load, for `strategy_version_id`) the version and store its id on the run row, then
  load the bundle, build the class and call `backtest()` with `check_causality=True`. Use the request's
  settings and a `TickStore` when the configured store has the symbol. Persist the result through the lake
  writer that `backtesting/run_import.py` uses, onto the job's existing row. Extract a shared function from
  `research/publishing.py` and `run_import.py` instead of copying either. Keep the job's progress and
  cancellation checkpoints.
- [ ] Run the new tests, the existing backtest job, import and research tests, and `ruff`/`black`. Commit.

### 6. Container mount and docs (`q_backend`)

**Files:** `docker-compose.yml`, `docs/research/user_guide/strategy_files.rst` (new) and its entry in `docs/research/user_guide/index.rst`,
`docs/research-library.md`, `README.md`.

- [ ] Mount `${Q_STRATEGY_DIR:-~/q-strategies}` read-only into the API and worker services, and set
  `Q_STRATEGY_DIR` inside them.
- [ ] Write the guide page the spec lists, with a complete example file and `_lib` helper. Add a doctest-style
  test that the example validates. Commit.

### 7. Research UI (`q_frontend`)

**Files:** `make contracts CONTRACTS_REPO=...` output, `src/api/queries/` (new `strategyFiles.ts`),
`src/lib/backtesting/useBacktestConfig.ts`, the Backtests entry strategy picker,
`src/components/shared/StrategyParamFields.tsx`,
`src/components/backtests/BacktestHistoryPanel.tsx`, `src/mocks/handlers.ts`, tests under `tests/unit/`.

- [ ] Vendor the contract and run `make contracts-check`.
- [ ] Write failing tests for acceptance 8.
- [ ] Add the query, with `refetchOnWindowFocus`. Add the Strategy files picker group, and send
  `strategy_file` with the parameters from the run config. Render `bool` and `time` fields. Show the id and
  short hash in history, and make Re-run send `strategy_version_id`. Keep strategy files out of Optimize,
  Validate and Discover.
- [ ] Add MSW handlers and fixtures. Run the new and affected tests, `lint` and `typecheck`. Commit.

## Verification

- `q_contracts`: `make check`.
- `q_backend`: `uv run pytest tests/research/test_strategy_params.py tests/research/test_strategy_files.py
  tests/storage/test_strategy_versions.py tests/api/test_strategy_files_api.py
  tests/api/test_strategy_file_backtests.py`, plus the existing backtest job, run import and research tests the
  change touches. Run `make contracts-check`, and `uv run ruff check` and `uv run black --check` on the changed
  files.
- `q_frontend`: `TZ=America/Sao_Paulo pnpm test:run` on the new and affected test files, `pnpm lint`,
  `pnpm typecheck`, `make contracts-check`.
- `./work board set Q-134 in-review -m "<summary per repository; checks and results; follow-ups>"`.
