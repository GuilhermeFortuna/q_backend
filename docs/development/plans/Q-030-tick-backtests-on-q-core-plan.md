# Q-030 implementation plan: Tick backtests on q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-030-tick-backtests-on-q-core-spec.md`](../specs/Q-030-tick-backtests-on-q-core-spec.md)  
**Depends on:** Q-029

## Current-system context

`backtesting/tick/kernel.py` holds `_compute_quantity` and `_simulate_njit`
(`@njit(cache=True)`) and the public `simulate(bid, ask, direction, sl_points,
tp_points, initial_capital, point_value, sizing_mode, sizing_a, sizing_b,
sizing_c)`, which returns seven length-`n` arrays (values past `trade_count` are
uninitialised `np.empty` memory), `trade_count` and `final_capital`.
`tick/orders.py` holds `ExitReason`, the `SIZING_*` constants and
`kernel_sizing_params(config)`, which reads `config.safety_margin_per_contract`
for every non-fixed-quantity config. `tick/engine.py` defines `_msc_to_datetime`,
`_split_ticks_by_day` (line 20), `_events_to_registry` (line 44, iterates
`range(trade_count)` building `Trade`s and closing them with
`ExitReason(code).name`), and `TickBacktestEngine` (line 94), whose
`_run_single_chunk` calls `strategy.compute_signals(chunk)` then `simulate`.
`tick/chart_data.py` defines `DISPLAY_TIMEFRAME_MS`, `_msc_to_iso`,
`_mid_price`, `_resolve_bar_ms`, `_resample_ticks_to_bars` (Python loop per bar),
`_sample_indicator_at_bars` and `serialize_tick_chart_data`. Callers are
`api/backtest_jobs._execute_tick` (engine and chart), and
`optimization/tick_backtest_runner.TickBacktestRunner.run` (engine, `DAY_TRADE`).
`numba` is also imported by `strategies/tsmom.py` and `strategies/gatev_pairs.py`.
Tests: `tests/backtesting/tick/test_kernel.py` imports `simulate` and the
`SIZING_*` constants and slices outputs to `trade_count`; `test_tick_engine.py`,
`test_tick_chart_data.py`, `test_tick_strategy_causality.py`; and the
`tick_ma_breakout` case in `test_goldens.py`, which pins summary metrics through
`TickBacktestRunner`.

After Q-029 the wheel exposes `q_core.engine.tick_simulate`, `tick_day_bounds`,
`tick_bars`, `resolve_bar_ms` and `sample_at_bar_ends`, gated by `tick_kernel`
and `tick_bars` fixtures exported from these functions at `067e29c`. After Q-028
(if merged first) the hygiene test allows two bridge modules. The gap is that the
tick engine and chart still run the `numba` and Python implementations.

## Interfaces produced

```python
# src/q_backend/backtesting/tick/kernel.py   (rewritten as a q_core bridge module; public signature unchanged)
REQUIRED_TICK_FUNCTIONS: Final = ("tick_simulate", "tick_day_bounds", "tick_bars", "resolve_bar_ms", "sample_at_bar_ends")
def check_tick_engine(module: Any) -> None: ...   # ImportError naming missing functions and module.version()

def simulate(bid: np.ndarray, ask: np.ndarray, direction: np.ndarray, sl_points: np.ndarray,
             tp_points: np.ndarray, initial_capital: float, point_value: float, sizing_mode: int,
             sizing_a: float, sizing_b: float, sizing_c: float) -> tuple[np.ndarray, ...]: ...
             # same 9-tuple; arrays length n, zero-filled past trade_count

def simulate_config(bid, ask, direction, sl_points, tp_points, initial_capital: float, point_value: float,
                    sizing: PositionSizingConfig) -> dict[str, np.ndarray | float]: ...   # exact-length ledger
def day_bounds(time_msc: np.ndarray) -> tuple[np.ndarray, np.ndarray]: ...
def bars(ticks: TickArrays, bar_ms: int) -> dict[str, np.ndarray]: ...
def resolve_bar_ms(base_bar_ms: int, span_msc: int) -> int: ...
def sample_at_bar_ends(series: np.ndarray, tick_end: np.ndarray) -> np.ndarray: ...
```

