# Q-089: Research market DataFrames

**Status:** written spec and plan awaiting human review; the Q project board is the status of record.
**Batch:** 15 — Python research library
**Depends on:** Q-020
**Implementation plan:** [Plan](../plans/Q-089-research-market-dataframes-plan.md)

## Purpose

Let experiment scripts obtain ordinary pandas OHLCV DataFrames from Q's cataloged market data or its MT5 data gateway, without starting the API, worker, Redis, or desktop applications. Establish `q_backend.research` as a supported Python namespace with explicit instance configuration and no import-time service startup.

## Public interface

```python
from q_backend.research import Research

research = Research(source="local", database_url=database_url,
                    market_data_root=market_data_root)
bars = research.bars("WIN$", timeframe="M5",
                     start="2026-09-01", end="2026-09-30T18:00:00-03:00")
inventory = research.inventory()
```

- `Research(*, source: Literal["local", "remote", "auto"] = "local", database_url: str | None = None, market_data_root: str | Path | None = None, gateway_url: str | None = None, gateway_token: str | None = None)` stores configuration without connecting. Constructor arguments override existing Q settings/environment for the relevant provider; resolve settings lazily. Do not call MarketDataService.load_env, overwrite os.environ, change runtime_config.json, replace catalog/settings singletons, or alter a desktop's source selection.
- `bars(symbol: str, *, timeframe: str, start: str | datetime, end: str | datetime) -> pd.DataFrame` supports existing canonical bar timeframes, case-normalized. Nonempty symbol required; invalid bounds/timeframe fail before I/O. Endpoints are inclusive bar-open timestamps; a date-only bound means midnight in America/Sao_Paulo, not the entire named day. Require start <= end.
- `inventory() -> pd.DataFrame` reads the configured local catalog, independently of the bars source. Columns: symbol, timeframe, start, end, rows, bytes, dataset_id. Only published bar datasets; empty catalog returns a typed empty frame. Times use the public timezone. It does not probe or list gateway symbols.
- `close()`, `__enter__`, `__exit__` release owned resources; close is idempotent, methods after close raise RuntimeError. Injected/shared resources are not disposed by the instance. No service lifecycle commands.

## DataFrame contract

1. DatetimeIndex named `time`, timezone-aware America/Sao_Paulo, sorted ascending and unique. Required float64 columns open/high/low/close; tick_volume int64; spread and real_volume float64. Preserve provider volume meanings; do not alias tick_volume to traded volume or invent nonzero real_volume.
2. Provider naive times are Brasília wall clock under the existing market_data.timezone contract. Aware input bounds convert to Brasília; naive inputs are explicitly interpreted there. Translate catalog query bounds as aware instants; avoid the existing local_store naive/UTC ambiguity. Normalize through shared timezone helpers rather than duplicating MT5 epoch math.
3. Invalid/missing required OHLC fields, nonfinite/nonpositive OHLC prices, invalid OHLC ordering (low <= open/close <= high), duplicate timestamps, or negative/nonfinite volumes fail with a descriptive ValueError. Optional missing spread/real_volume are NaN, never silently imputed. Duplicate bars are rejected, not deduplicated arbitrarily.
4. Exclude a currently forming bar using its timeframe completion boundary and the current exchange-local time. Fixed and calendar timeframes use their actual boundary rules, with an injectable clock for tests. Do not synthesize bars across weekends, holidays, or missing history.
5. No rows raises `NoMarketDataError` (a ValueError subclass) with symbol/timeframe/source/range. A short result stays short: no synthetic filling or claim of continuous coverage. Frame attrs include `q_research` metadata: symbol, timeframe, selected source, requested start/end, returned start/end, timezone, and local dataset_id when applicable. This metadata is informative; downstream code must not depend on pandas preserving attrs.

## Source behavior and ownership

- local: resolve immutable dataset files through a dedicated LakeCatalog/session factory and use lake_query.read_bars_table. Reuse checksum/catalog rules; no filename globbing of Q's managed lake and no legacy catalog.json fallback. PostgreSQL and the referenced Parquet files are required for this path. Missing/unreadable cataloged files or database failure raise an actionable error, not an empty dataset.
- remote: use RemoteMt5Client with explicit resolved gateway arguments and existing wire/timezone conversion. No PostgreSQL or Redis connection, no catalog publication, no fetch-through write, and no native MT5 client construction. Gateway failures remain failures.
- auto: resolve local catalog first. A published dataset whose catalog envelope spans the requested range selects local; otherwise fetch the entire requested range from the configured remote gateway. Envelope coverage is not a promise of every expected trading bar. If no gateway is configured, raise an actionable coverage error; never silently truncate to local coverage. Catalog failure is surfaced rather than hidden behind fallback.
- Calls do not ingest or persist new market data. The facade reads; existing Q ingestion remains the authoritative publication workflow. Remote can fetch history directly without that workflow.
- Make local and remote providers lazy and isolated. Importing the research namespace and constructing Research must not connect, create storage directories, import the ASGI app/tasks, initialize torch/CUDA, or initialize a native terminal. Keep private provider dependencies outside eager public exports. Existing service behavior is preserved.

## Files and documentation

Create research/__init__.py (lazy exports), research/data.py (facade), research/providers.py (scoped adapters), research/frame.py (normalization/validation), research/errors.py. All paths are under src/q_backend. Reuse market_data/catalog/service.py, lake_query.py, clients/remote.py and timezone.py; only factor small shared helpers where needed. Create docs/research-library.md and examples/research/load_market_data.py; README links to the supported library guide. Document running scripts with the existing uv environment, dependency/install scope, provider prerequisites, inclusive bounds and date-only meaning. Do not rely on cwd-specific sys.path modifications or an absolute workstation path.

## Acceptance and focused verification

- Catalog fixture -> DataFrame with exact schema, identity metadata and timezone; UTC/Brasília bounds select identical rows, including historical daylight-saving dates.
- local performs no gateway/native-terminal requests; remote performs no catalog/DB/cache writes; auto selection and missing coverage are explicit.
- Two configured instances use different sources/catalog roots without changing env/runtime configuration; close releases only owned resources.
- Empty, partial, duplicated, corrupt/missing-file, invalid-price, optional-volume and current-forming-bar cases satisfy the contract. Fixed/calendar timeframe tests use a frozen clock.
- Fresh-process import/construction has no service, GPU, terminal, network or filesystem side effects. Inventory is typed for empty and populated catalogs.
- Focused tests use temporary immutable files, injected session/catalog fixtures and mocked HTTP; no live database/gateway/GPU/Wine/desktop run. Run relevant existing lake-query/timezone/remote regression tests. No full CI requirement for this additive facade unless a shared behavior is changed.

## Delivery boundary

Written spec and plan await human review. This session authors documentation and board tasks only. Integrate/publish the documentation before implementation; the human owns initial Status and Todo approval. Launch through `./work start Q-089 --agent <agent> --worktree` from the workspace root after dependencies are Done. Implement natively using superpowers:executing-plans when available; delegation requires separate authorization. No push, merge, protected-branch checkout, or manual board-status changes by implementation agents.

Use the existing backend environment and pinned q_core package. This batch introduces a Python interface inside q_backend, not a separate distribution or a lightweight dependency extra. No HTTP API, generated wire contract, desktop UI, live order submission, tick strategy, optimizer/discovery integration, or q_core release is required. Existing consumers retain their behavior.
