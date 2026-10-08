# Q-103: Research tick store

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 18 — Stop and target orders in research backtests
**Depends on:** none
**Implementation plan:** [Plan](../plans/Q-103-research-tick-store-plan.md)

## Purpose

`load_ticks` fetches from the MT5 gateway on every call and keeps nothing. Broker tick history is short (`WDO$N` starts on 2025-10-01) and the terminal can stop serving old sessions, so a tick-resolved backtest needs ticks that stay on disk, can be read one session at a time, and produce the bars the backtest runs on.

After this task a research script keeps a local tick store per symbol, tops it up from the gateway, and reads ticks and bars from it without a gateway, database or running stack.

## Public interface

```python
from q_backend.research import TickStore

store = TickStore("WDO$N")
store.sync(start="2025-10-01")            # needs the MT5 gateway
bars = store.bars("M10", start="2025-10-01")   # offline
ticks = store.ticks(start="2026-10-05", end="2026-10-06")
```

`TickStore(symbol: str, *, root: str | Path | None = None)`

- `root` overrides the environment variable `Q_RESEARCH_TICK_STORE`, which overrides the default `data/tick_store` under the project root. The directory is created on first write, not on construction or import.
- `symbol` is required and non-empty. Constructing a store does not touch the gateway or read any file.

## Storage

- One Parquet file per symbol and exchange calendar day: `<root>/<symbol slug>/<YYYY-MM-DD>.parquet`, zstd-compressed, with the gateway's columns unchanged (`time_msc`, `bid`, `ask`, `last`, `volume`, `flags`) and rows in stable `time_msc` order.
- A stored day is never rewritten. Removing its file is the way to fetch it again.
- A file is written to a temporary name in the same directory and renamed, so an interrupted run leaves no partial day.
- The store is files only: no database, catalog entry or Redis key. It is separate from the stack's lake catalog and from `Q_TICK_CACHE_DIR`.

## Synchronising

`sync(*, start, end=None, gateway_url=None, gateway_token=None) -> TickSyncReport`

- Walks the exchange calendar days from `start` to `end` (default: yesterday in `America/Sao_Paulo`), skipping Saturdays, Sundays and days already stored. The current day is never stored, because its session is not finished.
- Each missing day is requested through `RemoteMt5Client.get_ticks_columnar` with `use_cache=False`, using the same gateway resolution as `load_ticks`.
- An empty response stores nothing. MT5 reports success with no rows both for a day without trading and for history it has not synchronised yet, so the day is reported as empty and requested again on the next `sync`.
- A day is stored only when two consecutive requests return the same number of rows. A day that differs is reported as unsettled and left for the next `sync`.
- A gateway failure on one day is reported for that day and does not stop the remaining days.
- `TickSyncReport` lists the stored, already-present, empty, unsettled and failed days, and prints as one line per group.

## Reading

- `sessions() -> list[date]`: the stored days, ascending.
- `ticks(*, start, end=None) -> DataFrame`: the same frame `load_ticks` returns (Brasília-aware index, `bid`, `ask`, `last`, `volume`, decoded `flags`), so `resample_ticks` and existing notebooks accept it. Bounds follow `load_bars` rules. No stored tick in the range raises `NoMarketDataError` with source `tick_store`.
- `bars(timeframe, *, start, end=None) -> DataFrame`: bars in the `load_bars` schema built from the stored ticks.
  - Prices are the positive, finite `last` values in stored order, as `resample_ticks` uses. `tick_volume` counts them; `real_volume` sums `volume` on rows flagged as a buy or sell trade; `spread` is `NaN`.
  - Bars are built per session and concatenated. A bar exists only if it contains at least one price, so no bar is synthesised across a gap or overnight.
  - Timeframes `M1` to `D1`. `W1` and `MN1` raise `ValueError`.
  - The store keeps one-minute bars per session in `<root>/<symbol slug>/bars_M1/<YYYY-MM-DD>.parquet`, written the first time a session is read, and aggregates every coarser timeframe from them. The result equals resampling the session's ticks directly.
  - `attrs["q_research"]` records symbol, timeframe, `source="tick_store"`, requested and returned bounds and timezone.
- `trade_prices(start, end) -> tuple[np.ndarray, np.ndarray]`: wall-clock microseconds and prices of the positive `last` values in `[start, end)`, in stored order. It reads one session file per call and keeps the most recently read session in memory, so repeated calls within a day do not reread the file. This is the accessor Q-104 hands to the candle kernel.

## Documentation and example

- `docs/research-library.md` gains a "Tick store" section after "Reading tick rows": what is stored and where, `sync`, the empty and unsettled cases, `bars` and its difference from `load_bars` (history limited to stored sessions, prices from trades only), and the advice to sync regularly because old sessions may stop being served.
- `examples/research/sync_ticks.py` with `--symbol`, `--start`, optional `--end`, `--root` and gateway overrides prints the report.
- `.env.example` and the README's storage and environment sections list `Q_RESEARCH_TICK_STORE` next to `Q_TICK_CACHE_DIR`.

## Focused acceptance

1. `sync` over a mocked gateway stores one file per weekday with rows, skips stored days without a request, never requests the current day, and reports empty, unsettled and failed days without storing them.
2. An interrupted write leaves no file under the day's final name.
3. `ticks` returns the `load_ticks` frame for a stored range and raises `NoMarketDataError` for an uncovered one.
4. `bars("M10")` equals `resample_ticks` applied to each stored session with bars that hold no price removed, for a two-session fixture with an intraday gap; aggregating from the cached one-minute bars gives the same frame as a first, uncached call.
5. `trade_prices` returns exactly the prices of one bar's interval, and a second call in the same session does not reread the file.
6. Importing `q_backend.research` and constructing a `TickStore` create no directory, open no connection and import no database module.

Verification uses a mocked gateway client and `tmp_path`. No live gateway, database, Docker, GPU or desktop run.

## Delivery boundary

- `load_ticks`, `load_bars`, `resample_ticks`, the gateway, the lake catalog and `Q_TICK_CACHE_DIR` are unchanged.
- No scheduled or background synchronisation, no automatic sync from `bars` or `ticks`, and no retention or pruning.
- No contract change.
- Backtesting against the store is Q-104.