```python
# src/q_backend/backtesting/tick/engine.py   (changed; public surface unchanged)
def _split_ticks_by_day(ticks: TickArrays) -> List[TickArrays]: ...      # slices at kernel.day_bounds
def _events_to_registry(events: tuple, time_msc: np.ndarray, symbol: str, point_value: float) -> TradeRegistry: ...  # unchanged
class TickBacktestEngine: ...                     # _run_single_chunk calls kernel.simulate_config

# src/q_backend/backtesting/tick/orders.py   (changed)
# kept: ExitReason, SIZING_FIXED_QUANTITY, SIZING_FIXED_SAFETY_MARGIN, kernel_sizing_params (rejects unsupported models by name)

# src/q_backend/backtesting/tick/chart_data.py   (changed; serialize_tick_chart_data signature and payload unchanged)
# _resample_ticks_to_bars, _sample_indicator_at_bars and _resolve_bar_ms delegate to kernel; DISPLAY_TIMEFRAME_MS, _msc_to_iso and name validation stay
```

```
tests/backtesting/tick/test_tick_chart_baseline.py   new: chart payloads vs committed JSON, recorded before the swap
tests/backtesting/goldens/tick_chart/                new: one JSON per chart case
tests/backtesting/tick/test_tick_kernel_bridge.py    new: bridge unit tests (padding, sizing rejection, import check)
tests/backtesting/test_indicator_kernels.py          hygiene allowlist gains backtesting/tick/kernel.py
pyproject.toml, uv.lock                              q-core tag -> a release containing Q-029 (and Q-027 if Q-028 has merged)
docs/development/FINDINGS.md                         item 19 (inverse-volatility sizing) gains its triage
```

## Implementation decisions

- **`tick/kernel.py` becomes the bridge and keeps `simulate`'s exact signature and
  tuple.** `test_kernel.py` and `_events_to_registry` depend on that shape, and
  keeping it lets those tests run unedited, which is the strongest regression
  evidence this swap can have. Arrays are allocated at length `n` and filled with
  zeros past `trade_count`; today those slots are uninitialised memory that no
  caller reads, so zeros are a strict improvement and change no observed value.

- **`simulate` converts the `(mode, a, b, c)` scalars back into a sizing mapping
  inside the bridge.** Mode 0 is `{"type": "fixed_quantity", "quantity": a}`;
  mode 1 is `{"type": "fixed_safety_margin", "safety_margin_per_contract": a,
  "min_contracts": int(b), "max_contracts": int(c) or None}`. That is the inverse
  of `kernel_sizing_params`, and Q-029's kernel applies `int()` to the same floats
  numba did, so a fractional `sizing_b` in a direct call truncates the same way.

- **The engine calls `simulate_config` with the pydantic config instead of going
  through the scalars.** The scalar encoding exists only because njit cannot take
  a pydantic model. The engine keeps `kernel_sizing_params` in its constructor
  purely as the validation point, so an unsupported config still fails when the
  engine is built rather than on the first day.

- **`kernel_sizing_params` rejects anything but the two supported types with
  `ValueError("tick engine does not support position sizing type '<type>'")`.**
  Today an inverse-volatility config raises `AttributeError` from an attribute
  lookup, which reads like a bug. The job fails either way; the named error is the
  only observable change in this task, and FINDINGS item 19 gets the triage
  "explicit rejection in Q-030; supporting the model is a feature decision".

- **`_split_ticks_by_day` keeps its return type and slices at `day_bounds`.**
  `TickArrays` slices are views, as today, so no tick data is copied. Only the
  boundary computation moves to `q_core`.

