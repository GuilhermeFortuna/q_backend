# Q-140 implementation plan: Finer decision timeframes for research strategies

> **For implementation agents:** Read the linked specification and `q_backend`'s instructions. Launch only through
> `./work start Q-140 --agent <agent> --worktree` after written-plan approval and after Q-136 is Done.

**Goal:** Strategies decide on M1 with indicators from completed coarser bars, both in backtests and in forward
execution.
**Spec:** [Specification](../specs/Q-140-finer-decision-timeframes-for-research-strategies-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `q_backend` only. Reuse the B3 session resampling that `TickStore.bars` uses (`research/tick_store.py`,
  `research/frame.py`); do not write a second resampler.
- Do not edit expected values in existing tests.
- Tests use hand-built bars and the fake edge. No MT5, Docker, GPU or desktop.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing.

## Ordered implementation

### 1. Closed-bar context

**Files:** `research/context.py` (new), `research/__init__.py`, `tests/research/test_closed_bars.py` (new).

- [x] Write failing tests for acceptance 1 and 3, implement `closed_bars`, and commit.
  The causality check cuts prefixes at any bar for strategies that declare `decision_timeframe`;
  other strategies keep the session-end cuts, which the existing causality test pins.

### 2. Before/after match

**Files:** `tests/research/test_decision_timeframe_equivalence.py` (new).

- [x] Write the M5 and M1 fixture strategies of acceptance 2, and require identical trades.
  Until `compare_results` (Q-139) is on `development`, the test compares the trade fields directly.

### 3. Decision timeframe in the stack and execution

**Files:** `research/strategy_files.py` (record `decision_timeframe`), `research/strategy_file_runs.py`,
`execution/validation.py`, `execution/research_evaluator.py`, `tests/execution/test_research_evaluator.py`.

- [x] Write failing tests for acceptance 4.
- [x] Record the attribute on the version, check the run and deployment timeframe, and allow M1 for declaring
  versions only. Run `tests/execution/`. Commit.
  The attribute is read from the version's frozen source at deploy time, so no column or migration is added;
  run parameters exclude it.

### 4. Guide

**Files:** `docs/research/user_guide/decision_timeframes.rst`, linked from the user guide index.

- [x] Document `closed_bars`, `decision_timeframe`, and an example "exit on a one-minute close back inside the
  range". Commit.

## Verification

- `uv run pytest tests/research tests/execution`, and `ruff`/`black` on the changed files.
- `./work board set Q-140 in-review -m "<summary; checks and results>"`.
