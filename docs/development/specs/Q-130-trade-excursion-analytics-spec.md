# Q-130: Trade excursion analytics

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 34 — Trade excursion analytics
**Depends on:** —
**Implementation plan:** [Plan](../plans/Q-130-trade-excursion-analytics-plan.md)

## Purpose

A backtest reports where each trade entered and exited, but not what it went through while it was open, or what happened after it closed. There is no way to tell whether a strategy's stops are too tight, its targets too close, or its exits too early.

After this task, every closed trade carries five path measurements:

| Measurement | Question it answers |
|---|---|
| MFE (maximum favorable excursion) | How far did price move in the trade's favour while it was open? |
| MAE (maximum adverse excursion) | How far did it move against it? |
| MFE capture | What share of that best move did the exit keep? |
| Time to MFE | How long after entry was the best price reached? |
| Post-exit move | Did price keep going the trade's way after it closed? |

The values appear in the following places:
- the trade records and the CSV export;
- the research trades frame;
- per-side aggregates in `BacktestPerformance`;
- Trade List columns, the trade hover card and a Performance tab panel.

The main goal is accuracy. Each value is measured on actual exchange deals wherever they are available, and each trade records the resolution it was measured at. A value derived from bars is never presented as tick-exact.

## Definitions

All prices are trade (last) prices: the series the candle bars are built from, the deals `MarketTickReplay` and `TickStore` serve, and the prices the tick engine fills on. Points are price units in the trade's direction, per contract. Money is points × `quantity` × `point_value`.

### The trade's path

The path of a closed trade is every deal from its **entry moment** through its **exit moment**, plus the entry fill price and the exit fill price themselves. Including both fills means the realised move always lies inside the path. MFE is therefore at least the realised gain, MAE is at least the realised loss, and capture never exceeds 1.

- **Entry moment:**
  - a fill that has an entry tick time (`entry_tick_time`: priced, stop and limit entries filled from deals) enters at that deal;
  - a market fill at the open of `entry_bar` enters at the start of that bar, so the whole entry bar is part of the path;
  - any other mid-bar fill enters at the first deal in the entry bar that trades at or through the fill price.
- **Exit moment:**
  - an intrabar exit with an `exit_tick_time` (stop, target, breakeven or custom intrabar exits decided on deals) exits at that deal;
  - a fill at the open of `exit_bar` (closed-bar signal, rule and end-of-day exits) exits at the start of that bar, so only its opening deal is part of the path;
  - any other mid-bar fill (for example a priced close) exits at the first deal in the exit bar that trades at or through the fill price.

The implementation pins each of these fill conventions to the q_core kernel's actual behaviour in tests. It must not assume them.

### Measurements

- `mfe_points` ≥ 0: the best price on the path, measured from the entry fill in the trade's direction.
- `mae_points` ≥ 0: the worst price on the path, measured the same way, as a magnitude.
- `mfe_time`: the first deal at which the MFE price was reached. When MFE is 0, it is the entry moment.
- `mae_time`: the same for MAE.
- `mfe_capture`: the realised gross points divided by `mfe_points`. It excludes commission, so it is a measure of price, not cost. It can be negative (a trade that ran up and then lost) and is at most 1. It is null when `mfe_points` is 0.
- Time to MFE is `mfe_time − entry moment`, in seconds. Clients derive it from the stored times and `entry_tick_time` or `entry_time`.

### Post-exit window

- The window runs from the exit moment through the end of the `post_exit_bars`-th bar after `exit_bar`.
- It is cut short at the end of the loaded data. In day-trade runs it is also cut at the session's `day_trade_close_time`, since a day-trade position could not have been held past it.
- Over that window:
  - `post_exit_mfe_points` ≥ 0 and `post_exit_mae_points` ≥ 0 are measured from the exit fill price in the trade's original direction;
  - `post_exit_return_points` is the last deal in the window minus the exit price, signed in the trade's direction. A positive value means price kept going the trade's way.
- `post_exit_bars_observed` is the number of bars after `exit_bar` that the window actually covered, so a cut-short window is visible.
- A window with no deals at all (exit at the very end of the data) gives null post-exit values and 0 bars observed.
- `post_exit_bars` is an integer between 1 and 500, default 10. Tick-engine runs use bars of the request's `display_timeframe`.

