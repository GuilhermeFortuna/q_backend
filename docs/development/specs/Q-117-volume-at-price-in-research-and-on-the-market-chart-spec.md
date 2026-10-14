# Q-117: Volume at price in research and on the Market chart

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 27 — Volume at price
**Depends on:** Q-116
**Repositories:** `q_backend` (owner), with the schema in `q_contracts` and the chart in `q_frontend`, delivered by the same task on same-named branches.
**Implementation plan:** [Plan](../plans/Q-117-volume-at-price-in-research-and-on-the-market-chart-plan.md)

## Purpose

Research can read bars, quotes and spread from the tick store, but not where volume traded. After this task a research script builds volume-at-price profiles from stored trades, per session, per bar or over a range, and reads point-of-control and value-area levels aligned to its bars without lookahead. The Market chart draws the same profiles beside the candles and the same levels as indicator lines, so every value can be inspected.

## Current system

- `q_backend.research.indicators` holds bar helpers and two tick-store helpers, `quote_spread` and `spread_vs_volatility`, which read local files only and report `unavailable_reason` when ticks are missing. Their shared service is `market_data/quote_spread.py`, used by both research and the chart.
- `TickStore` stores per-session columns `time_msc`, `bid`, `ask`, `last`, `volume` and `flags`. `quotes(day)` returns ordered quote columns; no method returns trades with their volume and flags. `real_volume` in its bars counts ticks with the buy or sell bit.
- `load_bars` records the gateway's `trade_tick_size` in `frame.attrs["q_research"]["tick_size"]` when the symbol info is available.
- The indicator catalog (`market_data/indicator_catalog.py`) serves `GET /api/v1/market/indicators` and `POST /api/v1/market/indicators/{symbol}/series`. Required data is `bars` or `quotes`; every output draws as a line. Every series is aligned to bar times, so a profile, which is indexed by price, cannot be served by it.
- The Market chart draws candles, volume, catalog indicators and published studies as SVG layers under `q_frontend/src/components/charts/layers/`. Nothing draws horizontally by price.

## Required behavior

### Kernel bridge and shared service

- `pyproject.toml` pins the `q-core` tag published by Q-116; `REQUIRED_KERNELS` lists `volume_at_price` and `volume_at_price_levels`, wrapped in `backtesting/technical_indicators.py`. No volume-at-price arithmetic is written in Python.
- `TickStore.trades(day)` returns one session's ordered `(time_msc, last, volume, flags)` columns, like `quotes(day)`.
- `market_data/volume_at_price.py` is the one service that research and the chart call. It builds segments on the tick store's clock with the `bar_bounds_msc` rules of `quote_spread.py`, keys each session by its calendar day, feeds each session's trades to the kernels, and returns `unavailable_reason` when the range holds no stored session.

### Research (`q_backend.research.indicators`)

```python
profile = indicators.volume_at_price(ticks, start="2026-10-01", end="2026-10-10", tick_size=0.5)
levels = indicators.value_area_levels(bars, ticks=ticks, timeframe="M5", mode="prior_session")
```

- `volume_at_price(ticks, *, start, end, tick_size, by="session", timeframe=None, row_ticks=1, value_area=0.7)` returns a frozen `VolumeProfile` with two DataFrames:
  - `rows`: indexed by segment start (Brasília time), columns `price`, `buy`, `sell`, `unknown`, `total`, `delta`, `trades`, ordered by segment then price.
  - `segments`: indexed by segment start, columns `end`, `total`, `buy`, `sell`, `unknown`, `trades`, `poc`, `vah`, `val`, `skipped_trades`, `off_grid_trades`.
  - `by` is `session` (one segment per stored session in the range), `bar` (one per bar of `timeframe`, which is then required) or `range` (one segment `[start, end)`). `unavailable_reason` is an attribute, `None` when ticks exist. A range without ticks returns empty frames with the reason, never zeros.
- `value_area_levels(frame, *, ticks, timeframe, mode="developing", tick_size=None, row_ticks=1, value_area=0.7)` returns a DataFrame with `poc`, `vah` and `val` on the frame's exact index, under the frame validation rules of `quote_spread`:
  - `developing`: the session's levels from its first trade up to the **bar's close**, through `volume_at_price_levels`. A value is known when the bar closes, like the bar's close price.
  - `prior_session`: the final levels of the previous session stored in the tick store, constant through the session; the first stored session is NaN. The store has no exchange calendar, so a weekday missing from it is skipped silently; `docs/research-library.md` says to sync contiguous history before using this mode.
  - `tick_size` defaults to `frame.attrs["q_research"]["tick_size"]`; without either, `ValueError` says how to supply it.
- Both functions read local files only: no gateway, synchronization, database or Redis. `tick_size`, `row_ticks`, `value_area`, `by` and `mode` are validated with errors naming the parameter.

### Indicator catalog

