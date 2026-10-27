# Q-133 implementation plan: Priced entries after same-bar exits in research backtests

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-133 --agent <agent> --worktree` after written-plan approval and after Q-132 is released.

**Goal:** `q_backend` pins the Q-132 kernel, reports a reason for each rejected entry, and documents the
priced-entry rule.
**Spec:** [Specification](../specs/Q-133-priced-entries-after-same-bar-exits-research-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `q_backend` only. No contract, frontend or `q_core` change.
- Do not edit expected values in existing tests to make them pass. If an existing test changes, it is a
  regression to report, not to absorb.
- Test with hand-built bars and a `tmp_path` tick store, as `tests/research/test_priced_entries.py` does. No
  worker, Redis, MT5, Docker, GPU, Wine or desktop.
- Create the environment with `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, and set the same variable when
  committing.

## Ordered implementation

### 1. Pin Q-132

**Files:** `pyproject.toml`, `uv.lock`.

- [x] Set the `q-core` tag to the Q-132 release and run `uv lock --upgrade-package q-core`.
- [x] Run `tests/research/test_priced_entries.py` and `tests/research/test_resting_orders_backtest.py` to
  confirm nothing else moved. Commit.

### 2. Rejection reason

**Files:** `src/q_backend/backtesting/candle_kernel.py`, `src/q_backend/research/results.py`,
`tests/research/test_priced_entries_after_exit.py` (new).

- [x] Write failing tests for acceptance 1–4.
- [x] Add `"reason"` to the arrays `run_chunk` copies. Add a `reason` column to `REJECTED_COLUMNS`,
  `empty_rejected_frame` and `rejected_entries_frame`, mapping codes to `level_on_wrong_side` and
  `touch_before_exit` and raising on any other code.
- [x] Run the new tests, the existing tests in acceptance 5, and `ruff`/`black`. Commit.

### 3. Documentation

**Files:** `docs/research/user_guide/execution.rst`, `docs/research/user_guide/backtesting.rst`,
`src/q_backend/research/orders.py`, `src/q_backend/research/results.py`.

- [x] Document the rule and the reason column as the spec lists. Commit.

### 4. Experiment rerun

- [x] Run `BracketMomentumStrategy` with its `main()` setup from `experiments/strategies/ind_strategies.py`
  (on the `research/experiments` branch, not `development`) on the new pin, with a throwaway script outside the
  repository. Record its trades, PnL, drawdown and
  `touch_before_exit` count for the board message. The pre-fix figures are 9,090 trades, PnL 185,705 and
  drawdown 7,632.

## Verification

- `uv run pytest tests/research/test_priced_entries_after_exit.py tests/research/test_priced_entries.py tests/research/test_resting_orders_backtest.py tests/research/test_stop_target_backtest.py tests/research/test_parallel_sessions.py`
- `uv run ruff check` and `uv run black --check` on the changed files.
- `./work board set Q-133 in-review -m "<summary, checks, experiment figures>"`.
