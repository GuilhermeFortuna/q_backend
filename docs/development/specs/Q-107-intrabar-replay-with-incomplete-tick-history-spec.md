# Q-107: Intrabar replay with incomplete tick history

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 20 — Intrabar replay with incomplete tick history
**Depends on:** none
**Repositories:** `q_backend` (owner), with one schema field in `q_contracts` and a results section in `q_frontend`, delivered by the same task on same-named branches.
**Implementation plan:** [Plan](../plans/Q-107-intrabar-replay-with-incomplete-tick-history-plan.md)

## Purpose

A candle backtest with a Breakeven Stop replays a candle's trade ticks to decide what happened inside it. `MarketTickReplay` (`src/q_backend/backtesting/intrabar.py`) requires those ticks to reproduce the candle's open, high, low and close exactly and fails the whole run otherwise:

> WSP$N: historical trade ticks do not reproduce OHLC for candle 2022-02-11 15:00:00: (4475.0, 4480.0, 4415.5, 4428.5) versus (4472.5, 4480.0, 4415.5, 4425.0); use bars and ticks from the same unadjusted history

MetaTrader 5 data cannot meet that requirement. M15 bars and ticks for `WSP$N` were compared on about 30 sessions from 2021 to 2026:

| Cause | Evidence | Frequency |
| --- | --- | --- |
| Bars and ticks use different clocks at a candle boundary | With identical traded volume, a deal stamped up to 0.5 s after a boundary belongs to the previous bar. Closing-auction deals 4 to 15 minutes after the last bar are folded into it. | The last candle of most sessions; up to 5 of 36 candles on a busy session |
| The tick archive is incomplete | 2022-02-11 holds 489 deals and 14% of the volume its bars report (2,585 of 18,008 contracts); 2022-02-10 holds 57%, 2022-01-12 91%, 2025-07-16 97%. `WDO$N` has no ticks before 2025-10-01. | Scattered sessions; whole years for some symbols |

A third cause is not missing data. On a price-adjusted series the terminal adjusts the bars and serves the deals unadjusted, so the deals never reproduce a bar although they are the same trades. One hour of M1 bars and deals was compared per series (2022 to 2026):

| Series | Adjustment observed | Minutes whose deal volume equals the bar's |
| --- | --- | --- |
| `WSP$N` | None; bars and deals identical | All |
| `WSP$D`, `WDO$D`, `DOL$D`, `IND$D`, `BIT$D`, `CCM$D` | One constant offset (644.25 for `WSP$D` on 2024-06-05) | All |
| `WSP$`, `BIT$`, `CCM$`, `PETR4`, `VALE3` | Proportional, each price rounded separately: no single offset or factor reproduces a bar, but a uniform factor does to within one tick inside a minute | All |
| `WDO$`, `DOL$` | A constant offset off the tick grid (68.065 for `WDO$` on 2026-08-04) | All (58 of 60 on 2026-03-11) |

A four-year `WSP$` H1 run substituted 254 of 255 replayed candles for this reason alone.

One-minute bars are complete where ticks are not: 2022-02-11 has 447 M1 bars, and M1 history starts in 2021-10 for `WSP$N` and 2022-01 for `WDO$N`. A typical one-minute range that day is 1.5 points against 11.75 for an M15 candle.

After this task the replay uses real ticks wherever they are complete, moves them onto the bars of an adjusted series, falls back to one-minute bars only for the minutes whose ticks are missing, never fails a run for incomplete tick history, marks each trade decided on a substituted price, and reports what it did in a Notes section of the result.

## Behaviour

### Replay of one candle

The replay is unchanged in when it runs: only for candles the kernel asks for. For such a candle it reads the candle's M1 bars and its deals, and builds the price path one minute at a time.

- **Complete minute.** A minute is complete when a contiguous run of deals has a summed volume equal to the M1 bar's `real_volume` and reproduces its open, high, low and close on the instrument's tick grid. The run starts after the deals claimed by the previous minute and may end up to one second after the minute's end. When no M1 bar follows within the next minute (session close, trading halt), the run may extend to the start of the next M1 bar or the end of the exchange day. The minute contributes those deals with their own timestamps.
- **Adjusted minute.** A minute that is not complete is adjusted when a contiguous run of deals, starting no earlier than the minute's start and within the same window, has a summed volume equal to the M1 bar's `real_volume` and the bar is an adjusted copy of it:
  - the bar's low, open, close and high are the run's own low, open, close and high either shifted by one offset or scaled by one factor, to within two ticks (the offset and factor are taken from the two opens);
  - the bar orders those four prices as the run does, and gives equal run prices equal bar prices.

  The minute contributes those deals with their own timestamps and with prices moved onto the bar: the run's low, open, close and high take the bar's values and the deals between them are interpolated linearly, rounded to the instrument's price digits. No adjustment formula is assumed, so difference-adjusted and proportionally adjusted series, including dividend-adjusted stocks, are handled alike. It needs the instrument's tick size; without one the minute is substituted.
