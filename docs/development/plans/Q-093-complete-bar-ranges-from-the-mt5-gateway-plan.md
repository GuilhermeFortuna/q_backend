# Q-093 implementation plan: Complete bar ranges from the MT5 gateway

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-093 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** A bar request returns every bar the terminal supplies for its range, or fails clearly.
**Architecture:** The gateway reports truncation in response metadata; the remote client follows the continuation rule; the native client drops its cap.
**Tech stack:** Python 3.12, numpy, httpx, pytest, the stdlib gateway script and its fake MetaTrader 5 test module.
**Spec:** [Specification](../specs/Q-093-complete-bar-ranges-from-the-mt5-gateway-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Follow the Q-092 contract exactly: metadata keys `truncated` and `max_bars`, continuation from the last returned bar time plus one second, unchanged `end`.
- The gateway script stays within its obligations: standard library, `MetaTrader5` and `numpy` only, and no import from `src/`.
- Do not change bar arrays, dtypes, timezone handling, `/v1/ohlcv/recent`, ticks or trades.
- Never hand-edit vendored contract code; use `make contracts`.
- Tests use `tests/gateway/fake_metatrader5.py` and mocked HTTP. No Wine, live terminal, Docker, GPU or desktop run.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- No path returns a cut range without `truncated: true`, including a cut inside a chunk and a cut exactly on a chunk boundary.
- The client cannot loop forever and cannot duplicate or drop the bar at a page boundary.
- A gateway without metadata fails closed with an actionable message.
- Single-page requests still issue one HTTP call.
- Existing test doubles that build bar archives are updated deliberately, not by loosening the client.

## Ordered implementation

### 1. Pin the Q-092 contract

**Files:** Modify `CONTRACTS_REV`; run `make contracts`.

- [x] Set `CONTRACTS_REV` to the merged Q-092 commit and run `make contracts` then `make contracts-check`.
- [x] Confirm the vendored tree is unchanged and the check is clean. If the commit cannot be fetched from the remote, stop and use the blocked workflow; do not point the Makefile at a local path in a committed change.
- [x] Commit the pin.

### 2. Report truncation from the gateway

**Files:** Modify `gateway/mt5_gateway.py` and `tests/gateway/test_mt5_gateway.py`; extend `tests/gateway/fake_metatrader5.py` only if it cannot already serve a multi-chunk range.
**Interfaces:** `_fetch_ohlcv_chunked` reports whether it stopped at the limit before exhausting the range; `GatewayApp.ohlcv` writes `metadata` with `truncated` and `max_bars`; `/v1/ohlcv/recent` output is unchanged.

- [ ] Add failing tests: under the limit gives `truncated: false`; over a patched small limit gives `truncated: true` and exactly `max_bars` bars, for a cut inside a chunk and for a cut on a chunk boundary; dtypes and raw epochs unchanged; `/v1/ohlcv/recent` has no `metadata` entry.
- [ ] Run `uv run pytest tests/gateway/test_mt5_gateway.py -q` and confirm the new cases fail on the missing metadata.
- [ ] Implement the metadata and update the module docstring's wire-contract notes.
- [ ] Run `uv run pytest tests/gateway -q` and confirm it passes.
- [ ] Commit this unit.

### 3. Follow the continuation rule in the remote client

**Files:** Modify `src/q_backend/market_data/clients/remote.py` and `tests/market_data/test_remote_client.py`; update other tests whose mocked gateway bar archives lack metadata.
**Interfaces:** `get_ohlcv` and `get_ohlcv_columnar` keep their signatures and return types. One private helper performs the paged fetch for both.

- [ ] Add failing tests: a three-page range from both methods (order, no duplicates, request `start` values one second after each page's last bar, unchanged `end`); one request for a single page; `ConnectionError` for missing metadata, for an empty truncated page and for a non-advancing page.
- [ ] Run `uv run pytest tests/market_data/test_remote_client.py -q` and confirm the new cases fail for the expected reason.
- [ ] Implement the paged fetch with a bounded loop. Reuse the existing timezone helpers for the cursor; add no new timezone code.
- [ ] Run `uv run pytest tests/market_data tests/streaming -q -k "ohlcv or remote or publisher"` and fix any test double that now needs metadata.
- [ ] Commit this unit.

### 4. Remove the cap from the native client

**Files:** Modify `src/q_backend/market_data/clients/metatrader.py` and `tests/market_data/test_get_ohlcv_chunked.py`.

- [ ] Replace `test_get_ohlcv_respects_max_bar_cap` with a test that a range longer than a patched small cap is returned whole; confirm it fails first.
- [ ] Remove the truncation from `_fetch_ohlcv_range_chunked`, keeping `_MAX_HISTORY_CHUNKS` as the loop bound, and delete the constant if nothing else uses it.
- [ ] Run `uv run pytest tests/market_data/test_get_ohlcv_chunked.py tests/market_data/test_metatrader_client_lock.py -q` and confirm it passes.
- [ ] Commit this unit.

### 5. Prove the research path and document it

**Files:** Modify `tests/research/test_data.py`, `docs/research-library.md` and `docs/mt5-wine-gateway.md`.

- [ ] Add a research test: `load_bars` over a mocked multi-page gateway range returns one frame with a unique ascending index covering the full range.
- [ ] Update both guides as the spec lists, including the troubleshooting row and the manual checklist step.
- [ ] Run `uv run pytest tests/research/test_data.py -q` and confirm it passes.
- [ ] Commit docs and test.

## Verification and handoff

- [ ] Run `uv run pytest tests/gateway tests/market_data tests/research/test_data.py -q`, `uv run ruff check gateway src/q_backend/market_data src/q_backend/research tests/gateway tests/market_data tests/research` and `uv run black --check` on the same paths.
- [ ] Run `make contracts-check`.
- [ ] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-093 in-review -m "<changes; checks and results; follow-ups>"`. State in the message that the operator must restart `mt5-gateway.service` after merge and run the manual checklist step; the task does not restart services.
