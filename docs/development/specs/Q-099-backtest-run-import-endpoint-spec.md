# Q-099: Backtest run import endpoint

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 17 — Research script runs in the Research stack
**Depends on:** Q-097
**Implementation plan:** [Plan](../plans/Q-099-backtest-run-import-endpoint-plan.md)

## Purpose

`run_backtest_job` is the only writer of backtest runs. It creates the `backtest_runs` row and the lake files `result.json`, `trades.parquet`, `equity.parquet` and `market_data.parquet`, and every history, artifact and export endpoint reads those. A finished backtest from a research script cannot be recorded.

After this task `POST /api/v1/backtests/import` records a finished backtest as a completed run with the same row and the same four lake files, marked with its origin. Every existing read endpoint then serves it without knowing it was imported.

## Behaviour

### Endpoint

`POST /api/v1/backtests/import` in `api/routers/backtest.py`, with the request and response models of Q-097. It runs synchronously in the API process and answers `201` with the new `run_id`. Nothing is dispatched to a worker and Redis is not used.

### Validation

A request that passes the schema is rejected with `422` and a message naming the rule when:

- `result.bars` is empty, not strictly ascending by timestamp, or holds a duplicate timestamp;
- an indicator series does not have exactly one value per bar, or two series share a key;
- a trade is not closed, lacks an exit time or PnL, or has an entry or exit time outside the bar range;
- `config.engine` is not `candle`, or `config.ml_filter` or `config.entries` is present;
- `config.strategy` is empty.

`config.strategy` is not looked up in the strategy registry: it is the display name of a script strategy.

### Persistence

One function owns the write so the lake and the ledger cannot disagree:

1. Create the `backtest_configs` and `backtest_runs` rows with `status` completed, `started_at` and `finished_at` set to the import time, `config` set to the request's `config`, `result_summary` set to `result.metrics`, and `origin` `script`. The strategy name is registered through `get_or_create_strategy`, as job runs do.
2. Write `result.json` (the request's `result` with the new `run_id`), `trades.parquet`, `equity.parquet` and `market_data.parquet` through the existing lake writers, and store their paths in `lake_paths`.
   - The equity curve is derived from the closed trades and `config.initial_capital` with the existing `build_equity_curve`, as job runs derive it.
   - The market-data table holds the bars and one column per indicator series.
3. If a lake write fails, remove the run's lake directory and the rows, and answer `500`. An imported run is never listed without its result.

An import always creates a new run. `find_backtest_run_by_config`, which lets a job reuse a run with an identical configuration, never returns a script run.

### Origin and provenance

- `backtest_runs` gains `origin`, a non-null string column with server default `stack`, added by an Alembic revision. Existing rows become `stack`.
- The request's `provenance` is stored in a new nullable JSON column `provenance` in the same revision.
- `BacktestRunListItem` and `BacktestRunDetailResponse` report `origin`; the detail reports `provenance`.
- `GET /api/v1/backtests` accepts `origin` and filters on the column; omitted, it returns every run.

### Script runs are review-only

- `POST /api/v1/backtest` is unchanged and still validates its strategy against the registry, so a script run's configuration cannot be re-run by the stack.
- ML filter sources report a script run as ineligible with a reason that names its origin.
- Bookmarking, deletion, bulk deletion, the equity and trades artifacts and both CSV exports work for script runs exactly as for stack runs.

## Contracts

Update `CONTRACTS_REV` to the merged Q-097 commit and run `make contracts`; `make contracts-check` must be clean. API schema models in `api/schemas/backtest.py` match the contract.

## Documentation

`README.md`: the section on backtest lake files says that `POST /api/v1/backtests/import` writes the same files for an imported run. The research guide is written in Q-100.

## Focused acceptance

1. Importing a valid request returns `201`; the run then appears in `GET /api/v1/backtests` with `origin` `script`, and `GET /api/v1/backtest/{run_id}/result` returns the bars, indicators, trades and metrics that were sent.
2. The equity artifact, the trades artifact and both CSV exports answer for the imported run, and the market-data export has one column per indicator series.
3. Each validation rule above answers `422` and leaves no row and no lake directory.
4. A lake write failure leaves no row and no lake directory.
5. `GET /api/v1/backtests?origin=script` and `?origin=stack` partition the runs; a run created before the migration reports `stack`.
6. A job request whose configuration equals an imported run's configuration creates a new run instead of reusing the script run.
7. `make contracts-check` is clean at the new `CONTRACTS_REV`.

Verification uses the API test client with the test database and a temporary lake root. No worker, Redis, gateway, Docker, GPU or desktop run is required.

## Delivery boundary

- No strategy code is uploaded, stored for execution or run by the stack.
- No change to the job endpoints, the worker, the candle or tick engines, or the lake layout.
- No request-size limit is introduced: an import carries the same payload the result endpoint already serves for a run of the same length.
- The library call that sends the request is Q-100; the desktop changes are Q-101.