- **Chart functions keep their private names and delegate.** The two chart tests
  exercise `serialize_tick_chart_data`; keeping `_resample_ticks_to_bars`'
  `(bars, bar_ranges)` return lets `serialize_tick_chart_data` stay unchanged. The
  list of per-bar dicts and ISO timestamps is still built in Python, because the
  payload is JSON, but from arrays returned in one call rather than from a numpy
  reduction per bar. `_mid_price` is deleted, because the midpoint-or-last choice
  is inside `q_core.engine.tick_bars`.

- **A non-finite volume sum still fails the chart request.** Q-029 returns an
  error where `int(nan)` raised; the bridge re-raises it as `ValueError` so the job
  fails as it does today.

- **A chart-payload baseline is recorded before the swap.** Two chart tests assert
  a handful of values; the payload the UI renders has far more. The baseline pins
  full payloads for M1 and M15 over `synthetic_ticks()`, an all-zero `last`
  stream, a sparse 40-day stream that doubles the M1 interval, and
  `TickMaBreakout` indicators with NaN warm-up values.

- **`numba` stays in `pyproject.toml` with a comment naming `tsmom.py` and
  `gatev_pairs.py`.** Removing the dependency is not possible while candle
  strategies use it, and a comment keeps the next reader from assuming the tick
  kernel still needs it.

- **Cold-start cost is measured with an empty numba cache.** `cache=True` hides
  compilation after the first run on a machine, so the before measurement sets
  `NUMBA_CACHE_DIR` to a fresh temporary directory in a fresh process. The after
  measurement uses a fresh process too, so both include import time.

## Ordered implementation

1. [x] Work on the branch `Q-030-tick-backtests-on-q-core` in `q_backend`, created
   from `development` by `./work start`. Confirm a `q_core` tag containing Q-029
   exists, and, if Q-028 has merged, that the tag also contains Q-027. If not, set
   the task blocked and stop.
2. [x] Measurement before. Run the commands below: five warm runs and one cold run of
   the tick engine over the golden stream and a 1,000,000-tick stream, and five
   chart serializations of the 1,000,000-tick stream at M1. Record every reading.
   Nothing to commit.
3. [x] Write `tests/backtesting/tick/test_tick_chart_baseline.py` with the five cases in
   the decisions, record with `--regen-goldens`, confirm a second run is green and
   that editing one bar's volume fails with a diff. Commit.
4. [x] Bump the `q-core` tag and refresh `uv.lock`. Run `tests/backtesting` to confirm
   the pin alone changes nothing. Commit.
5. [x] Write failing tests in `tests/backtesting/tick/test_tick_kernel_bridge.py`:
   - `simulate` returns nine elements, arrays of length `n`, zeros past
     `trade_count`, and values up to `trade_count` equal to `simulate_config` on
     the same inputs;
   - mode 1 with `sizing_b = 1.7` behaves as min contracts 1;
   - `kernel_sizing_params(InverseVolatilityPositionSizing())` raises `ValueError`
     naming `inverse_volatility`;
   - `check_tick_engine` on a stub missing `tick_bars` raises `ImportError` naming
     it and the stub's version.

   Confirm they fail. Rewrite `tick/kernel.py` as the bridge and change
   `kernel_sizing_params`. Confirm they pass, and that `test_kernel.py` passes
   unedited. Commit.
6. [x] Swap the engine: `_split_ticks_by_day` slices at `day_bounds`, and
   `_run_single_chunk` calls `simulate_config`. Confirm `test_tick_engine.py`, the
   tick golden and determinism tests, and `tests/optimization` tick runner tests
   pass unchanged. Commit.
7. [x] Swap the chart: delegate `_resolve_bar_ms`, `_resample_ticks_to_bars` and
   `_sample_indicator_at_bars`, delete `_mid_price`. Confirm the chart baseline and
   `test_tick_chart_data.py` pass unchanged. Commit.
8. [x] Update the hygiene allowlist, add a test that no module under
   `backtesting/tick/` imports `numba`, and add the `numba` dependency comment.
   Commit.