**Recalculable from stored data.** The only part of the window that needs deals is the rest of the exit bar after the exit moment. Each trade therefore stores that part's extremes:
- `exit_bar_rest_high` and `exit_bar_rest_low` are the highest and lowest deals in the exit bar from the exit moment on, including the exit price. For a fill at the exit bar's open, they are simply the bar's high and low.

Everything after the exit bar is made of whole bars, whose high, low and close are deal-exact. The last deal of any window is the close of its last bar, or the exit bar's close when the window covers no further bars. So post-exit values for **any** `post_exit_bars` follow from the two stored extremes plus the run's stored bars (the `market_data` artifact), without reading deals again. A measurement at run time and a recalculation from stored data give identical values for the same `post_exit_bars`. The trade's `excursion_resolution` does not depend on `post_exit_bars`.

### Resolution

Each trade records `excursion_resolution`, the coarsest source used for any part of its path or post-exit window:

- `"tick"`: every part was measured either from real deals or from bars the trade spanned whole, whose high and low are deal extremes.
- `"minute"`: some part came from substituted one-minute prices (`MarketTickReplay`'s fallback for incomplete history).
- `"candle"`: some partial bar had no deal data, so its whole range was used. This gives an upper bound on MFE and MAE, and a time to MFE accurate only to that bar.

A run without any deal source (bars only, for example an imported run, or a research `backtest()` with `ticks=None`) still measures every trade. Trades whose entry and exit both fall on bar opens are fully `"tick"`, because they span whole bars only. Trades with a partial bar are `"candle"`.

Time to MFE and MAE needs the exact deal in the bar that holds the extreme. Without deals for that bar, the time is the bar's start and the trade is `"candle"`.

## Behaviour

### Where values are computed

- One source-agnostic module, `q_backend.backtesting.excursions`, measures closed trades from the run's bars plus an optional **deal source**. A deal source is a callable that returns the deal times and prices of one bar, and how those deals were resolved. There are three adapters:
  - `MarketTickReplay`, for service-backed candle backtests. These build a replay for the measurement even when no breakeven rule is enabled.
  - `TickStore`, for research `backtest(ticks=...)`.
  - The tick engine's own tick arrays. These need no bars: the path and the window are read directly from the ticks, and every trade is `"tick"`.
- Whole bars inside the path are measured from their high and low without reading deals. Deals are read only for:
  - the partial entry and exit bars, and only when their full range could change MFE or MAE;
  - the bar that holds each extreme, to time it;
  - the partial bar the post-exit window starts in.

  That is at most a handful of bar reads per trade. Each bar is read at most once per run.
- Measurement runs after the simulation. It never changes fills, PnL or any existing trade field.

### Records and exports

- `Trade` gains these optional fields, all included in `model_dump`:
  - `mfe_points`, `mae_points`, `mfe_time`, `mae_time`, `mfe_capture`;
  - `post_exit_mfe_points`, `post_exit_mae_points`, `post_exit_return_points`, `post_exit_bars_observed`;
  - `exit_bar_rest_high`, `exit_bar_rest_low`;
  - `excursion_resolution`.

  The fields therefore reach published runs, lake artifacts and the API trade dicts, which stay free-form in the contract.
- The research trades frame and the trades CSV export gain the same columns, after `exit_reason`. MFE and MAE also get money columns, `mfe_value` and `mae_value`.
- Runs stored before this task have none of these fields; they read as null. Imported runs keep whatever values the publisher computed.

### Performance

`TradeStats` (all, long and short) gains these fields. Each is computed over the measured trades of its group, and is null when the group has none.

| Field | Definition |
|---|---|
| `excursion_trades` | Number of trades with measured MFE and MAE |
| `avg_mfe`, `avg_mae` | Mean MFE and MAE in money |
| `mfe_capture` | Σ gross realised money ÷ Σ MFE money: the share of all available favourable movement the strategy kept |
| `median_mfe_capture` | Median per-trade `mfe_capture` |
| `median_time_to_mfe_seconds` | Median time from entry to MFE |
| `avg_post_exit_return` | Mean `post_exit_return_points` in money |
| `post_exit_continuation_rate` | Share of trades with a positive post-exit return |

`BacktestPerformance` gains `excursions`, holding:
- `post_exit_bars`;
- `resolution`, the trade counts `{tick, minute, candle}`.

A stored `performance` without `excursions` is treated as missing. The result endpoint recomputes it on read from the stored trades and does not write it back, as Q-123 and Q-129 do.

### Run requests and progress

- `BacktestRequest` gains `post_exit_bars: int = 10` (1–500). Research `backtest()` gains the same keyword. This sets the run's stored post-exit values. The Research UI changes the window from the results instead (below), so the setup form gets no field.
- Service-backed runs report a `measuring` phase between `simulating` and `saving`. While it reads deals, it reports progress in measured trades.

### Recalculating the post-exit window

`GET /api/v1/backtests/{run_id}/post-exit?bars=N` recomputes the post-exit values of a stored run for `N` bars (1–500), using its stored trades and `market_data` artifact.
- Its response, `PostExitAnalysis`, holds:
  - `post_exit_bars`;
  - per trade, the `trade_id`, `post_exit_mfe_points`, `post_exit_mae_points`, `post_exit_return_points` and `post_exit_bars_observed`;
  - per side (all, long and short), `avg_post_exit_return` and `post_exit_continuation_rate`, defined as in `TradeStats`.
- It is synchronous: it reads no deals, contacts no market-data service and writes nothing. The run's stored values and `performance` are unchanged.
- Day-trade truncation uses the run's stored config.
- Trades without `exit_bar_rest_high` and `exit_bar_rest_low` (runs stored before this task) get null values.
- An unknown run, or a run without a `market_data` artifact, returns 404. `N` outside 1–500 returns 422.
- `GET /api/v1/backtests/{run_id}/export/trades` accepts the same optional `post_exit_bars`. When it is given, the CSV's post-exit columns use that window.

### Research UI

- **Trade List:** MFE, MAE, Capture, Time to MFE and Post-exit columns after PnL. MFE and MAE show money, Capture shows a percentage, and Time to MFE is formatted like hold times. All five sort with the Q-129 comparator. Nulls show `-`. A trade whose resolution is not `"tick"` shows a small marker with a tooltip naming its resolution.
- **Trade hover card:** rows for the five measurements, plus the resolution when it is not `"tick"`.
- **Performance tab:** a "Trade excursions" panel with:
  - the aggregate figures, switchable between all, long and short like the existing trade stats;
  - an MAE vs PnL scatter and an MFE vs PnL scatter, with winners and losers distinguished;
  - a note giving the resolution counts whenever any trade is not `"tick"`.

  The panel is hidden when `excursion_trades` is 0 or `excursions` is absent.
- **Post-exit bars control:** a number input in the Trade excursions panel header. It starts at the run's `post_exit_bars`.
  - Changing the input requests `/post-exit` for the new value. Requests are debounced, and a value outside 1–500 is not sent.
  - The response replaces the post-exit figures shown:
    - the panel's post-exit aggregates;
    - the Trade List Post-exit column and its sort;
    - the hover card's post-exit row;
    - the trades CSV export, which passes the value.
  - While a request is in flight, the old figures stay visible with a loading indicator. A failed request shows an inline error and keeps the last good figures.
  - A Reset action returns to the run's stored value without a request.
  - The value is view state for the open run. It is not saved, and reopening the run starts from its stored value.

## Interfaces

- `q_backend.backtesting.excursions`:
  - `measure_trades(trades, bars, *, deals=None, post_exit_bars, day_trade_close=None)`, which fills the fields above in place;
  - the deal-source protocol and its three adapters.
- `q_backend.backtesting.models.Trade`: the fields listed under Records and exports.
- `q_backend.backtesting.analytics.compute_backtest_performance`: fills the new `TradeStats` fields and `excursions`. It takes `post_exit_bars`, read from the stored run config for stored runs.
- `q_backend.backtesting.excursions.post_exit_from_stored(trades, bars, *, post_exit_bars, day_trade_close=None)`: the shared post-exit computation that both run-time measurement and the endpoint use.
- `q_backend.api.schemas.backtest.BacktestRequest.post_exit_bars`, and `q_backend.research.backtest(post_exit_bars=10)`.
- `GET /api/v1/backtests/{run_id}/post-exit` with `PostExitAnalysis`, and the optional `post_exit_bars` on the trades CSV export.
- `q_contracts/schema/api/openapi.yaml`:
  - the new `TradeStats` fields, nullable except `excursion_trades`;
  - `BacktestPerformance.excursions` (`ExcursionSummary`);
  - `BacktestRequest.post_exit_bars`;
  - the `/post-exit` path, `PostExitAnalysis`, and the export's `post_exit_bars` query parameter;
  - the `measuring` value in the backtest phase enums.

  The generators are re-run, and q_backend and q_frontend pin the new `CONTRACTS_REV`.
- q_frontend: the `Trade` excursion fields in `src/types/backtesting.ts`, a `usePostExitAnalysis(runId, bars)` query, the panel with its Post-exit bars control, the columns and the hover-card rows.
- Docs: `docs/research/user_guide/` gains a section with the definitions, the resolution levels and the `post_exit_bars` parameter.

## Focused acceptance

1. **Hand-computed paths.** On synthetic bars with known deals, `measure_trades` gives exact MFE, MAE, times, capture and post-exit values for:
   - long and short trades;
   - market entries at a bar open;
   - priced and resting entries with entry tick times;
   - intrabar stop and target exits;
   - open-fill signal exits;
   - a trade that is open within a single bar;
   - a trade whose MFE is 0.

   Prices in a partial bar outside the trade's moments are excluded, and fixtures show that including them would change the answer.
2. **Fill conventions.** A kernel-backed test pins every entry and exit fill convention used under Definitions against `run_candle` output.
3. **Resolution.** Deals from substituted minutes mark the trade `"minute"`. A partial bar with no deals marks it `"candle"` and uses the whole bar range. A bars-only run whose trades all enter and exit on bar opens is fully `"tick"`.
4. **Bounded reads.** A deal source that counts its calls shows that whole interior bars are never read, a partial bar that cannot change an extreme is not read, and no bar is read twice.
5. **Post-exit window.** The window stops at `post_exit_bars`, at the session close in day-trade runs and at the end of the data. `post_exit_bars_observed` reports the truncation. `exit_bar_rest_high` and `exit_bar_rest_low` exclude exit-bar deals before an intrabar exit.
6. **Recalculation.** For a stored run measured with `post_exit_bars = 10`, `/post-exit?bars=3` returns values identical to a fresh run measured with `post_exit_bars = 3`, for intrabar and open-fill exits, in both day-trade and swing runs. It reads no deals: the test passes a market-data service that fails on any call. It writes nothing, gives nulls for trades stored without rest extremes, returns 404 and 422 as specified, and the export honours `post_exit_bars`.
7. **Tick engine.** On synthetic ticks, tick-engine trades match MFE, MAE, times and post-exit values computed by hand, and are all `"tick"`.
8. **Unchanged results.** For an existing candle-backtest fixture, fills, PnL and all pre-existing performance fields are unchanged by measurement.
9. **Performance.** `compute_backtest_performance` fills the new `TradeStats` fields and `excursions`. A stored result without `excursions` gets them on read, and nothing is written. Stored trades without excursion fields give null aggregates and `excursion_trades = 0`.
10. **Exports.** The trades CSV and the research trades frame carry the new columns. A research run published through the import endpoint keeps its per-trade values.
11. **Frontend tests:** the columns render and sort, the resolution marker appears for non-tick trades, the panel renders aggregates and both scatters, and the panel is hidden without `excursions`. Changing Post-exit bars requests `/post-exit` (debounced, never outside 1–500) and updates the panel aggregates, the Post-exit column and the hover card. Reset restores the stored values without a request. A failed request keeps the last figures and shows an error. The CSV export passes the selected value.
12. **Checks:** `make check` passes in q_contracts. `make contracts-check` passes in q_backend and q_frontend. The backend lint and focused pytest pass, and `pnpm typecheck` and focused vitest pass with `TZ=America/Sao_Paulo`.

Tests use synthetic bars and deals, a fake deal source, `tmp_path` tick stores, the API test client and a temporary lake root. No worker, Redis, gateway, MT5, Docker, GPU, Wine or desktop run is needed.

## Delivery boundary

- No q_core change. Measurement is a q_backend post-pass over the kernel's trades.
- No change to fills, exit decisions or the intrabar replay used for exits. Where a run's exits were decided without deal data, the measurement still uses real deals when it can read them, and records the resolution.
- Bid/ask excursions are out of scope: B3 futures fill on trade prices.
- Strategy Search `exit_quality` and the Discover `ExitInsightPanel` are unchanged. Moving them onto these per-trade measurements is a follow-up.
- No excursion-based ranking, filters or optimisation objectives.
- No backfill or write-back for stored runs.
