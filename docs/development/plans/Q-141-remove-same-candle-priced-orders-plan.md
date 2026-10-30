# Q-141 implementation plan: Remove same-candle priced orders

> **For implementation agents:** Read the linked specification and `q_backend`'s instructions. Launch only through
> `./work start Q-141 --agent <agent> --worktree` after written-plan approval, after Q-139 is Done, and after the
> author's confirmation is recorded on the issue.

**Goal:** `price=` entries and closes and priced order lists are refused, and every research strategy is
live-placeable.
**Spec:** [Specification](../specs/Q-141-remove-same-candle-priced-orders-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `q_backend` only. Do not change `q_core`.
- Stop and set the task to `blocked` if the issue lacks the author's confirmation.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing.

## Ordered implementation

### 1. Refusal

**Files:** `research/orders.py`, `research/adapter.py`, `tests/research/test_priced_orders_removed.py` (new).

- [x] Write failing tests for acceptance 2, implement the refusals with replacement-naming messages, and commit.

### 2. Tests and kinds

**Files:** tests under `tests/research/` that use `price=` on entries or closes (for example
`test_priced_entries.py`, `test_priced_entries_after_exit.py` and `test_stop_target_backtest.py`), and the
order-kind classification.

- [x] Rewrite each test to its replacement form, or remove it when the replacement is already covered by Q-139's
  fixtures, and list each one for the board message. Keep the new-form halves of the equivalence fixtures
  unchanged.
- [x] Remove the priced kinds from classification. Run `tests/research tests/backtesting tests/execution`.
  Commit.

### 3. Guide

**Files:** `docs/research/user_guide/execution.rst`, `live_placeable_orders.rst`, `backtesting.rst`, and the
`TradeOrder` docstrings.

- [x] Remove the priced-order documentation and the deprecation notes. Commit.

## Verification

- `uv run pytest tests/research tests/backtesting tests/execution`, and `ruff`/`black` on the changed files.
- `./work board set Q-141 in-review -m "<summary; the rewritten or removed tests; checks>"`.
