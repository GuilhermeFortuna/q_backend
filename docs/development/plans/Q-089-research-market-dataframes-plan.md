# Q-089 implementation plan: Research market DataFrames

> **For implementation agents:** Read the revised spec and repository instructions. Use superpowers:executing-plans when available; implement natively. Resume Q-089 only through the workspace workflow after the human ends inspection and integrates this documentation revision. Delegation requires separate authorization.

**Goal:** Replace the inspected catalog-first interface with one-call fresh MT5 loading.
**Architecture:** load_bars resolves the existing gateway configuration, fetches through RemoteMt5Client on every call and reuses the completed-candle DataFrame helpers.
**Tech stack:** Python 3.12+, pandas, existing Q MT5 gateway client.
**Spec:** [Specification](../specs/Q-089-research-market-dataframes-spec.md)
**Status:** revised correction plan awaiting integration; checkboxes represent remaining work, not a reset of historical implementation results.

## Global constraints

- Signature/defaults/frame schema are exactly those in the revised spec: end=None means now; source is MT5; no fallback to stored data.
- No public Research instance/lifecycle/provider-selection API, catalog dependency or implicit service startup.
- Retain correct existing normalization and shared market-data behavior; remove code/tests introduced solely for the superseded Q-089 catalog interface.
- Use mocks and focused existing regressions only. No benchmarks, full CI, live terminal, GPU/Wine/desktop or database requirement for verification.
- Preserve the active inspection state; no push, merge or manual Status changes.

## Review focus

- A second identical call really fetches again rather than using stored data.
- Omitted end and forming-bar filtering use one captured current time.
- Configuration overrides and failures do not mutate global settings or hide MT5 errors.
- Timezone/schema/partial-history behavior survives replacement of the facade.
- Import and CLI use avoid database/native-terminal initialization.

## Ordered correction work

### 1. Replace the public loader and remove catalog coupling

**Files:** Modify src/q_backend/research/{__init__,data,providers,frame,errors}.py and tests/research/{test_data,test_imports}.py. Remove a research-only module if it becomes unused; do not edit shared catalog/service code.
**Interface:** load_bars(symbol, *, timeframe, start, end=None, gateway_url=None, gateway_token=None) -> DataFrame, plus the existing NoMarketDataError and frame validation utilities needed by Q-091.

- [ ] Replace obsolete Research/catalog/auto-selection tests with focused load_bars cases using mocked RemoteMt5Client. Assert two calls issue two get_ohlcv requests, exact symbol/timeframe/bounds, output timezone/schema and source="mt5". Assert no SQLAlchemy engine/catalog/cache write and no environment mutation.
- [ ] Add omitted-end/frozen-clock and explicit-bound cases, forming-bar exclusion, missing gateway URL and failed request. Retain the existing useful validation/timeframe tests instead of duplicating them.
- [ ] Run `uv run pytest tests/research/test_data.py tests/research/test_imports.py -q`; implement load_bars with existing gateway getters/client and captured now. Replace the public Research export and remove Q-089-only DB/catalog/inventory resources and obsolete tests.
- [ ] Run `uv run pytest tests/research/test_data.py tests/research/test_imports.py tests/market_data/test_timezone.py tests/market_data/test_remote_client.py -q`; confirm frame behavior and gateway integration. Commit the correction as a focused unit.

### 2. Make the example and guide match the simple interface

**Files:** Modify examples/research/load_market_data.py, docs/research-library.md and README.md. Reuse the existing example test module if present; otherwise add its focused CLI assertions to test_data.py.

- [ ] Replace source/database/root flags with --symbol/--timeframe/--start, optional --end and gateway overrides. Assert omitted end forwards None and the script calls load_bars without constructing Research.
- [ ] Lead documentation with `load_bars("WIN$", timeframe="M5", start="2026-09-01")`. Explain MT5/gateway readiness, environment configuration, latest completed candles and explicit fixed ranges; remove stale catalog/default/auto/inventory instructions. Do not present catalog loading as a second prerequisite for Q-090/Q-091.
- [ ] Run the focused data/import tests including the mocked example path. Commit docs/examples; no live gateway run or broad CI is required.

## Handoff

- [ ] Record actual focused checks/results for the corrected implementation. Prior checks do not establish that load_bars is implemented.
- [ ] Commit on the resumed Q-089 task branch and use `./work board set Q-089 in-review -m "<fresh MT5 loader correction; focused checks and results; follow-ups>"` when the workspace workflow permits. Human review/finish owns integration.
