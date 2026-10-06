# Q-093: Complete bar ranges from the MT5 gateway

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 16 — Research data and backtest correctness
**Depends on:** Q-092
**Implementation plan:** [Plan](../plans/Q-093-complete-bar-ranges-from-the-mt5-gateway-plan.md)

## Purpose

A bar request that spans more than 50,000 bars comes back cut, with nothing to say so. `_fetch_ohlcv_chunked` in `gateway/mt5_gateway.py` stops at `_MAX_OHLCV_BARS` and the gateway answers HTTP 200 with the first 50,000 bars. `MetaTraderClient._fetch_ohlcv_range_chunked` has the same cap for the native path. Every consumer of `get_ohlcv` inherits it: research `load_bars`, the market-data service, backtest jobs and the stream publisher.

Observed on 2026-10-06: `load_bars("WDO$N", timeframe="M10", start="2021-10-01")` returned 50,000 bars ending 2025-05-13, while the terminal held 70,032 bars through that day. A backtest over that frame silently omits the most recent 17 months.

After this task a bar request returns every bar the terminal supplies for the requested range, or fails with a clear error. It never returns a cut range as if it were complete.

## Behaviour

### Gateway (`gateway/mt5_gateway.py`)

- `/v1/ohlcv` keeps its per-response limit and adds the `metadata` archive entry defined by Q-092: `{"truncated": <bool>, "max_bars": <limit>}`, encoded like the `/v1/trades` metadata.
- `truncated` is true when the chunk loop stops at the limit before the range is exhausted. A range that ends exactly on the limit may report `truncated: true` and be followed by an empty continuation.
- Bar arrays, dtypes, ordering and the raw-epoch convention are unchanged. `/v1/ohlcv/recent` is unchanged and carries no metadata.
- The module docstring's wire-contract notes describe the new entry.

### Remote client (`src/q_backend/market_data/clients/remote.py`)

- `get_ohlcv` and `get_ohlcv_columnar` follow the Q-092 continuation rule: while a response reports `truncated: true`, request again from one second after the last returned bar's time with the same `end`, and concatenate the pages in order.
- A response without the `metadata` entry raises `ConnectionError` naming the cause (the gateway predates bar completeness reporting) and the remedy (redeploy `gateway/mt5_gateway.py` and restart `mt5-gateway.service`). An older gateway therefore fails loudly instead of truncating silently, as the client already does for a gateway without `/v1/trades`.
- A truncated page that holds no bars, or whose last bar does not advance the cursor, raises `ConnectionError`; the loop is bounded so a faulty gateway cannot spin it forever.
- A range within the limit still costs exactly one request. The existing 404 `symbol_not_found` mapping to an empty result and all other error mappings are unchanged.

### Native client (`src/q_backend/market_data/clients/metatrader.py`)

- `_fetch_ohlcv_range_chunked` returns the complete range: the chunk loop continues to `end` and no longer stops at `_MAX_OHLCV_BARS`. The in-process path has no response-size reason for the cap. `_MAX_HISTORY_CHUNKS` remains the loop bound.

### Consumers

- `load_bars` and every other `get_ohlcv` caller receive complete ranges with no code change of their own. `load_bars` keeps its documented stance on short broker history: it returns what the terminal has and records the returned range in `attrs`.

## Contracts

Update `CONTRACTS_REV` to the merged Q-092 commit and run `make contracts`. The vendored tree is expected to be unchanged, because Q-092 edits only the descriptive YAML; `make contracts-check` must be clean.

## Documentation

- `docs/mt5-wine-gateway.md`: describe the bar completeness metadata, add a troubleshooting row for the "does not report bar completeness" error, and add a manual checklist step that loads more than 50,000 bars after restarting the gateway.
- `docs/research-library.md`: state that `load_bars` returns the whole requested range in one call, and that history depth still depends on the terminal and broker.

## Focused acceptance

1. Gateway tests with the existing fake MetaTrader 5 module: a range under the limit reports `truncated: false`; a range over a patched small limit reports `truncated: true` with exactly `max_bars` bars; array dtypes and raw epochs are unchanged.
2. Remote client tests with mocked HTTP: a three-page range arrives whole, in order and without duplicates from both `get_ohlcv` and `get_ohlcv_columnar`, and the second and third requests start one second after the previous page's last bar; a single-page range issues one request; a response without metadata, an empty truncated page and a non-advancing page each raise `ConnectionError`.
3. Native client test: with the cap patched to a small value, a longer range is returned whole. This replaces `test_get_ohlcv_respects_max_bar_cap`, which pins the old truncation.
4. Research test: `load_bars` over a mocked multi-page range returns one frame covering the full range with a unique, ascending index.
5. `make contracts-check` is clean at the new `CONTRACTS_REV`.

Verification uses the fake MetaTrader 5 module and mocked HTTP. No Wine, live terminal, Docker, GPU or desktop run is required by automated checks.

## Delivery boundary

- No change to ticks, trades, `/v1/ohlcv/recent`, routing, fetch-through or the candle engine.
- The limit value itself is unchanged; it bounds one response so a long download cannot hold the gateway's chart lane.
- The running gateway process must be restarted to load the new script. That is an operator step after merge and is documented, not automated.
