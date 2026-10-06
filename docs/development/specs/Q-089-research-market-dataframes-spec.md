# Q-089: Research market DataFrames

**Status:** revised during human inspection; the Q project board is the status of record.
**Batch:** 15 — Python research library
**Depends on:** none
**Implementation plan:** [Plan](../plans/Q-089-research-market-dataframes-plan.md)

## Purpose

Fetch fresh historical MT5 candles into an ordinary pandas DataFrame with one function call. Use Q's existing MT5 gateway client and timezone/schema helpers without requiring the control API, worker, PostgreSQL, Redis or desktop UI. The MT5 terminal and its gateway must already be running and connected.

## Public interface

```python
from q_backend.research import load_bars

bars = load_bars("WIN$", timeframe="M5", start="2026-09-01")
# A fixed interval is also supported:
bars = load_bars("WIN$", timeframe="M5", start="2026-09-01",
                 end="2026-09-30T18:00:00-03:00")
```

`load_bars(symbol: str, *, timeframe: str, start: str | datetime, end: str | datetime | None = None, gateway_url: str | None = None, gateway_token: str | None = None) -> pd.DataFrame`

- symbol, timeframe and start identify the requested history; end defaults to the current time captured once per call. No Research object, source argument, database URL, catalog root, context manager or close() is required.
- Explicit gateway_url/token override the existing Q_MT5_GATEWAY_URL/Q_MT5_GATEWAY_TOKEN and runtime configuration resolution. Reuse the existing gateway getters; do not add an implicit .env credential loader or overwrite os.environ/runtime configuration. A missing URL raises an actionable error explaining gateway configuration. Credentials are never included in metadata, examples or errors.
- Use the existing canonical timeframe names, case-normalized; validate a nonempty symbol and start <= end before the history request. Bounds are inclusive bar-open timestamps. Naive inputs and date-only strings mean America/Sao_Paulo time; a date-only end means midnight, not the full named day. Aware inputs convert to exchange time.
- Each call sends a new history request through RemoteMt5Client to the connected MT5 terminal. The gateway is Q's transport to MT5, including on the same Linux workstation under Wine; users need no knowledge of local/remote provider selection. Fresh means the latest history available to that terminal, not a guarantee of broker history completeness or a continuous stream.

## DataFrame contract

1. DatetimeIndex named time, timezone-aware America/Sao_Paulo, sorted ascending and unique. Required float64 columns open/high/low/close; tick_volume int64; spread and real_volume float64. Preserve provider volume meanings; do not alias tick volume to traded volume or invent nonzero real volume.
2. Reuse existing market_data.timezone conversion for MT5 naive Brasília timestamps; do not duplicate epoch math. Keep Q-089's existing frame/schema helpers where their behavior already meets this contract.
3. Missing required OHLC fields, nonfinite/nonpositive OHLC prices, invalid OHLC ordering (low <= open/close <= high), duplicates or invalid volumes raise descriptive ValueError. Optional missing spread/real_volume are NaN. No synthetic filling or arbitrary duplicate removal.
4. Return completed candles only. Exclude the current forming bar using the same captured current time and the actual fixed/calendar timeframe completion boundary, even when the requested end is in the future. Keep clock injection private to tests; no public clock option.
5. No completed rows raises NoMarketDataError (ValueError subclass) naming symbol/timeframe/range and MT5 as source. Short history remains short; do not claim complete coverage. Informative attrs["q_research"] records symbol, timeframe, source="mt5", requested start/end, returned start/end and timezone. No catalog dataset_id or credential metadata; downstream computation must not depend on attrs preservation.

## Execution and revision scope

- Reuse RemoteMt5Client.get_ohlcv with explicit resolved gateway arguments and existing wire/timezone conversion. Validate/normalize the frame through shared helpers. HTTP resources are managed internally using the client's existing request lifecycle.
- Fetch directly on every call: no local-catalog read, Parquet cache, Redis, read-through/fetch-through persistence, source auto-selection or fallback on MT5 failure. An unavailable/disconnected gateway remains a clear failure with its original cause.
- Importing load_bars does not connect, create directories, load ASGI/tasks, initialize torch/CUDA or a native MT5 terminal. Calling it does not construct a SQLAlchemy engine or import catalog/provider modules solely for stored-data selection.
- Replace the unreleased Research class public API with load_bars. Remove Q-089-only catalog/inventory/auto-selection code, exports and tests instead of maintaining parallel APIs. Keep the underlying shared market-data service, catalog and other consumers unchanged. Stored-data loading is outside this revised task; pandas can load a caller-supplied historical frame for offline calculations.
- Update existing src/q_backend/research/{__init__,data,providers,frame,errors}.py and tests/research/{test_data,test_imports}.py as needed. Retain reusable normalization; simplify providers.py to gateway configuration/client access, or remove it if data.py can cleanly reuse the existing getters/client directly.

## Documentation and examples

Update docs/research-library.md, README.md and examples/research/load_market_data.py to lead with the one-call example. Script flags: --symbol, --timeframe, --start, optional --end and optional gateway URL/token overrides; omit --source/--database-url/--market-data-root. Explain the already-running MT5/gateway prerequisite, existing environment configuration, end=None, date-only bounds and completed candles. No fixed workstation URLs, sys.path hacks, implicit service startup or public lifecycle configuration.

## Focused acceptance

- Mocked gateway -> specified DataFrame schema/timezone. Calling twice sends two history requests; no cached/catalog response or database engine is involved.
- Frozen private clock proves omitted end is now, aware/naive equivalent bounds select the same rows and the forming bar is excluded. Retain existing timeframe/schema checks; do not rewrite already established cases.
- Missing configuration, gateway failure and empty/partial/malformed history preserve clear errors and the specified frame behavior. Explicit configuration overrides do not mutate global state.
- Import is inert, and the simplified CLI forwards only MT5 data arguments and returns the same frame. Verification uses mocked history, not a live terminal or database.
- Run focused research data/import tests and the existing timezone/remote-client regressions. No benchmark, new mathematical goldens, full CI, Docker/GPU/Wine/desktop or live-feed acceptance gate.

## Delivery boundary

This document revision changes requirements, not product code. It supersedes the catalog-first specification used by the inspected implementation. Original completed work/checks remain in git history; the revised plan tracks only outstanding correction work. The human integrates this documentation commit into the Q-089 task branch after ending inspection, then resumes the existing task through the workspace workflow. Do not alter active inspection checkout state, mark work complete from this authoring session, push/merge or change Status outside ./work.

Q-090 and Q-091 consume this DataFrame contract through load_bars; their indicator/strategy semantics stay as specified. The namespace remains inside q_backend with its existing installation/dependency scope. No new distribution, wire contract, live orders, optimizer integration or q_core release.
