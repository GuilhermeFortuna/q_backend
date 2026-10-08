# Q-104 implementation plan: Stop, target and lazy intrabar exits

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-104 --agent <agent> --worktree` after written-plan approval and with Q-102 and Q-103 Done. Implement natively; delegation requires separate authorization.

**Goal:** Entry orders carry protective levels, and `exit_strategy()` can confirm
exits inside a candidate candle using lazily loaded `TickStore` prices.
**Architecture:** Extend the shared Rust/PyO3 loop with screen/tick callbacks and
runtime entry-level transport. Resolve protective/custom exits chronologically.
The research adapter rebuilds causal partial candles and recomputes indicators
only during candidate replay; results record the actual tick fill.
**Tech stack:** Rust, PyO3, Python 3.12, pandas, numpy, `q_core`, pytest.
**Spec:** [Specification](../specs/Q-104-stop-and-target-orders-in-research-backtests-spec.md)
**Status:** In Progress. Core step 0 is implemented, verified and published; continue backend steps 1–4.

## Global constraints

- Execution ordering and protective fill rules live in `q_core`. User exit hooks
  define screen/confirmation conditions; Python does not implement a second ledger.
- Levels and the price source reach the kernel only from `q_backend.research`. `BacktestEngine` callers that pass neither take today's path unchanged.
- Results for strategies without levels or phase-aware exits are byte-for-byte
  unchanged, including the empty-frame path.
- `exit_time` stays a timestamp of the frame's index.
- Tests use stores written under `tmp_path`; no gateway.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.
- Preserve the released position-aware hooks. Explicit keyword-capable `phase`
  opts exit hooks into `screen`, `tick` and `bar` calls; other signatures keep
  today's closed-bar behavior. Screening is execution-free and conservative.
- Final-candle information may screen, but cannot enter causal replay frames,
  indicator values or execution state. Never backdate a screen result as a fill.
- The required core extension is published as `v2026.10.08.3`; its branch and
  worktree use Q-104's canonical name. Q-102 remains Done. Continue the remaining
  backend implementation through final verification and In Review; no additional
  core scope approval or intermediate release handoff is needed.

## Review focus

- The bar interval handed to the store is `[index[i], index[i + 1])`, and for the last bar it ends with that bar's calendar day, so the timeframe is never guessed.
- The frame-versus-store check runs on every bar whose prices are loaded and its error names the bar.
- A level set on a bar whose entry is later skipped by the position cap neither opens a trade nor appears in `rejected_entries`.
- `ledger_to_registry` still asserts that kernel and registry profit agree for protective exits.
- The public export list and the import test agree, and importing `q_backend.research` stays inert.
- `publish()` output is unchanged for a result that has the new columns.
- A high/low crossing can reverse before close; screens must not miss it. For
  conditions OHLC cannot exclude, screen every open-position candle.
- Partial current-row unavailable fields are `NaN`; replay indicators are freshly
  computed. No preview state or shared frame/index backing leaks into replay.
- One chronological walk resolves custom and protective candidates, requesting
  each interval once; stop once no positions remain. Same-tick protective priority
  and the exclusion of a newly opened position's own entry tick are explicit gates.

## Ordered implementation

### 0. Extend the core for lazy custom exits

**Repository:** `q_core` candle loop, input/decision types, PyO3 binding and focused
Rust/Python gates. Its existing callback runs after protective execution and is
insufficient for a custom exit earlier in the same candle.
**Interfaces:** additive optional `exit_screen_callback(bar, positions) -> bool`
and `exit_tick_callback(bar, tick_index, time_us, price, positions) -> bool` in the
candle binding. A screen `True` requests replay; a tick `True` closes at that tick.
Snapshots use the existing binding shape; tick callbacks receive one observed
price at a time. Runtime entry orders must also transport their protective levels,
preserving the old strategy callback return for callers without levels.

- [x] Core/binding tests cover lazy union screening, one source request,
  chronological custom/protective competition, ties, snapshots, entry-tick
  exclusion, source ordering and old callback compatibility.
- [x] Callbacks are integrated into the shared loop; screen after queued fills,
  combine protective/custom processing in tick order, then evaluate bar decisions.
- [x] Core changes committed as `e4ed25d` and `095c2ce`. Fresh `make check` passed
  on the final source, including Rust, fixtures, parity isolation, Python wheel,
  Qt harness and contracts gates. The publication pre-push CI also passed.
- [x] Published `v2026.10.08.3` at
  `095c2ce507278dcb814b620db1087fa5b77393a9` on 2026-10-08. The intermediate core
  fast-forward/tag/push was authorized by the user's instruction to remove the
  release gate so the assigned agent can finish Q-104. Q-104 itself stays open.

**Final binding interface:** `strategy_callback` returns either the original
`(entry, exit_long, exit_short, strength)` or six values
`(entry, exit_long, exit_short, strength, stop_price, target_price)`. `NaN` means
no level; six-value decisions override static levels. Screen/tick callbacks must
be supplied together, return Python booleans, and use the signatures above.
There is no separate custom-exit capability constant; check the supported
callback parameters in addition to `PROTECTIVE_ORDERS` when adopting the binding.

**Continuation:** Use the published tag, complete all remaining steps and their
acceptance checks, and report Q-104 In Review only after the backend work passes.
The `q_core`, `q_backend` and compatibility-record branches/worktrees share the
canonical name `Q-104-stop-target-and-lazy-intrabar-exits-in-research-backtests`.
Record backend adoption and its evidence in the existing Q-104 `q_contracts`
worktree. Final task merging remains the normal human review/finish step.

### 1. Pin the kernel and extend the order

**Files:** Modify `pyproject.toml`, `uv.lock`, `src/q_backend/backtesting/candle_kernel.py` (`check_engine`), `src/q_backend/research/orders.py`, `tests/research/test_research_strategy.py` and the `check_engine` test.
**Interfaces:** `TradeOrder.buy(*, stop_loss=None, take_profit=None)`, `TradeOrder.sell(...)`.

- [x] Move the pin from `v2026.10.08.2` to published `v2026.10.08.3`.
  `uv lock` and `uv sync` passed; the lock resolves
  `095c2ce507278dcb814b620db1087fa5b77393a9`. Installed-module inspection confirmed
  `PROTECTIVE_ORDERS` and both screen/tick callback parameters. Package version is
  `2026.10.8` and contracts revision is `998a50570905524bfb9af0465a725b170f2970df`.
- [x] Update `check_engine` to require protective and custom intrabar callback
  capabilities, preserving positions.
- [x] Add failing tests for spec acceptance item 9 and for `check_engine` rejecting a module without `PROTECTIVE_ORDERS`.
- [x] Implement, run `uv run pytest tests/research/test_research_strategy.py tests/backtesting/test_candle_kernel_bridge.py -q` and confirm green. Commit this unit.

### 2. Carry levels to the kernel

**Files:** Modify `src/q_backend/backtesting/signal_columns.py`, `src/q_backend/backtesting/candle_kernel.py` (`run_chunk`, `ChunkRun`, `ledger_to_registry`), `src/q_backend/backtesting/engine.py`, `src/q_backend/backtesting/models.py` (`Trade` gains optional `exit_tick_time`, `stop_loss`, `take_profit`) and `src/q_backend/research/adapter.py`; add `tests/research/test_stop_target_backtest.py`.
**Interfaces:** two optional level columns in the signal contract; `BacktestEngine(..., intrabar=None)` forwarded to `run_chunk`.

- [x] Add failing tests for spec acceptance items 1 to 3 and 6 to 8 with a counting store.
- [x] Transport levels through static columns and actual runtime position-aware
  decisions; do not infer fills in Python. Map protective and custom tick times
  to `exit_tick_time` in the frame timezone.
- [x] Implement the price callable in `src/q_backend/research/engine.py`: bar interval, `TickStore.trade_prices`, the frame-versus-store check, and the missing-session error.
- [x] Run `uv run pytest tests/research tests/backtesting -q` and confirm green, including the unchanged candle goldens. Commit this unit.

### 2b. Adapt phase-aware exit screening and causal replay

**Subsystems:** research strategy/adapter, prepared engine and candle bridge.
**Interface:** `exit_strategy(frame[, positions], *, phase="bar")`, returning a
close request or `None` in every phase; explicit `phase` selects the capability.

- [x] Add failing tests for spec acceptance 11–17, including a reversed price
  crossing, false-positive screens, partial-candle indicator crossing, future-data
  isolation, optional positions, `**kwargs` detection and contextual errors.
- [x] Bind screen/tick callbacks to isolated raw history. Screen on the full
  candidate candle; reconstruct observed OHLC/tick count for replay, set unavailable
  current-row fields `NaN`, and recompute indicators before each tick hook call.
- [x] Validate phases/actions and keep screen/replay free of evolving user state.
  Never invoke entry hooks per tick. Evaluate bar-phase exit before entry afterward
  with the same post-fill position snapshot. Preserve legacy signature behavior.
- [x] Verify no reads/calls for screened-out or flat candles, one interval load for
  overlapping candidates, exact first-confirmed fill and chronological precedence.
- [x] Run focused research/bridge/engine tests and commit this unit.

### 3. Expose results and errors

**Files:** Modify `src/q_backend/research/engine.py`, `src/q_backend/research/results.py`, `tests/research/test_stop_target_backtest.py` and `tests/research/test_backtest.py`.
**Interfaces:** `backtest(..., ticks=None)`; `BacktestResult.rejected_entries`; `trades` columns `exit_tick_time`, `stop_loss`, `take_profit`.

- [x] Add failing tests for spec acceptance items 4, 5 and 11–17: missing stores
  for phase-aware hooks, candidate-only reads, OHLC consistency, empty intervals,
  same-session interval ends and empty result schemas.
- [x] Implement argument checks and complete normal/empty result schemas; record
  custom tick exits with the signal-exit reason and actual tick timestamp. Verify
  realized equity/costs for both mechanisms and preserve `rejected_entries`.
- [x] Run `uv run pytest tests/research tests/execution -q` and confirm green with unchanged evaluator tests. Commit this unit.

### 4. Document

**Files:** Create `examples/research/stop_target_backtest.py`; modify `docs/research-library.md` and `tests/research/test_examples.py`.

- [x] Document entry levels plus phase-aware screening/replay, conservative range
  checks, causal indicators, tick prices/costs and legacy/bar-phase next-open closes.
  Extend the example/test to prove an exit inside its candle at a price different
  from the next open, and no replay for a screened-out candle.
- [x] Run `uv run pytest tests/research/test_examples.py -q`. Commit docs and example.

## Verification and handoff

Commands run on the final tree:

- `uv run pytest tests/research tests/backtesting tests/execution -q`: 1058 passed, 11 skipped.
- `uv run pytest tests/api -k "payload or backtest"`: 93 passed. The pinned job payload fixture is unchanged because the new trade fields are `exclude=True` on the model.
- `uv run ruff check src tests examples/research`: passed. `uv run black --check` on the changed Python files: passed.
- `uv run pytest tests -k "backtest or publish or trade or candle or research"` outside the three suites: 257 passed and one payload test failed before the `exclude=True` fix, which now passes. Seven integration tests need Postgres/Redis and were not run.

Known deviation: q_core rejects a wrong-side entry before it applies sizing and the position cap, so an entry that is both on the wrong side and capped is reported in `rejected_entries`. The spec says it should not be. Fixing this needs a q_core change that moves the rejection after sizing, and it is left as a follow-up.

- [x] Run `uv run pytest tests/research tests/backtesting tests/execution -q`, `uv run ruff check src tests examples/research` and `uv run black --check` on the changed paths.
- [x] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [x] Record core/binding verification and the published additive release. Ensure
  the spec, plan and issue agree on the phase API and acceptance coverage. This
  scope revision does not change the board status or dependency completion.
- [x] Use `./work board set Q-104 in-review -m "<changes; checks and results; follow-ups>"`.

`make contracts-check` is not needed: this task changes no contract.
