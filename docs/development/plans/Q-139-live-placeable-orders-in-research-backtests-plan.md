# Q-139 implementation plan: Live-placeable orders in research backtests

> **For implementation agents:** Read the linked specification and `q_backend`'s instructions. Launch only through
> `./work start Q-139 --agent <agent> --worktree` after written-plan approval, after Q-138 is released and after
> Q-136 is Done.

**Goal:** The research library offers OCO groups, timed exits and expiry, and placement latency. It deprecates
same-candle priced orders, compares backtests trade by trade, and proves that the author's ported strategies
match their originals.
**Spec:** [Specification](../specs/Q-139-live-placeable-orders-in-research-backtests-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `q_backend` only. Pin the Q-138 release.
- Do not edit expected values in existing tests. A changed existing result is a regression to report.
- Never commit the author's strategies or their ports. Read them with
  `git show research/experiments:experiments/strategies/<file>` into a scratch directory outside every repository.
- Tests use hand-built bars and a `tmp_path` tick store. No MT5, worker, Docker, GPU or desktop.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing.

## Ordered implementation

### 1. Pin and baseline

**Files:** `pyproject.toml`, `uv.lock`.

- [ ] Pin the Q-138 tag. Run `tests/research/` and `tests/backtesting/`, which must pass unchanged. Commit.
- [ ] In the scratch directory, run the three author strategies on their `main()` setups (same symbols, ranges
  and tick stores) and save each `BacktestResult`'s trades as the baseline.

### 2. Comparison tool

**Files:** `research/compare.py` (new), `research/__init__.py`, `tests/research/test_compare_results.py` (new).

- [ ] Write failing tests for acceptance 6, implement `compare_results` and its report, and commit.

### 3. New orders and latency

**Files:** `research/orders.py`, `research/adapter.py`, `research/engine.py`, `backtesting/candle_kernel.py`,
`research/strategy_file_runs.py`, `api/schemas/backtest.py` (`latency_ms`), and the Q-136 order-kind
classification, plus `tests/research/test_oco_orders.py`, `test_timed_orders.py` and `test_latency.py` (new).

- [ ] Write failing tests for acceptance 3 and 5.
- [ ] Implement the orders, pass them to the kernel, and classify the new kinds. Commit.

### 4. Equivalence fixtures and deprecation

**Files:** `research/orders.py`, `research/adapter.py`, `tests/research/test_live_placeable_equivalence.py`
(new), `tests/research/test_priced_deprecation.py` (new).

- [ ] Write fixture strategy pairs for each migration-table row, and test acceptance 2 with `compare_results`.
- [ ] Add the deprecation warning (acceptance 4), and the "not deployable" marker in the strategy-file listing.
  Commit.

### 5. Guide

**Files:** `docs/research/user_guide/live_placeable_orders.rst` (new) and `index.rst`, `execution.rst`,
`strategy_files.rst`.

- [ ] Write the migration guide with the table, the known differences and worked examples. Commit.

### 6. Port the author's strategies

- [ ] In the scratch directory, port the three strategies with the guide only, run each port on the baseline
  setup, and compare. Attribute every difference to a known one; an unexplained difference blocks the task.
  Record the reports for the board message.

## Verification

- `uv run pytest tests/research tests/backtesting`, and `ruff`/`black` on the changed files.
- `./work board set Q-139 in-review -m "<summary; checks; the three comparison reports>"`.