9. [x] Write the triage beside FINDINGS item 19. Commit.
10. [x] Regression. Confirm `git diff <step-3 commit> -- tests/backtesting/goldens/tick_ma_breakout.json tests/backtesting/goldens/tick_chart`
    is empty and `git diff development -- tests/backtesting/tick/test_kernel.py tests/backtesting/tick/test_tick_engine.py tests/backtesting/tick/test_tick_chart_data.py tests/backtesting/tick/test_tick_strategy_causality.py`
    is empty. Commit any fixes.
11. [x] Measurement after. Repeat step 2 on the same machine. Nothing to commit.
12. [ ] Human step, matching human-verifiable criterion 1: one day of real WIN$N ticks
    on both branches.
13. [ ] Run the full validation suite. Commit.

## Validation

- **Unit:** tuple shape and zero padding; scalar-to-mapping inverse including
  truncation; named rejection of unsupported sizing; tick import check.
- **Integration:** tick engine in both modes on `q_core`; tick chart payload on
  `q_core`; tick optimizer runner unchanged.
- **Regression:** tick golden byte-identical; chart baseline for five cases; four
  tick test files unchanged and passing.
- **Manual:** real-tick backtest identical across branches.
- **Measurement:** warm and cold tick engine runs and chart serialization, before
  and after, individual readings and medians.
- **Pins:** `q-core` tag bumped; `CONTRACTS_REV` unchanged.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/backtesting/tick tests/backtesting/test_goldens.py -k "tick" -v
uv run pytest tests/backtesting/test_indicator_kernels.py tests/optimization -q
git diff development -- tests/backtesting/tick/test_kernel.py tests/backtesting/tick/test_tick_engine.py \
  tests/backtesting/tick/test_tick_chart_data.py tests/backtesting/tick/test_tick_strategy_causality.py

# measurement (steps 2 and 11); the cold run uses a fresh process and cache directory
NUMBA_CACHE_DIR="$(mktemp -d)" uv run python - <<'PY'
import sys, time
sys.path.insert(0, "tests")
started = time.perf_counter()
from backtesting.test_goldens import run_tick_case
run_tick_case()
print("cold golden tick", round(time.perf_counter() - started, 3))
PY
uv run python - <<'PY'
import statistics, sys, time
import numpy as np
sys.path.insert(0, "tests")
from backtesting.test_goldens import run_tick_case, synthetic_ticks
from q_backend.backtesting.engine import ParallelMode
from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy

def median_of_five(fn):
    readings = []
    for _ in range(5):
        started = time.perf_counter(); fn(); readings.append(time.perf_counter() - started)
    return [round(r, 4) for r in readings], round(statistics.median(readings), 4)

ticks = synthetic_ticks(1_000_000, msc_per_tick=50)
strategy = build_tick_strategy("TickMaBreakout", {"short_period": 20, "long_period": 50, "sl_points": 0.3, "tp_points": 0.6}, "SYNTH")
engine = TickBacktestEngine(strategy, FixedQuantityPositionSizing(quantity=1.0), symbol="SYNTH")
print("golden tick warm", median_of_five(run_tick_case))
print("1M engine warm", median_of_five(lambda: engine.run(ticks, parallel_mode=ParallelMode.DAY_TRADE)))
print("1M chart M1", median_of_five(lambda: serialize_tick_chart_data(ticks, strategy, "M1")))
PY

# human, step 12
pnpm tauri:dev   # in q_frontend; Backtests workspace, tick engine, against each backend branch
```

## Handoff

Report the chart-baseline cases with bar counts, and confirm the baseline was not
regenerated after step 3. Report the final `git diff` results from step 10. Report
cold and warm tick engine readings and medians and chart serialization readings
and medians, before and after. Report the q_core tag pinned, confirm
`CONTRACTS_REV` is unchanged, and quote FINDINGS item 19's triage.
Confirm no module under `backtesting/tick/` imports `numba`, and name the remaining
`numba` users. From the human step, report trade count, headline metrics and the
number of M1 chart bars on both branches.
