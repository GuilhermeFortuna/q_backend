# Q-131 implementation plan: Fast and optional trade excursion measurement

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-131 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** a research `backtest()` can skip excursion measurement with `excursions=False`. When the measurement
runs, it returns exactly today's values at least ten times faster.
**Architecture:**
- `q_backend.backtesting.excursions` keeps its public functions and deal-source protocol. Deal times and prices
  become cached NumPy arrays per bar, and per-trade candidates become concatenated arrays reduced with
  `argmax`/`argmin`.
- `post_exit_from_stored` works on bar arrays.
- `q_backend.research.engine.backtest` gains the `excursions` flag and writes the measured columns back in one
  vectorized step.
**Tech stack:** Python 3.12, NumPy, pandas, pytest.
**Spec:** [Specification](../specs/Q-131-fast-optional-trade-excursions-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `q_backend` only. No q_core, contract or frontend change, and no new dependency.
- Measurement values must not change. The parity test is the guard; never update an existing excursion
  assertion to make a test pass.
- Test with synthetic bars and deals, fake deal sources and `tmp_path` tick stores. No worker, Redis, gateway,
  MT5, Docker, GPU, Wine or desktop.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- **Tie-breaking.** Candidate arrays are in today's order and the reductions take the first occurrence, so
  `mfe_time`, `mae_time` and `excursion_resolution` match on equal prices.
- **Boundaries.** The entry-bar mask keeps deals at or after the entry moment, the exit-bar mask keeps deals at or
  before the exit moment, and the rest extremes keep deals at or after the exit moment. Times are compared as UTC
  nanoseconds, and naive inputs are still treated as UTC, as `_timestamp` does today.
- **Reads.** Each bar is fetched from the deal source at most once per `measure_trades` call, and not at all with
  `excursions=False`.
- **Write-back.** The vectorized assignment is aligned by `trade_id`, keeps the declared dtypes, and leaves rows
  without a measured trade null.
- **Parity oracle.** The test-only reference is the pre-change code copied verbatim and is not imported by
  `src/`.

## Ordered implementation

### 1. Parity oracle and generator

**Files:** `tests/backtesting/excursions_reference.py` (new), `tests/backtesting/test_excursions_parity.py` (new).

- [x] Copy the current `measure_trades`, `post_exit_from_stored` and their helpers verbatim into
  `excursions_reference.py`, with a docstring saying they are the Q-130 implementation kept as a test oracle.
- [x] Write a seeded generator of synthetic bars, deal sources (`tick`, `minute`, missing bars giving `candle`)
  and closed `Trade` objects. It must cover:
  - every fill kind;
  - same-bar and multi-bar trades;
  - repeated extreme prices;
  - exits near the end of the data;
  - day-trade close truncation.
- [x] Assert exact field-by-field equality between `q_backend.backtesting.excursions` and the oracle for at
  least 500 trades and several `post_exit_bars` values. Run it against the unchanged module: it must pass
  before any change, then commit.

### 2. Vectorized measurement

**Files:** `src/q_backend/backtesting/excursions.py`.

- [x] Cache each bar's deals as `(times_ns: int64, prices: float64, resolution)`. Normalize times with
  `pd.DatetimeIndex`: localize naive values to UTC, convert aware values to UTC, then take `.asi8`.
- [x] Rebuild the per-trade path from arrays in the current candidate order, apply the entry and exit masks,
  and take the extremes with `argmax`/`argmin`. Convert only the two chosen times back to `datetime`.
- [x] Compute `exit_bar_rest_high` and `exit_bar_rest_low` from the cached exit-bar arrays.
- [x] Rewrite `post_exit_from_stored` over `high`, `low`, `close` arrays and a precomputed bar time of day,
  with the close-time parse hoisted out of the loop.
- [x] Run the parity test, `tests/backtesting/test_excursions.py`, the API post-exit test and
  `ruff`/`black`, then commit.

### 3. Research toggle and write-back

**Files:** `src/q_backend/research/engine.py`, `tests/research/test_backtest_excursions_toggle.py` (new).

- [x] Write failing tests for spec acceptance items 1–3:
  - with a call-counting deal source, `excursions=False` reads no deals and gives null excursion columns with
    their declared dtypes;
  - every other result is equal to the `excursions=True` run;
  - a non-`bool` value raises `ValueError`;
  - `config["excursions"]` is recorded;
  - the published payload carries null trade excursions and `excursions: false`, checked through the
    `httpx.MockTransport` pattern of `tests/research/test_publishing.py`;
  - that payload, imported through the harness of `tests/api/test_backtest_import.py::test_import_of_unmeasured_trades_reads_back_with_zero_excursion_counts`, reads back with zero
    measured trades in `performance.excursions`.
- [x] Add `excursions: bool = True` to `backtest()`, validate it beside `post_exit_bars`, record it in the
  config, and skip the measurement block when it is `False`.
- [x] Replace the per-row `.loc` write-back with one assignment of the measured columns, mapped by `trade_id`.
- [x] Run the new tests, `tests/research/test_publishing.py`, `tests/api/test_backtest_import.py::test_import_of_unmeasured_trades_reads_back_with_zero_excursion_counts`,
  `tests/research/test_stop_target_backtest.py`, `tests/api/test_tick_backtest_api.py`,
  `tests/api/test_local_candle_backtest.py`, `tests/api/test_backtest_performance.py` and `ruff`/`black`, then
  commit.

### 4. Timing check

- [x] Time `measure_trades` plus the research write-back on a synthetic workload of the profiled size (about 1,750
  trades of one or two bars, about 5,000 deals per partial bar), using the oracle for "before" and the new code
  for "after". Use a throwaway script outside the repository, and do not commit it.
- [x] Confirm the speed-up is at least ten times, and record both times for the board message.

### 5. Documentation

**Files:** `docs/research/user_guide/backtesting.rst`, `docs/research-library.md`, the `backtest()` docstring.

- [x] Document `excursions=False`:
  - what it skips;
  - that the excursion columns are null;
  - that it is useful for parameter searches that do not read them.
- [x] Add the `excursions` and `post_exit_bars` rows to the `docs/research-library.md` parameter table.
- [x] Commit.

## Verification

- `uv run pytest tests/backtesting/test_excursions.py tests/backtesting/test_excursions_parity.py tests/research/test_backtest_excursions_toggle.py tests/research/test_publishing.py tests/api/test_backtest_import.py::test_import_of_unmeasured_trades_reads_back_with_zero_excursion_counts tests/research/test_stop_target_backtest.py tests/api/test_backtest_post_exit.py tests/api/test_tick_backtest_api.py tests/api/test_local_candle_backtest.py tests/api/test_backtest_performance.py`
- `uv run ruff check` and `uv run black --check` on the changed files.
- The before and after timings from step 4, reported in the board message.
