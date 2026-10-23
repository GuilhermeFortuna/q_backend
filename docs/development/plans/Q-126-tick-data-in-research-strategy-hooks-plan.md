# Q-126 implementation plan: Tick data in research strategy hooks

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-126 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** `compute_indicators`, `entry_strategy` and `exit_strategy` can read ticks on demand through a `TickData` object that never returns ticks after the hook's decision moment.
**Architecture:**
- `research/tick_data.py` holds `TickData`: a store, the backtest frame's bar windows, a cutoff, and a shared one-session cache.
- `detect_hook_call` also reports whether a hook takes `ticks` and whether that parameter is required.
- The adapter holds the store and builds a `TickData` per call with that call's cutoff.

**Tech stack:** Python 3.12, pandas 3 (copy-on-write), numpy, pyarrow, pytest.
**Spec:** [Specification](../specs/Q-126-tick-data-in-research-strategy-hooks-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Fills and tick matching stay in `q_core`. Nothing here changes a fill or a result for strategies that do not declare `ticks`.
- Bar windows come from one helper shared with `native_tick_source`, so `TickData.bar` and the kernel agree on every candle's interval.
- Tests use stores written under `tmp_path`; no gateway. Prefer counting spies over timing assertions.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Ordered implementation

### 1. `TickData`

**Files:** `src/q_backend/research/tick_data.py` (new), `src/q_backend/research/intrabar.py`, `src/q_backend/research/tick_store.py`, `src/q_backend/research/__init__.py`, `tests/research/test_tick_data.py` (new).

- [ ] Write failing tests for acceptance 2, 3 (time cutoffs, using `TickData` directly) and 6.
- [ ] Make `_bar_end` a shared helper and use it from `native_tick_source` and `TickData`.
- [ ] Add a `TickStore` method that returns one session's full columns as a frame, sorted stably by `time_msc`, so `TickData` and `TickStore.ticks` build frames the same way.
- [ ] Implement `TickData` with `bar`, `between` and `cutoff`. Keep the session cache in a small object shared by every instance of a run, and drop it when pickling. A tick-phase cutoff is a row position within the session.
- [ ] Export `TickData` lazily from `q_backend.research`, as the other names are. Commit.

### 2. Hook signatures and enforcement

**Files:** `src/q_backend/research/hooks.py`, `src/q_backend/research/strategy.py`, `src/q_backend/research/adapter.py`, `src/q_backend/research/engine.py`, `tests/research/test_tick_data_hooks.py` (new).

- [ ] Write failing tests for acceptance 1.
- [ ] Extend `HookCall` with how `ticks` is accepted and whether it is required, and detect it for `compute_indicators` too. Signatures that do not bind keep raising the current `TypeError`, with the message listing `ticks`.
- [ ] Replace the adapter's `ticks_available` flag with the store (`None` when absent), and keep `_require_ticks` working from it. Widen `requires_ticks` and the `backtest()` error to name the hook that needs ticks.
- [ ] Document the parameter in `strategy.py`. Commit.

### 3. Per-call cutoffs

**Files:** `src/q_backend/research/adapter.py`, `src/q_backend/research/engine.py`, `tests/research/test_tick_data_hooks.py`.

- [ ] Write failing tests for acceptance 3 (through `backtest()`), 4, 5 and 7.
- [ ] Pass a `TickData` cut off at the frame's end to `compute_indicators`, and one cut off at each prefix's end in `_check_causality`.
- [ ] In `_RuntimeHooks`, pass the bar-phase cutoff in `strategy`, the bar's open in `_screen`, and in `_tick` the stored row of the bar's `tick`-th trade. Map that index to a row with the session's trade mask, computed once per session.
- [ ] Run `uv run pytest tests/research -q` (acceptance 8). Commit.

### 4. Documentation

**Files:** `docs/research-library.md`, `src/q_backend/research/engine.py` (`ticks` docstring).

- [ ] Write the "Tick data in hooks" subsection as specified, and check its examples by running them against a `tmp_path` store in a test or a scratch run. Commit.

## Review focus

- No path returns a row after the cutoff: `between` clipping, a `bar` that straddles the cutoff, and same-millisecond rows in the tick phase.
- Strategies without `ticks` take the same code path and give the same results as before.
- A strategy that declares `ticks` but never calls it reads no tick file.
- `TickData` and the kernel use the same bar windows.
- The required-ticks error is raised before any hook runs.

## Validation and handoff

- [ ] Run `uv run pytest tests/research tests/backtesting -q`, `uv run ruff check src tests` and `uv run black --check` on the changed paths.
- [ ] Record the commands actually run and their results in this plan.
- [ ] Run `./work board set Q-126 in-review -m "<changes; checks and results; follow-ups>"`.

`make contracts-check` is not needed: this task changes no contract.
