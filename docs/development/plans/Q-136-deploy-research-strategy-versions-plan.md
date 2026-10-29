# Q-136 implementation plan: Deploy research strategy versions

> **For implementation agents:** Read the linked specification and the instructions of each repository you
> change. Launch only through `./work start Q-136 --agent <agent> --worktree` after written-plan approval and
> after Q-135 is Done.

**Goal:** A strategy-file run deploys from `q_terminal` to MT5. The forward evaluator runs the version's hooks
on M5 inside the run's day-trade session, and only market orders are deployable.
**Spec:** [Specification](../specs/Q-136-deploy-research-strategy-versions-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `./work start` creates the task branch in `q_backend`. Create the same-named branch from `development` in
  `q_contracts` and `q_terminal`, in worktrees under `.worktrees/`. Vendor the unpushed contract commit with
  `make contracts CONTRACTS_REPO=<q_contracts worktree>`.
- Call hooks only through the research adapter's existing invocation (`research/hooks.py`, `research/adapter.py`).
  Do not reimplement order validation.
- Do not edit expected values in existing tests to make them pass.
- Tests use fixture strategy directories, hand-built bars, `tests/execution/fake_edge.py` and the terminal's fake
  server. No MT5, Wine, Docker, GPU or desktop run.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing. `q_terminal`
  worktree: symlink `target` to `/mnt/build/targets/q_terminal-<branch-slug>` before the first cargo build.

## Ordered implementation

### 1. Contract (`q_contracts`)

**Files:** `schema/api/openapi.yaml`, `schema/api/examples/`, `COMPAT.md`, generated outputs.

- [ ] Add `order_kinds` to the backtest run detail, the `strategy_version` source kind and its provenance
  (version id, bundle hash) to deployment summary and detail, and the day-trade session to deployment detail.
- [ ] Regenerate, run `make check`, and commit.

### 2. Bundles side by side (`q_backend`)

**Files:** `research/strategy_files.py`, `tests/research/test_strategy_files.py`.

- [ ] Write a failing test for acceptance 1.
- [ ] Import each bundle as `q_strategies_<hash12>`. Drop the eviction of other bundles, but keep the lock
  around `sys.path` edits. Keep `load_strategy()` behaviour. Run the Q-134 tests. Commit.

### 3. Order kinds (`q_backend`)

**Files:** `research/adapter.py`, `research/results.py`, `research/strategy_file_runs.py`,
`api/backtest_jobs.py`, `backtesting/run_service.py`, `tests/research/test_order_kinds.py` (new).

- [ ] Write failing tests for acceptance 2.
- [ ] Classify every order the adapter receives and expose the set on the result. Store it in
  `result_summary.order_kinds`, and add `tick_hooks` from the version's file info. Expose it in run detail.
  Commit.

### 4. Research evaluator (`q_backend`)

**Files:** `execution/research_evaluator.py` (new), `execution/strategy_build.py`, `execution/worker.py`,
`execution/recovery.py`, `execution/warmup.py`, `execution/parity.py`, `storage/settings.py`,
`tests/execution/test_research_evaluator.py` (new), `tests/execution/test_research_parity.py` (new).

- [ ] Write failing tests for acceptance 4–6.
- [ ] Implement the evaluator with `StrategyEvaluator`'s contract, building the strategy from the frozen
  version (`bundle_of`, `load_bundle`, `coerce_params`). Map orders to decision actions. Pause with a risk event
  on a non-deployable kind or a raising hook.
- [ ] Add the day-trade session rules, including the flatten at start or resume.
- [ ] Choose the evaluator in the worker and recovery by source kind. Add `Q_EXECUTION_RESEARCH_WARMUP_BARS` and
  `warmup_bars`.
- [ ] Add a research reference to `execution/parity.py` and the parity test. Run `tests/execution/`. Commit.

### 5. Deployment creation (`q_backend`)

**Files:** vendored contracts, `execution/validation.py`, `execution/domain.py`, `api/services/execution.py`,
`api/schemas/execution.py`, `storage/db/execution_models.py` with an Alembic revision if a column is needed,
`tests/api/` execution tests.

- [ ] Vendor the contract and run `make contracts-check`.
- [ ] Write failing tests for acceptance 3 and 7.
- [ ] Extend the saved-run path for strategy-file runs. Validate order kinds, the timeframe and day trading,
  validate configuration patches against the version specs, and lower the timeframe floor to M5.
- [ ] Run the new tests, `tests/execution/`, the execution API tests, and `ruff`/`black` on the changed files.
  Commit.

### 6. Terminal (`q_terminal`)

**Files:** vendored contracts, `src/execution_controls.rs`, `src/execution_models.rs`,
`src/stream/fake_commands.rs`, `src/stream/fake_server.rs`, `qml/DeployDialog.qml`,
`qml/EditDeploymentDialog.qml`, `qml/DeploymentList.qml`, `qml/DeploymentDetail.qml`, tests under `tests/` and
`qml/tests/`.

- [ ] Vendor the contract and run `make contracts-check`.
- [ ] Write failing tests for acceptance 8.
- [ ] List strategy-file runs with their kinds and session. Render `bool` and `time` fields. Select MT5 from
  `mt5_orders`, and show the badge and version in the list and detail. Use design tokens only; `make check`
  runs the token gate.
- [ ] Run `env -u WAYLAND_DISPLAY -u DISPLAY make check`. Commit.

### 7. Documentation (`q_backend`)

**Files:** `docs/research/user_guide/strategy_files.rst`, `docs/operations/mt5-demo-trading.md`.

- [ ] Document deployable order kinds, `warmup_bars`, and the day-trade session rule, including choosing a close
  time inside trading hours. Commit.

## Verification

- `q_contracts`: `make check`.
- `q_backend`: `uv run pytest tests/research/test_strategy_files.py tests/research/test_order_kinds.py
  tests/execution` and the execution API tests, `make contracts-check`, and `ruff`/`black` on the changed files.
- `q_terminal`: `env -u WAYLAND_DISPLAY -u DISPLAY make check`.
- `./work board set Q-136 in-review -m "<summary per repository; checks and results; follow-ups>"`.
