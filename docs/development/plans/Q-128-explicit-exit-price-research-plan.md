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

- [ ] Replace the `q-core` tag in `pyproject.toml` with the Q-127 release and run `uv sync --frozen` after updating the lock through `uv lock`. Confirm the kernel rejects the removed callbacks.

### 2. Remove phase

**Files:** `research/hooks.py`, `research/adapter.py`, `research/tick_phase.py`, `research/engine.py`, `research/strategy.py`.

- [ ] Write failing tests for acceptance 1 and 2.
- [ ] Stop detecting `phase`; raise `TypeError` for a declared `phase` parameter.
- [ ] Remove the screen and tick callbacks, `_tick`, `_screen`, `FormingRow` and `forming_compatible` use. Keep `resolve_workers`.
- [ ] Remove the phase-based `requires_ticks` error in `engine.py`. Commit.

### 3. Priced close

**Files:** `research/orders.py`, `research/adapter.py`, `research/hooks.py`.

- [ ] Write failing tests for acceptance 3 to 8.
- [ ] Give `TradeOrder.close` an optional `price` with validation; keep other kinds' rules.
- [ ] Pass the price as `exit_price` in the adapter's decision and require ticks for it. Commit.

### 4. TickData

**Files:** `research/tick_data.py`, `tests/research/test_tick_data.py`.

- [ ] Remove the tick argument and row cutoff; cutoff is the bar end. Update tests.
- [ ] Commit.

### 5. Tests and examples

- [ ] Rewrite `tests/research/test_intrabar_exit_hooks.py`, `tests/research/test_tick_phase.py` and the phase cases in `tests/research/test_tick_data_hooks.py` around priced closes, so each acceptance item has a test.
- [ ] Update repository-owned strategies found by `git grep -n "phase" -- experiments src examples`.
- [ ] Commit.

### 6. Documentation and checks

- [ ] Update `docs/research-library.md` and the `strategy.py` docstrings as the spec lists.
- [ ] Run `uv run pytest tests/research tests/backtesting -q`, `uv run ruff check src tests` and `uv run black --check` on the changed paths. Record results in this plan.
- [ ] Set the board task to in review with `./work board set Q-128 in-review -m "<summary, checks and results>"`.
