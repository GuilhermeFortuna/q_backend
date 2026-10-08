# Q-103 implementation plan: Research tick store

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-103 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** A file-only, per-session tick store that a research script syncs from the MT5 gateway and reads ticks and bars from offline.
**Architecture:** One new research module that reuses the gateway client, the timezone helpers and the bar frame contract; one Parquet file per symbol and day.
**Tech stack:** Python 3.12, pandas, pyarrow, pytest.
**Spec:** [Specification](../specs/Q-103-research-tick-store-spec.md)
**Status:** implemented on branch `Q-103-research-tick-store`.

## Global constraints

- Files only: no SQLAlchemy engine, lake catalog, Redis or `market_data.tick_cache` use.
- Reuse `RemoteMt5Client`, `resolve_gateway_url`/`resolve_gateway_token`, `market_data.timezone`, `validate_bars_frame` and `attach_metadata`; add no second timezone conversion or bar schema.
- A stored day is immutable; nothing in this task rewrites or deletes one.
- No machine-specific path anywhere. The default root is relative to the project root, as `Q_TICK_CACHE_DIR` resolves.
- Tests use a mocked client and `tmp_path` only.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- An empty or unsettled response can never leave a file behind, and is requested again on the next `sync`.
- The current exchange day is excluded using one captured current time per `sync` call.
- Bars from the one-minute cache equal bars from the ticks, including `tick_volume`, `real_volume` and the first and last bar of a session.
- No bar is produced for an interval without a price, within a session or across sessions.
- `trade_prices` uses the half-open interval `[start, end)` and stored row order for equal timestamps.
- Import and construction stay inert.

## Ordered implementation

### 1. Store layout and synchronising

**Files:** Create `src/q_backend/research/tick_store.py` and `tests/research/test_tick_store.py`; modify `src/q_backend/research/__init__.py` and `tests/research/test_imports.py`.
**Interfaces:** `TickStore(symbol, *, root=None)`, `sync(...) -> TickSyncReport`, `sessions()`.

- [x] Add failing tests for spec acceptance items 1, 2 and 6, with a fake client that returns scripted row counts per day and a frozen current time.
- [x] Run `uv run pytest tests/research/test_tick_store.py tests/research/test_imports.py -q` and confirm the new cases fail for the expected reason.
- [x] Implement root resolution, the day walk, the two-request settle check, the atomic write and the report. Export `TickStore` lazily like the other public names.
- [x] Re-run until green. Commit this unit.

### 2. Reading ticks and bars

**Files:** Modify `src/q_backend/research/tick_store.py` and `tests/research/test_tick_store.py`. Factor the frame construction shared with `load_ticks` in `src/q_backend/research/data.py` into one private helper used by both; do not change `load_ticks` behaviour.
**Interfaces:** `ticks(...)`, `bars(timeframe, ...)`, `trade_prices(start, end)`.

- [x] Add failing tests for spec acceptance items 3, 4 and 5 on a handwritten two-session fixture with an intraday gap and equal-timestamp rows.
- [x] Implement `ticks`, the per-session one-minute cache, the aggregation to coarser timeframes and `trade_prices` with its one-session memory.
- [x] Run `uv run pytest tests/research -q` and confirm it passes, including the unchanged `load_ticks` and `resample_ticks` tests. Commit this unit.

### 3. Document

**Files:** Create `examples/research/sync_ticks.py`; modify `docs/research-library.md`, `README.md`, `.env.example` and `tests/research/test_examples.py`.

- [x] Write the guide section, the example and the environment entries as the spec lists. Add a mocked example test that asserts the arguments forwarded to `sync`.
- [x] Run `uv run pytest tests/research/test_examples.py -q`. Commit docs and example.

## Verification and handoff

- [x] Run `uv run pytest tests/research -q`, `uv run ruff check src/q_backend/research tests/research examples/research` and `uv run black --check` on the same paths.
- [x] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-103 in-review -m "<changes; checks and results; follow-ups>"`.

**Checks run (2026-10-08):**

- `uv run pytest tests/research -q` → 189 passed
- `uv run ruff check src/q_backend/research tests/research examples/research` → all checks passed
- `uv run black --check src/q_backend/research tests/research examples/research` → passed after formatting

The first real synchronisation of a symbol is an operator step on the workstation after merge and is not part of the automated checks.
