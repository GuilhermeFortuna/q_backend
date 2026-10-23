# Q-128 implementation plan: Explicit exit price and removal of phase in research hooks

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-128 --agent <agent> --worktree` after written-plan approval, and only once Q-127 is released and pinned.

**Goal:** no `phase` in strategies; a priced close fills at a traded price inside the bar.
**Spec:** [Specification](../specs/Q-128-explicit-exit-price-research-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Pin the Q-127 release tag in `pyproject.toml` (`q-core`) and resolve it with `uv sync`. Do not edit `uv.lock` by hand.
- Test with synthetic data (`tmp_path` tick stores). No gateway, Docker, GPU or Wine.

## Ordered implementation

### 1. Pin the q_core release

- [x] Replace the `q-core` tag in `pyproject.toml` with Q-127 release `v2026.10.23` and update `uv.lock` with `uv lock --offline` against the local tagged `q_core` clone. Confirmed the release kernel signature omits the removed callbacks and accepts the callback decision API used for `exit_price`.

### 2. Remove phase

**Files:** `research/hooks.py`, `research/adapter.py`, `research/tick_phase.py`, `research/engine.py`, `research/strategy.py`.

- [x] Add tests for acceptance 1 and 2.
- [x] Stop detecting `phase`; raise `TypeError` for a declared `phase` parameter.
- [x] Remove the screen and tick callbacks, `_tick`, `_screen`, `FormingRow` and `forming_compatible` use. Keep `resolve_workers`.
- [x] Remove the phase-based `requires_ticks` error in `engine.py`.

### 3. Priced close

**Files:** `research/orders.py`, `research/adapter.py`, `research/hooks.py`.

- [x] Add tests for acceptance 3 to 8.
- [x] Give `TradeOrder.close` an optional `price` with validation; keep other kinds' rules.
- [x] Pass the price as `exit_price` in the adapter's decision and require ticks for it.

### 4. TickData

**Files:** `research/tick_data.py`, `tests/research/test_tick_data.py`.

- [x] Remove the tick argument and row cutoff; cutoff is the bar end. Update tests.

### 5. Tests and examples

- [x] Rewrite the phase-based research tests around priced closes and bar-end tick data, with acceptance coverage.
- [x] Update repository-owned strategies found by `git grep -n "phase" -- experiments src examples`.

### 6. Documentation and checks

- [x] Update `docs/research-library.md` and the `strategy.py` docstrings as the spec lists.
- [x] Run tests, lint and formatting checks. Results: full `tests/research tests/backtesting` run: 1,390 passed, 11 skipped, 10 failed; eight failures were blocked by local socket restrictions on gateway/process-worker tests, and two test expectations were corrected and passed in the focused run. Focused Q-128 and adjacent tests: 144 passed; focused worker/progress/trailing-stop tests: 9 passed. `ruff check src tests` and Black check on all 23 changed Python files passed. `uv lock --check --offline` passed. `uv sync --frozen --offline` could not finish because the shared uv cache is read-only; Q-127's tagged Python extension was built from the local `q_core` source into `/tmp` for the test run.
- [ ] Set the board task to in review with `./work board set Q-128 in-review -m "<summary, checks and results>"`.
