# Q-120 implementation plan: Resting orders and stop amendments in research backtests

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-120 --agent <agent> --worktree` after written-plan approval and with Q-118 Done. Implement natively; delegation requires separate authorization.

**Goal:** `ResearchStrategy` hooks place stop and limit entries and amend open levels; the kernel fills them from ticks.
**Architecture:**
- `TradeOrder` and `ResearchPosition` gain the new fields.
- The adapter always uses runtime callbacks and returns Q-118's `dict` decisions with `snapshot_levels=True`.
- `run_chunk` and the results map the new ledger columns.

**Tech stack:** Python 3.12, pandas, numpy, `q_core`, pytest.
**Spec:** [Specification](../specs/Q-120-resting-orders-and-stop-amendments-in-research-backtests-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Order matching, expiry and amendment semantics live in `q_core`; Python does not reimplement or infer fills.
- Existing strategies produce identical `trades`, `metrics`, `equity` and `data`.
- Only `q_backend.research` uses the new kernel inputs. `BacktestEngine` callers that do not pass them take today's path.
- Tests use stores written under `tmp_path`; no gateway.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Ordered implementation

### 1. Pin the kernel

**Files:** `pyproject.toml`, `uv.lock`, `src/q_backend/backtesting/candle_kernel.py` (`check_engine`) and its test.

- [ ] Move the `q-core` pin to the tag published by `./work finish Q-118`. Run `uv lock` and `uv sync`.
- [ ] Require `RESTING_ORDERS` and the `snapshot_levels` parameter in `check_engine`, with a failing test first. Commit.

### 2. Orders and positions

**Files:** `src/q_backend/research/orders.py`, `positions.py`, `hooks.py`, `tests/research/test_research_strategy.py`.

- [ ] Write failing tests for acceptance 4. Add `kind`, `expire_bars`, the four constructors, `amend` and `cancel`, with validation.
- [ ] Make `invoke_hook` allow actions per hook and phase as the spec lists.
- [ ] Add `stop_loss` and `take_profit` to `ResearchPosition`. Commit.

### 3. One runtime path carrying orders

**Files:** `src/q_backend/research/adapter.py`, `src/q_backend/backtesting/candle_kernel.py` (`run_chunk`, `ChunkRun`, `ledger_to_registry`), `src/q_backend/backtesting/models.py`, `tests/research/test_resting_orders_backtest.py`.

- [ ] Write failing tests for acceptance 1, 2, 3 and 5.
- [ ] Remove the precomputed-decision loop from `compute_indicators`, so every strategy uses `_RuntimeHooks`.
- [ ] Return Q-118's `dict` from `_RuntimeHooks.strategy`: entry kind, price, expiry, cancel and amendment. Build positions from 7-field snapshots.
- [ ] Map `entry_kind`, `entry_time_us` and the final levels onto `Trade`, keeping the new model fields out of serialized payloads as Q-104 did.
- [ ] Run `uv run pytest tests/research tests/backtesting -q`, including the unchanged candle goldens (acceptance 6). Commit.

### 4. Results, docs and example

**Files:** `src/q_backend/research/results.py`, `docs/research-library.md`, `examples/research/resting_orders_backtest.py`, `tests/research/test_examples.py`.

- [ ] Add the new `trades` columns and the complete empty schema.
- [ ] Document as specified, and add the example and its test (acceptance 7).
- [ ] Run `uv run pytest tests/research tests/execution -q`. Commit.

## Review focus

- No Python-side fill logic: every new behaviour is a decision field passed to the kernel.
- The removed precomputed loop leaves no behaviour change for hooks without `positions`. Check stateful hooks too: the call order per bar stays exit then entry.
- `amend` is rejected in the `screen` and `tick` phases, and ignored while flat.
- The parity test compares tick times and prices, not only bars.
- `publish()` output is unchanged for a result with the new columns.

## Validation and handoff

- [ ] Run `uv run pytest tests/research tests/backtesting tests/execution -q`, `uv run ruff check src tests examples/research` and `uv run black --check` on the changed paths.
- [ ] Record the commands actually run and their results in this plan.
- [ ] Run `./work board set Q-120 in-review -m "<changes; checks and results; follow-ups>"`.

`make contracts-check` is not needed: this task changes no contract.
