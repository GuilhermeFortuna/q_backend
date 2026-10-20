# Q-121 implementation plan: Fast tick-backed research backtests

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-121 --agent <agent> --worktree` after written-plan approval and with Q-119 and Q-120 Done. Implement natively; delegation requires separate authorization.

**Goal:** Research backtests with ticks cost one Python decision per closed bar plus Rust tick work, with results that do not depend on worker count.
**Architecture:**
- `backtest()` builds a native `SessionTickSource`.
- The adapter passes uncopied prefixes and builds tick frames from a reusable forming row.
- `TickEvaluator`'s rebuild and `TickWorkerPool` are removed.
- Day-trade runs split into session blocks, run in worker processes and merge deterministically.

**Tech stack:** Python 3.12, pandas 3 (copy-on-write), numpy, `q_core`, pytest.
**Spec:** [Specification](../specs/Q-121-fast-tick-backed-research-backtests-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Fills, ordering and tick matching stay in `q_core`. Nothing here changes a fill.
- Existing results are identical; the one deliberate contract change is the forming row's `NaN` indicator columns.
- Tests use stores written under `tmp_path`; no gateway. Prefer counting spies and `numpy.shares_memory` over timing assertions.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Ordered implementation

### 0. Baseline

- [x] Before changing code, run the benchmark logic (step 5's script, drafted first) on 60 synthetic sessions with the current code. Record the timings in this plan.

  Baseline, `uv run python examples/research/tick_backtest_benchmark.py --sessions 60 --workers 4`, on the development workstation, before any change (`q-core` v2026.10.19, `TickReplay`, `TickWorkerPool`):

  | strategy | workers=1 (s) | workers=4 (s) | trades |
  |---|---|---|---|
  | amend | 0.58 | 0.58 | 354 |
  | phase | 1.16 | 5.68 | 177 |

### 1. Native price source

**Files:** `pyproject.toml`, `uv.lock`, `src/q_backend/backtesting/candle_kernel.py` (`check_engine`), `src/q_backend/research/engine.py`, `src/q_backend/research/tick_store.py` (a session path and interval helper), `src/q_backend/research/intrabar.py` (removed or reduced to the helper).

- [x] Pin the tag published by `./work finish Q-119`, and require `SESSION_TICK_SOURCE`.
- [x] Write failing tests for acceptance 2 and the error mapping.
- [x] Build the source with each bar's `[start_msc, end_msc)` from `naive_local_to_time_msc`, using `TickReplay`'s session rule and `None` for sessions that are not stored. Map `IntrabarDataError` to today's exceptions.
- [x] Run `uv run pytest tests/research tests/backtesting -q` (acceptance 1). Commit.

### 2. Constant-cost frames

**Files:** `src/q_backend/research/adapter.py`, `src/q_backend/research/tick_phase.py`, `tests/research/test_hook_frames.py`.

- [x] Write failing tests for acceptance 3–5.
- [x] Replace `_owned_prefix` copies with positional slices of the prepared frame, with empty `attrs`.
- [x] Build tick frames from a scratch copy of the prepared frame, allocated once per run: write the forming row's market columns, set its other columns to `NaN`, and pass a slice. Restore the row when the bar ends.
- [x] Remove `compute_indicators` from replay, and remove `TickWorkerPool` and the wave logic in `_RuntimeHooks._decide`.
- [x] Run the research suites. Commit.

### 3. Parallel day-trade sessions

**Files:** `src/q_backend/research/engine.py`, `src/q_backend/research/progress.py`, `tests/research/test_parallel_sessions.py`.

- [ ] Write a failing test for acceptance 6.
- [ ] After preparation, split the frame into contiguous session blocks. Run each block's kernel chunk in a `forkserver` process pool. The payload is the strategy, the prepared frame and the source arguments, and each worker builds its own source.
- [ ] Merge trades, rejected entries and progress in session order. `force_close_at_end` applies only to the last block.
- [ ] Keep `day_trade=False` sequential. Commit.

### 4. Causality check

**Files:** `src/q_backend/research/engine.py`, `tests/research/test_backtest.py`.

- [ ] Write a failing test for acceptance 7, then add `check_causality`. Commit.

### 5. Docs, examples and timings

**Files:** `docs/research-library.md`, `examples/research/stop_target_backtest.py`, `examples/research/tick_backtest_benchmark.py`, `tests/research/test_examples.py`.

- [ ] Document as specified. Update the existing example, and add the benchmark and its test.
- [ ] Run the benchmark on 60 synthetic sessions with `workers` 1 and 4. Record the timings beside the baseline in this plan. Commit.

## Review focus

- No hook frame copies the history. Writes from hooks are absorbed by copy-on-write and never reach the prepared frame or the scratch row of a later tick.
- The forming row never carries the completed candle's values.
- Parallel blocks never split a session. The merged result is order-identical to a sequential run.
- Error types and messages are unchanged for missing sessions, empty intervals and mismatches.
- `TickReplay`, `TickWorkerPool` and their tests are removed, not left unused. `workers` docs describe sessions, not ticks.

## Validation and handoff

- [ ] Run `uv run pytest tests/research tests/backtesting tests/execution -q`, `uv run ruff check src tests examples/research` and `uv run black --check` on the changed paths.
- [ ] Record the commands actually run, their results and the benchmark timings in this plan.
- [ ] Run `./work board set Q-121 in-review -m "<changes; checks and results; timings; follow-ups>"`.

`make contracts-check` is not needed: this task changes no contract.