- `value_area_levels` is a catalog entry in a new **Volume** category, price pane, with outputs `poc`, `vah`, `val`. Parameters: `mode` (`developing`, `prior_session`), `row_ticks` (1 to 1,000) and `value_area` (0.05 to 1, default 0.7). It requires the new data kind `trades`.
- Catalog outputs gain an optional `kind` (`line` default, or `step`), drawn with the Q-115 step renderer; the three levels are `step`.
- The series service takes the tick size from the symbol info the market service already serves (`trade_tick_size`). Without a tick size or without stored ticks, the series is unavailable with a reason, as quote indicators are.

### Volume profile endpoint

- `POST /api/v1/market/volume-profile/{symbol}` takes `start`, `end`, `segment` (`session` or `range`), `rowTicks` and `valueArea`. It returns `tickSize`, `rowSize` and, per segment, `start`, `end`, the totals, `poc`, `vah`, `val` and its rows as parallel arrays `price`, `buy`, `sell`, `unknown`.
- When the range holds no ticks, or the symbol has no tick size, the response is `200` with no segments and an `unavailableReason`.
- `422` for an inverted range, an unknown segment, or `rowTicks` or `valueArea` out of range. `413` when the rows across segments exceed `Q_MARKET_VOLUME_PROFILE_MAX_ROWS` (default 200,000), with detail "Profile has N rows; the limit is L; raise the row size".

### Market chart

- The indicator picker lists **Volume profile** under **Volume**, with `segment` (`Session` or `Visible range`), row size in ticks, value-area share, buy/sell split on or off, and width (5 to 50% of the plot, default 25%).
- **Session profiles** draw at the left edge of each session's first bar, extending right, scaled to that session's largest row and never past the session's last bar. **Visible range** draws one profile docked to the right edge of the plot for the bars in view; it refetches, debounced, when the visible range changes.
- A new `VolumeProfileLayer` draws beneath the candles. Rows inside the value area are drawn stronger than the rest; the POC row is highlighted and VAH and VAL are marked. With the split on, each row shows its buy and sell parts, and unknown volume in a neutral colour. Only rows inside the visible price range are rendered.
- Hovering a row shows price, total, buy, sell, unknown and delta in the existing tooltip.
- `value_area_levels` appears in the picker like any catalog indicator and draws as steps.
- An unavailable profile shows its reason the way an unavailable spread does. Saved chart profiles persist the volume profile and its settings; profiles saved before this task load unchanged.

### Contract

`q_contracts` `schema/api/openapi.yaml` gains the volume-profile route and schemas, the `trades` required-data value and the optional catalog output `kind`. Backend and frontend vendor the new revision and `make contracts-check` passes in both.

## Documentation

`docs/research-library.md` gains **Volume at price from local trades** next to **Liquidity from local quote history**: the trade rule, the row grid, POC and value area, the two level modes and their timing, and the size limit. `examples/research/volume_at_price.py` profiles a synced week and publishes prior-session levels as a study.

## Focused acceptance

Synthetic trades in a tick store under `tmp_path`, the API test client with a temporary database, mocked symbol info, and component tests against mocked endpoints. No gateway, Docker, GPU or desktop.

1. Importing the bridge with a `q_core` lacking a Q-116 kernel raises `ImportError` naming it.
2. `TickStore.trades(day)` returns time-ordered columns with volume and flags; quote-only ticks are present and ignored by the kernel.
3. `volume_at_price(by="session")` over two synthetic sessions returns the kernel's rows and summaries, and each session's `total` equals the sum of that session's `real_volume` from `TickStore.bars`.
4. `by="bar"` with M5 and `by="range"` give segments with those bounds; a range without ticks returns empty frames with an `unavailable_reason`.
5. `value_area_levels(mode="developing")` on M5 bars equals `volume_at_price(by="range")` from the start of the bar's session to its close; appending later trades leaves earlier rows unchanged.
6. `mode="prior_session"` repeats the previous session's final levels through the next session and is NaN for the first session.
7. A missing `tick_size` with no frame metadata, `row_ticks = 0` and `value_area = 1.5` raise errors naming the parameter.
8. The catalog lists `value_area_levels` with `trades` data and `step` outputs; the series endpoint returns the research helper's values for the same bars, and an unavailable series without ticks or tick size.
9. The profile endpoint returns the research helper's sessions; an empty range returns its reason; each validation rule returns `422`; a request over the row limit returns `413` with the documented detail.
10. The chart draws session profiles inside their sessions and a visible-range profile at the right edge, highlights POC and the value area, splits buy and sell, renders no row outside the visible price range, and shows the tooltip values.
11. A saved chart profile with SMA and RSI from before this task renders unchanged; one with a volume profile restores its settings.
12. `make contracts-check` passes in `q_backend` and `q_frontend`; the focused tests and each repository's lint and type checks pass.

## Implementation boundary

This issue authorizes only its listed deliverable after written-plan approval and
`./work start Q-117 --agent <agent> --worktree`.
The task branch also exists in `q_contracts` and `q_frontend`. No genome node or feature-store feature, no change to backtests or the candle kernel, no footprint chart, no high- or low-volume-node detection, no naked POC tracking, no composite profiles over several sessions, no `q_terminal` change, and no synchronization or gateway call from the research helpers.
