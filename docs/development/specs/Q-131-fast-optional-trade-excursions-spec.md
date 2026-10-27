# Q-131: Fast and optional trade excursion measurement

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 35 — Fast and optional trade excursion measurement
**Depends on:** —
**Implementation plan:** [Plan](../plans/Q-131-fast-optional-trade-excursions-plan.md)

## Purpose

Q-130 added per-trade excursion measurements (MFE, MAE, capture, extreme times, post-exit move). Every research
`backtest()` and every service backtest now measures them after the simulation, and the measurement costs more
than the simulation itself.

Profile of one research run: `IND$N` M5, March 2026, 2,424 bars, 1,758 trades, `ticks=` a `TickStore`, one
process. The run took 33 s in total:

| Step | Time |
|---|---|
| Simulation (`run_candle`) and strategy hooks | ~3 s |
| `measure_trades` | 24.9 s |
| Writing the measurements back into `result.trades`, row by row | 3.4 s |

Inside `measure_trades`:
- 13.9 s is spent boxing 16.5 million deal timestamps one at a time (`_timestamp(v) for v in deals_in_bar.times`,
  and a second pass over the exit bar).
- 6.2 s is spent building per-trade Python candidate lists.
- 2.8 s is `post_exit_from_stored` reading bars one at a time with `iloc`.

The cost grows with trades × deals per partial bar. It runs in the parent process after the session workers
finish, so `workers=` does not reduce it. An Optuna study that runs hundreds of backtests, and never reads the
excursion columns, pays it on every trial.

After this task:
1. A research `backtest()` can skip the measurement with `excursions=False`.
2. When it runs, the measurement produces **exactly** the values it produces today, at least ten times faster on
   the profiled workload.

## Behaviour

### Turning the measurement off

- `backtest()` gains a keyword `excursions: bool = True`. It must be a `bool`; any other type, such as `0` or
  `"no"`, raises `ValueError` naming the parameter, so a mistyped value cannot silently turn the measurement off.
- With `excursions=True` (the default), nothing changes.
- With `excursions=False`:
  - `measure_trades` is not called and no deals are read for it;
  - the results are otherwise unchanged: trades, fills, PnL, metrics, equity, rejected entries and data are
    identical to the same run with `excursions=True`;
  - every excursion column of `result.trades` is null, with the dtypes `results.py` already declares:
    `mfe_points`, `mae_points`, `mfe_value`, `mae_value`, `mfe_time`, `mae_time`, `mfe_capture`,
    `post_exit_mfe_points`, `post_exit_mae_points`, `post_exit_return_points`, `post_exit_bars_observed`,
    `exit_bar_rest_high`, `exit_bar_rest_low` and `excursion_resolution`;
  - `result.performance.excursions` reports zero measured trades, which `analytics.py` already does for
    unmeasured trades;
  - `result.config` records `"excursions": False`, so a published run states that it was not measured.
- `post_exit_bars` is still validated whatever `excursions` is.
- A run published with `excursions=False` publishes and reads back without error. Its trades carry null
  excursion fields and its Performance excursion summary shows zero measured trades. The read path does not
  try to measure it.
- Service backtests (`BacktestRequest`, `backtest_jobs.py`) keep measuring and get no toggle. They gain the
  speed-up below.

### Faster measurement, identical values

`measure_trades` and `post_exit_from_stored` keep their signatures, their deal-source protocol (`DealBar`,
`DealSource`) and their definitions from Q-130. Only the implementation changes:

- **Deals as arrays.** Each bar's deal times are converted once to UTC `int64` nanoseconds and its prices
  to `float64`, then cached per bar. This replaces per-deal `pd.Timestamp` boxing. A bar is still read at most
  once per call.
- **Per-trade candidates as arrays.** The path's candidate prices, times and resolutions are concatenated in the
  same order as today: entry fill, exit fill, then bars from entry to exit, each bar's deals in time order. The
  entry and exit boundaries are applied with array masks. The best and worst values come from `argmax` and
  `argmin`, which return the first occurrence, so ties resolve to the same candidate, time and resolution as
  today's `list.index`.
- **Exit-bar rest extremes.** They are computed from the cached arrays with the same `>= exit moment` boundary.
- **Post-exit window.** It is computed from `high`, `low` and `close` arrays and a precomputed bar time of day,
  with the same truncation at `post_exit_bars`, at the day-trade close and at the end of the data. The
  run-time measurement and the `/post-exit` recalculation still share `post_exit_from_stored` and still agree.
- **Write-back.** The research engine assigns the measured columns to `result.trades` in one vectorized step,
  aligned by `trade_id`, instead of one `.loc` assignment per row.

"Exactly" means:
- every excursion field of every trade is equal to the current implementation's value;
- floats are compared with `==` (NaN equal to NaN);
- times are equal `datetime` values in the same timezone;
- `excursion_resolution` strings and `post_exit_bars_observed` counts are equal.

This holds for every fill kind (market at open, priced, stop, limit, tick-timed intrabar exit, open-fill
exit), for `tick`, `minute` and `candle` resolutions, with and without a deal source, and with day-trade
truncation.

## Out of scope

- An excursion toggle on service backtests or in the Research UI.
- Any change to what is measured or how the Q-130 definitions apply. Where the current code differs from the
  Q-130 spec text, this task keeps the current values and does not fix the difference.
- Changes to deal sources beyond what the array conversion needs, to q_core, to contracts or to the frontend.

## Acceptance criteria

1. `backtest(..., excursions=False)` skips `measure_trades` and reads no deals for it. Its trades, metrics,
   equity and rejected entries equal the `excursions=True` run. Its excursion columns are null with the
   declared dtypes, and `result.config["excursions"]` is `False`.
2. A non-`bool` `excursions` raises `ValueError` naming the parameter.
3. A run with `excursions=False` publishes with null trade excursions and `excursions: false` in its config.
   Imported into the stack, it reads back with zero measured trades in `performance.excursions`.
4. A seeded parity test compares the new `measure_trades` and `post_exit_from_stored` with a test-only copy of
   the current implementation, field by field with exact equality. It covers at least 500 synthetic trades
   spanning:
   - every fill kind;
   - `tick`, `minute` and `candle` deal sources, and no deal source;
   - multi-bar trades and same-bar entry and exit;
   - ties at the extreme price;
   - day-trade truncation and the end of the data.
5. The existing tests pass unchanged: `tests/backtesting/test_excursions.py`,
   `tests/research/test_stop_target_backtest.py`, and the service backtest tests that measure excursions
   (`tests/api/test_backtest_post_exit.py`, `tests/api/test_tick_backtest_api.py`,
   `tests/api/test_local_candle_backtest.py`, `tests/api/test_backtest_performance.py`).
6. On a synthetic workload of the profiled size (about 1,750 trades of one or two bars, about 5,000 deals per
   partial bar), `measure_trades` plus the research write-back is at least ten times faster than before. The
   implementing agent reports the before and after times in its board message. No test asserts a timing.
7. The research guide (`docs/research/user_guide/backtesting.rst`), the `backtest()` docstring and the
   parameter table in `docs/research-library.md` document `excursions`. The table also gains the missing
   `post_exit_bars` row.