- **Substituted minute.** Any other minute contributes four prices from its M1 bar: open, then low and high in that order when the minute closes at or above its open and high then low otherwise, then close. They are stamped at 0, 15, 30 and 45 seconds into the minute and recorded as substituted.
- **Substituted candle.** When the candle has no M1 bars, or its M1 bars do not aggregate to the candle's own open, high, low and close, the same four-price rule is applied to the candle itself, spread evenly over its duration, and recorded as substituted at candle resolution.
- An empty tick read is retried as today before its minutes are treated as substituted.
- The path handed to the kernel always starts at the candle's open, ends at its close and spans exactly its high and low. The candle remains the authority for those four prices.
- A gateway or provider error is still an error. Only missing or inconsistent history is absorbed.

The two `ValueError`s for "no historical trade ticks" and "do not reproduce OHLC" are removed.

### Trade classification

`Trade` gains `exit_resolution`, serialized with the trade:

| Value | Meaning |
| --- | --- |
| `null` | The exit was decided on a closed bar; no ticks were involved |
| `"tick"` | The exit filled on a real deal, at its own price or at that price moved onto an adjusted series |
| `"minute"` | The exit filled on a price substituted from an M1 bar |
| `"candle"` | The exit filled on a price substituted from the candle itself |

A gap elsewhere in the candle does not make a trade approximate: the substituted high and low of a minute trigger any level inside that minute's range, so a trade that exits on a real deal was not affected by the gaps before it.

### Notes

A backtest result gains `notes`, a list that is empty when the engine has nothing to report. Each note has a stable `code`, a human-readable `message` and a `details` object.

This task emits one note, `intrabar_incomplete_ticks`, when at least one replayed candle used a substituted price. Its message states the number of closed trades, how many exited at each resolution and the net profit and loss of the trades that exited at `minute` or `candle` resolution, for example:

> 412 trades: 397 exits decided on real ticks, 15 at one-minute resolution because tick history was incomplete (net R$ -320.00).

`details` carries the same counts, the number of candles replayed, substituted and adjusted (`candles_rescaled`), and the affected session dates. Adjusted minutes alone do not produce a note: their exits are decided on real deals.

The list is the engine's general channel for run-level warnings; other producers may add codes later without a schema change.

### Contract (`q_contracts`)

`schema/api/openapi.yaml` gains a `BacktestNote` schema (`code` and `message` required strings, `details` an open object) and an optional `notes` array on `BacktestResponse`, defaulting to empty so stored results without it stay valid. Trades are already open objects and need no schema change. Generated code is refreshed with the repository's generator.

### Results view (`q_frontend`)

- The backtest results view shows a Notes section after the existing result content when `notes` is non-empty, listing each note's message; nothing is rendered when it is empty or absent.
- The trade hover card and the trades table show the exit resolution for trades with `minute` or `candle`.

## Focused acceptance

1. Deals that reproduce every M1 bar yield exactly those deals; every trade exiting inside the candle has `exit_resolution == "tick"` and the result has no notes.
2. A deal stamped 300 ms after a minute boundary that belongs to the previous M1 bar by volume and close is assigned to that bar, and both minutes are complete. A closing-auction deal minutes after the last M1 bar is assigned to it.
3. With one minute's deals removed, only that minute is substituted, in the stated order and at the stated offsets; the other minutes keep their deals.
4. With no deals at all, every minute is substituted and the run completes.
5. With no M1 bars, or M1 bars that do not aggregate to the candle, the candle's four prices are used and marked `candle`.
6. A trade whose exit fills on a substituted price is `minute`; one in the same candle that exits on a real deal is `tick`; a closed-bar exit is `null`.
7. The note appears exactly when a substitution happened, and its counts and net value match the trades.
8. A provider `ConnectionError` still fails the run.
11. Deals of a series adjusted by a constant offset are returned shifted onto its M1 bars with their own timestamps, without substitution or note. Deals of a proportionally adjusted series whose bar no single factor reproduces land exactly on the bar's open, high, low and close and between them elsewhere.
12. A run of deals with the bar's volume is substituted, not adjusted, when the bar is more than two ticks from both its shifted and its scaled copy, when the bar orders its four prices differently, or when the instrument's tick size is unknown.
9. `make check` passes in `q_contracts`; `make contracts-check` passes in `q_backend` and `q_frontend` against the task's contract commit.
10. The results view renders the Notes section for a result with notes and omits it otherwise; a stored result without `notes` loads.

Backend tests use a fake market-data service. No live terminal, Wine, Docker, GPU or desktop run is required.

## Delivery boundary

- Candle backtests through `BacktestEngine` and `MarketTickReplay` only. The research path (`research/intrabar.py`) builds its candles from the stored ticks themselves and is unchanged.
- ML-filter evaluation uses the same replay and stops failing, but does not surface notes.
- The order of high and low inside a substituted minute is the fixed rule above. A worst-case order per open position needs the kernel to know the substitution and is recorded for a later `q_core` task, to be taken up if notes show approximate trades carrying a meaningful share of results.
- A breakeven activation that happens on a substituted price, followed by an exit on a real deal, is classified `tick`. The kernel reports the exit tick only.
- An adjusted minute is exact at its four bar prices and interpolated between them. Inside a minute the difference from the series' true adjusted price stayed under one tick in every series measured; it is not reported per trade.
- No tick repair, re-download or caching of coverage; no change to which candles are replayed.
- Imported runs and the tick engine do not produce notes.
