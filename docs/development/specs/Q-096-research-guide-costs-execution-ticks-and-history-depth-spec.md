# Q-096: Research guide: costs, execution, ticks and history depth

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 16 — Research data and backtest correctness
**Depends on:** Q-094, Q-095
**Implementation plan:** [Plan](../plans/Q-096-research-guide-costs-execution-ticks-and-history-depth-plan.md)

## Purpose

Four gaps in `docs/research-library.md` each led a research session to a wrong or misleading result on 2026-10-06. None is a code defect; each is something the guide should have said.

1. **Costs.** `backtest()` charges nothing unless `costs` is passed, and both backtest examples run without it. A market order at a bar's open also pays half the spread, which the candle price does not include.
2. **Execution.** The guide says fills happen at the next open but does not describe exit rules, reversals, the day-trade window or overnight carry.
3. **Tick rows.** The guide says quote rows carry the previous `last` and `volume`. It does not say that trade rows carry the current bid and ask. An experiment therefore treated a quote as stale unless a quote-update row arrived within one second, and discarded 93% of its signal windows.
4. **History depth.** Nothing explains why M1 and M5 history is short, or that a first tick request for a symbol can come back empty.

This task writes those four sections, makes the examples show their costs, and adds the operator note on history depth.

## Guide content (`docs/research-library.md`)

Each statement below is either demonstrated by a named test or carries its measurement context. Where no test demonstrates an execution statement, this task adds a focused one.

### Execution model

- Strategy hooks see completed bars only. An entry or close decided on a bar fills at the next bar's open.
- Exit rules follow the catalog text from Q-094: evaluated on each completed bar against its high or low, closing at the next bar's open, so the exit price can differ from the level. A rule can trigger on the entry bar.
- One position per symbol under fixed-quantity sizing: repeated entry requests do not stack, and an opposite entry request is skipped while the position cap is full. Returning a close and an opposite entry on the same bar reverses at the next open.
- With `day_trade=True`: an entry is taken only from a signal bar whose time lies between the start and end times inclusive; open positions close at the open of the first bar at or after the close time; a position still open on the last bar of a calendar day closes at that bar's close.
- Without `day_trade`, positions carry across sessions. `force_close_at_end` closes at the last bar's close.
- `equity` is realized only, as the guide already says.

### Transaction costs

- `costs=None` means zero cost. `TransactionCostConfig.cost_per_contract` is charged per contract on each side.
- A realistic per-side cost for a market order is the exchange and broker fee per side plus half the spread: `fee_per_side + 0.5 × tick_size × point_value` when the spread is one tick.
- Worked example for the mini dollar future with `point_value=10.0`, a 0.5-point tick and an assumed fee of R$1.25 per side: `cost_per_contract=3.75`, R$7.50 per round trip, 0.75 points. The fee is an assumption the reader replaces with their own.
- Measurement behind the half-spread term: on 250 `WDO$N` sessions from 2025-10-01 to 2026-10-05, a market order sent within one to three seconds of a 10-minute bar's open paid 0.25 points per side beyond the bar's recorded open price, and the spread at those moments averaged 0.50 points.

### Reading tick rows

- Every row carries the terminal's current bid and ask, including trade rows. The flags say which fields changed on that row; an unchanged quote produces no new quote row.
- The time since the last quote-update row is therefore not a measure of staleness. To price a moment, use the bid and ask of the latest row at or before it.
- Rows can carry a zero bid or ask and must be filtered before computing a midpoint or spread.
- A row with both the buy and the sell flag has an unknown aggressor, as the guide already says.

### History depth and completeness

- `load_bars` and `load_ticks` return what the terminal has. Compare `attrs["q_research"]["returned_start"]` with the requested start before trusting a range.
- The terminal keeps a limited number of bars per symbol and timeframe (its "Max bars in chart" setting, 100,000 by default). At that setting on 2026-10-06 `WDO$N` held M1 from 2026-01-22, M5 from 2023-03-17 and M10 from 2021-10-04, the last being the start of broker history.
- The first tick request for a symbol can return nothing while the terminal downloads history, and tick history depth differs by symbol. MetaTrader 5 reports success with an empty result in both cases, so the gateway cannot tell them apart. `load_ticks` raises `NoMarketDataError` for an empty result and returns a shorter frame, without error, when only part of the range has ticks. Retry after a few seconds and check the returned range.

## Examples

`examples/research/rsi_reversion.py` and `examples/research/mt5_backtest.py` accept `--cost-per-contract` (default `0.0`), pass it as `TransactionCostConfig`, print the total commission charged, and print one line saying no transaction costs were applied when it is zero.

## Operator guide (`docs/mt5-wine-gateway.md`)

A "History depth" section: the "Max bars in chart" setting (Tools → Options → Charts; stored as `MaxBars` under `[Charts]` in the terminal's `Config/common.ini`), how to raise it and restart the terminal, and how to confirm the result with `/v1/available_range`. It states that deeper history then loads on demand and remains subject to what the broker serves. A troubleshooting row covers "M1 or M5 history is shorter than expected" and "the first tick request for a symbol returns nothing".

## Focused acceptance

1. Every execution statement in the guide names, in the plan's record, the test that demonstrates it; any statement without one gains a focused test in `tests/research/test_backtest.py`.
2. Example tests cover the new flag: a non-zero value reaches `backtest()` as `TransactionCostConfig` and appears in the reported commission; zero prints the no-costs line.
3. The guide's cost example computes to the numbers it states.
4. The operator guide's steps were followed once by a human on the target machine, or the section says plainly that it records expected MetaTrader 5 behaviour not yet confirmed there.

Automated verification uses synthetic frames and mocked loaders. No Wine, live terminal, Docker, GPU or desktop run.

## Delivery boundary

- Documentation, two example flags and at most a few pinning tests. No change to `backtest()`, the loaders, the gateway or defaults.
- No cost presets, fee tables or instrument registry.
- Detecting unsynchronized tick history in code is not attempted; it needs a cross-check against bar volume and is recorded in the Batch 16 document.
- `experiments/wdo_order_flow.py`, the script that mis-read quote staleness, lives on the `research/experiments` branch and is not part of `development`; it is corrected there, outside this task.
