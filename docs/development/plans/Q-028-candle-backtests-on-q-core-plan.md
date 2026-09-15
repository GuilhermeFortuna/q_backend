# Q-028 implementation plan: Candle backtests on q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-028-candle-backtests-on-q-core-spec.md`](../specs/Q-028-candle-backtests-on-q-core-spec.md)  
**Depends on:** Q-027

## Current-system context

After Q-023 and Q-024, `BacktestEngine._run_single_chunk`
(`backtesting/engine.py`) calls `augment_indicator_frame(strategy, chunk)`,
builds `signal_arrays(strategy, chunk)` once, parses the three `HH:MM` day-trade
strings (raising `ValueError("Invalid day trade time config (start=..., end=...,
close=...). Must be HH:MM format.")`), and then loops `for i in range(len(chunk))`
over `chunk.iloc[i]` through sections A to E and the end-of-chunk force close,
calling `signal_columns.evaluate_queued_signals(strategy, signals, i, row,
open_trades)` in section D, `sizer.size_signal`/`max_position_size` in C, and
`_close_trade_with_costs` → `TradeRegistry.close_trade` for every close. `run`
converts a non-datetime index, and in `DAY_TRADE` mode loops over
`data.groupby(data.index.date)` and merges registries. Engine callers are
`api/backtest_jobs._execute_candle` (Brasília wall-clock naive index),
`optimization/backtest_runner.BacktestRunner.run` (with `trade_start` when
`warmup_bars > 0`), and through it walk-forward, lockbox, hypothesis and research
acceptance; tests construct it in `test_engine.py`, `test_transaction_costs.py`,
`test_exit_rules.py`, `test_lai_lau_strategies.py`, `test_tsmom_strategy.py`,
`test_hurst_trend_blend.py`, `test_gatev_pairs.py`, `test_composite_entry.py`,
`test_goldens.py` and `tests/api/test_strategy_builder_compile.py`. The only
sizers are `FixedQuantitySizer`, `FixedSafetyMarginSizer` and
`InverseVolatilitySizer` (`position_sizing.py`), and no test subclasses
`PositionSizer`.

`execution/parity.reference_queued_signals_by_close` builds the augmented frame
through a throwaway `StrategyEvaluator`, then calls the Python consumer per bar;
`StrategyEvaluator._evaluate_row` calls the same consumer, so today's parity test
compares Python with Python. `backtesting/indicator_kernels.py` is the only
module importing `q_core` (enforced by `test_indicator_kernels.py` line 153) and
validates `REQUIRED_KERNELS` at import. `pyproject.toml` pins `q-core` as a uv git
source by tag (`v2026.09.14` after Q-023). After Q-027 the wheel exposes
`q_core.engine.run_candle`, `DecisionStep`, `size_entry` and `required_columns`,
all gated by `candle_engine` and `decision_step` fixtures exported from this
engine at `067e29c`. `FINDINGS.md` item 11 records `gatev_pairs` overwriting
`open`/`high`/`low`/`close` and writing a float `spread`, and expects its
resolution here. The gap is that the engine still runs its own row loop and the
parity reference never touches `q_core`.

## Interfaces produced

```python
# src/q_backend/backtesting/candle_kernel.py   (new; a q_core bridge module)
REQUIRED_ENGINE_FUNCTIONS: Final = ("run_candle", "DecisionStep", "size_entry", "required_columns")

def check_engine(module: Any) -> None: ...        # ImportError naming missing functions and module.version()

@dataclass(frozen=True)
class KernelSizing:
    mapping: dict[str, object]                    # PositionSizingConfig-shaped dict
    point_value: float                            # the sizer's own point value (1.0 unless inverse volatility)
    needs_volatility: bool

def sizer_to_kernel(sizer: PositionSizer) -> KernelSizing: ...    # TypeError naming type(sizer).__name__
def parse_day_trade_times(start: str, end: str, close: str) -> tuple[int, int, int]: ...  # microseconds since midnight; today's ValueError
def wall_clock_us(index: pd.DatetimeIndex) -> np.ndarray: ...    # int64; the index's own wall clock, floor to microseconds

@dataclass(frozen=True)
class ChunkRun:
    trades: dict[str, np.ndarray]                 # q_core.engine.run_candle ledger columns
    exit_reason_text: list[str | None]

def run_chunk(
    strategy: TradingStrategy,
    chunk: pd.DataFrame,                          # augmented frame
    signals: SignalArrays,                        # Q-024
    *,
    sizing: KernelSizing,
    initial_capital: float,
    point_value: float,
    costs: TransactionCostConfig | None,
    day_trade_us: tuple[int, int, int] | None,
    force_close_at_end: bool,
    trade_start: datetime.datetime | None,
) -> ChunkRun: ...

def ledger_to_registry(run: ChunkRun, index: pd.DatetimeIndex, *, symbol: str, point_value: float) -> TradeRegistry: ...

def reference_decisions(
    strategy: TradingStrategy,
    frame: pd.DataFrame,                          # augmented frame
    signals: SignalArrays,
    open_trades: list[Trade],
) -> list[tuple[list[Signal], list[Signal]]]: ...  # per bar (exits, entries), via one DecisionStep
```

```python
# src/q_backend/backtesting/engine.py   (changed; public surface unchanged)
class BacktestEngine:
    def __init__(self, strategy, sizer, initial_capital=100000.0, point_values=None, day_trade=False,
                 day_trade_start_time="09:00", day_trade_end_time="16:00", day_trade_close_time="17:00",
                 costs=None): ...                 # additionally resolves KernelSizing once
    def run(self, data, parallel_mode=ParallelMode.SEQUENTIAL, trade_start=None) -> TradeRegistry: ...
    def _run_single_chunk(self, chunk, force_close_at_end, trade_start=None) -> TradeRegistry: ...  # augment, signal_arrays, run_chunk, ledger_to_registry
# removed: _close_trade_with_costs and the per-bar loop
```

```python
# src/q_backend/execution/parity.py   (changed; signatures unchanged)
def reference_queued_signals_by_close(strategy, data, *, timeframe, open_trade=None): ...  # uses candle_kernel.reference_decisions
```

```
tests/backtesting/test_engine_registry_baseline.py    new: full registry and orders vs committed JSON, recorded before the swap
tests/backtesting/goldens/registry/                   new: one JSON per registry-baseline case
tests/backtesting/test_candle_kernel_bridge.py        new: bridge unit tests
tests/backtesting/test_indicator_kernels.py           hygiene allowlist gains candle_kernel.py
tests/execution/test_parity_reference.py              new: the reference comes from q_core and a tampered reason fails parity
pyproject.toml, uv.lock                               q-core tag -> the Q-027 release
docs/development/FINDINGS.md                          item 11 triage decision
```

## Implementation decisions

- **A second bridge module, `backtesting/candle_kernel.py`, rather than growing
  `indicator_kernels.py`.** Q-023 made one module the only importer of `q_core`
  so that the boundary is reviewable. Indicators and the engine are separate
  concerns with separate required-function lists, and folding them would make
  every indicator change touch the engine bridge. The hygiene test becomes an
  explicit allowlist of two paths, which keeps the boundary just as visible.

- **The bridge validates `REQUIRED_ENGINE_FUNCTIONS` at import.** A stale wheel
  must fail at startup with the missing names and `q_core.version()`, exactly as
  `check_kernels` does, instead of failing inside the first backtest job.

- **The sizer is mapped by exact type, once, in the constructor.** The three
  concrete sizers carry all their configuration as attributes, so each maps to
  the `PositionSizingConfig` dict the kernel takes, plus
  `InverseVolatilitySizer.point_value`. `type(sizer) is ...` rather than
  `isinstance` rejects a subclass that overrides `size_signal`, whose behaviour
  the kernel could not reproduce. No subclass exists today, so this changes no
  result.

- **Wall-clock microseconds are `index.tz_localize(None)` for aware indexes and
  the index itself otherwise, as `datetime64[ns]` integers floor-divided by
  1000.** `timestamp.time()` and `timestamp.date()` read the index's own wall
  clock, which is exactly what removing the timezone without conversion gives.
  Flooring matches `Timestamp.time()`, which drops nanoseconds, including before
  1970. Q-025's frame rejects sub-microsecond times, but the engine never did, so
  the bridge floors instead of rejecting.

- **`parse_day_trade_times` is the engine's existing parsing moved, not
  rewritten.** Its `ValueError` text is observable through the API. It is called
  only when `day_trade` is true, as today, so malformed strings with day trade
  off still do not raise.

- **`trade_start` becomes the mask `~(chunk.index < trade_start)`.** That is the
  per-row comparison vectorised. A comparison that raises today (aware index,
  naive start) still raises, before any simulation.

- **Price columns are passed only when present, and the volatility column only
  when the sizer needs it.** `row.get("open", row.get("close", 0.0))` and the
  exit rules' `row.get("high", close)` fallbacks are reproduced by the kernel from
  absent inputs, so passing a column of zeros instead would change close-only
  series. Volatility is read with `pd.to_numeric(errors="coerce")`, because
  `_read_volatility` turns anything `float()` rejects into "no volatility", which
  is NaN to the kernel.

- **Registry objects are still built by `TradeRegistry`.** `ledger_to_registry`
  creates `Order` and `Trade` objects with fresh UUIDs, sets `commission` to the
  kernel's total cost, and closes trades with `registry.close_trade(id,
  index[exit_bar], exit_price, exit_reason)`. The registry's own pnl formula then
  produces `pnl`, so trade objects come from the same code the tick engine and
  the API already depend on. The bridge asserts the registry pnl equals the
  kernel pnl bit for bit, which turns any formula drift into a loud failure.
  Entry and exit times are `index[bar]`, the same `Timestamp` objects
  `chunk.iloc[i].name` returned.

- **The day-trade parallel split stays in `run`.** It recomputes indicators per
  day, which is Python work, and Q-027's kernel is deliberately a single chunk.

- **The parity reference moves to `q_core` now, and the evaluator stays in
  Python.** Once the engine runs on the kernel, a Python-only reference would
  make `test_backtest_live_parity` compare two Python paths that no backtest uses.
  Driving the reference with one `DecisionStep` across all bars makes the test
  compare the kernel's decision semantics with the live evaluator on every golden
  case until Q-031. `reference_decisions` resolves an open trade's entry bar with
  `index.get_indexer([entry_time])`, the Q-024 consumer's rule, and maps queued
  exits and entries back to `Signal` objects so `signals_equal` is unchanged.

- **The Python decision consumer and `ExitStrategy.check_exits` are kept, with a
  docstring naming their single remaining caller.** The forward evaluator still
  needs them, and deleting them here would be Q-031's swap done without its
  benchmark gate. This is a known, bounded second implementation of section D
  that exists for the length of one task, and the parity test compares the two on
  every golden case while it exists.

- **A registry baseline is recorded before the swap, in addition to the goldens.**
  The goldens pin every trade field except symbol and ids, but they do not pin
  orders, and they do not cover `trade_start`, costs with a non-unit point value
  in `DAY_TRADE` mode, or timestamp types. The baseline test records, per case,
  every trade field except `id`/`order_id`, the type name of `entry_time` and
  `exit_time`, and each order's symbol, action, type and quantity, and it honours
  `--regen-goldens` only before the swap commit.

- **FINDINGS item 11 is triaged as "no action".** The kernel takes plain columns
  (Q-027), so no Q-025 frame is built on the backtest path and the reserved-name
  collision cannot occur. The pairs strategy's fills at its synthetic prices are
  today's behaviour and stay pinned by the `gatev_pairs` golden. The triage text
  says both.

- **Costs are measured, not assumed.** Small golden frames may get slower from
  the conversion overhead while long series get much faster, so both are measured:
  three golden cases and a 50,000-bar synthetic series, five runs each before and
  after, medians reported whichever way they move.

## Ordered implementation

1. Work on the branch `Q-028-candle-backtests-on-q-core` in `q_backend`, created
   from `development` by `./work start`. Confirm Q-024 is merged (engine section D
   calls `signal_columns.evaluate_queued_signals` with `SignalArrays`) and that a
   `q_core` tag containing Q-027 exists
   (`git ls-remote --tags https://github.com/GuilhermeFortuna/q_core.git`). If
   either is missing, set the task blocked and stop.
2. Measurement before. Run the engine timing command below five times per case for
   the three golden cases and the 50,000-bar series. Record every reading. Nothing
   to commit.
3. Write `tests/backtesting/test_engine_registry_baseline.py` with cases: every
   candle golden case through `run_candle_case`'s engine construction;
   `ma_crossover_day_trade` through `run` in `DAY_TRADE` mode; an MA crossover with
   `trade_start` at bar 120 through `BacktestRunner`-style `run(..., trade_start=)`;
   an MA crossover with costs `{cost_per_contract: 1.5, cost_bps: 3}` and point
   value 0.2 in `DAY_TRADE` mode on 15-minute bars; `trb_fixed_holding`; and
   `gatev_pairs`. Record with `--regen-goldens`, confirm a second run is green,
   and confirm that editing one order quantity in the JSON fails with a diff.
   Commit.
4. Write failing tests in `tests/backtesting/test_candle_kernel_bridge.py`:
   - `sizer_to_kernel` maps each of the three sizers to its config dict and point
     value, and a subclass of `FixedQuantitySizer` raises `TypeError` naming the
     subclass;
   - `parse_day_trade_times("9:0", "16:00", "17:00")` returns microseconds, and
     `"09-00"` raises today's message;
   - `wall_clock_us` of a UTC index and of the same instants in
     `America/Sao_Paulo` differ by the offset, of a naive index equals its
     microsecond integers, and of `1969-12-31 23:59:59.999999999` floors to
     `-1`;
   - `check_engine` on a stub module missing `DecisionStep` raises `ImportError`
     naming it and the stub's version.

   Confirm they fail. Implement those four functions only. Confirm they pass.
   Commit.
5. Bump the `q-core` tag in `pyproject.toml` to the Q-027 release and refresh
   `uv.lock`. Run the Q-022 indicator tests, the goldens and the full
   `tests/backtesting` suite to confirm the pin alone changes nothing. Commit.
6. Write failing tests for `run_chunk` and `ledger_to_registry` on the
   `ma_crossover_trailing` golden frame: the registry from the bridge equals the
   registry from the unmodified engine in every recorded baseline field; a
   deliberately wrong `commission` makes the pnl assertion raise. Confirm they
   fail. Implement both. Confirm they pass. Commit.
7. Swap the engine. `_run_single_chunk` computes the augmented frame and
   `SignalArrays` as before, then `run_chunk` and `ledger_to_registry`; delete the
   loop and `_close_trade_with_costs`; resolve `KernelSizing` in `__init__`. Confirm
   the goldens, the registry baseline, the queued-signal baseline, and
   `test_engine.py`, `test_transaction_costs.py`, `test_exit_rules.py`,
   `test_lai_lau_strategies.py`, `test_tsmom_strategy.py`,
   `test_hurst_trend_blend.py`, `test_gatev_pairs.py`, `test_composite_entry.py`
   and `tests/api/test_strategy_builder_compile.py` pass with unchanged expected
   values. Commit.
8. Write failing tests in `tests/execution/test_parity_reference.py`: patching
   `candle_kernel.reference_decisions` to raise makes
   `reference_queued_signals_by_close` raise (proving the reference comes from
   `q_core`); replacing one reference exit's `exit_reason` with `"fixed_sl"` makes
   the parity comparison for `ma_crossover_trailing` with an open trade fail.
   Confirm they fail. Implement `reference_decisions` and switch the reference.
   Confirm `test_backtest_live_parity` passes for every case with and without an
   open trade. Commit.
9. Update the hygiene test to the two-path allowlist and add a test that the
   engine module contains no `iloc[` and no `for i in range(len(` over the frame.
   Add docstrings naming the evaluator as the only remaining caller of the Python
   consumer and `ExitStrategy.check_exits`, and a test that
   `grep -rn "evaluate_queued_signals\|check_exits" src/q_backend/backtesting` finds
   only definitions and re-exports. Commit.
10. Write the FINDINGS item 11 triage beside the item. Commit.
11. Regression. Confirm `git diff <step-3 commit> -- tests/backtesting/goldens` is
    empty and that the diff against `development` of `test_goldens.py`,
    `test_strategy_causality.py`, `genome/test_node_causality.py`,
    `test_composite_genome_causality.py`, `test_genome_parity.py` and
    `test_signal_baseline.py` is empty. Commit any fixes.
12. Measurement after. Repeat step 2 on the same machine. Nothing to commit.
13. Human step, matching human-verifiable criterion 1: the three real-lake
    backtests on both branches.
14. Human step, matching human-verifiable criterion 2: a 50-trial optimization
    study with a fixed seed on both branches.
15. Run the full validation suite. Commit.

## Validation

- **Unit:** sizer mapping and subclass rejection; day-trade parsing and its
  message; wall-clock conversion for aware, naive and pre-1970 indexes; engine
  import check; bridge registry equality and the pnl assertion.
- **Integration:** engine on the kernel for every golden case, both parallel modes,
  trade start, costs with non-unit point value, holding period and pairs; parity
  reference driven by `q_core` against the Python evaluator.
- **Regression:** 19 golden files byte-identical; registry baseline including
  orders; queued-signal baseline, determinism, parity, genome parity and causality
  suites with unchanged bodies; engine-level strategy tests with unchanged expected
  values.
- **Manual:** three real-lake backtests identical across branches; optimization
  study timing.
- **Measurement:** three golden cases and a 50,000-bar series, five runs each,
  before (step 2) and after (step 12), individual readings and medians.
- **Pins:** `q-core` at the Q-027 tag; `CONTRACTS_REV` unchanged.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/backtesting/test_goldens.py tests/backtesting/test_engine_registry_baseline.py \
  tests/backtesting/test_signal_baseline.py tests/backtesting/test_candle_kernel_bridge.py \
  tests/execution/test_parity_reference.py tests/backtesting/test_indicator_kernels.py -v
uv run pytest tests/backtesting tests/execution tests/api/test_strategy_builder_compile.py -q
git diff --stat "$(git log --format=%H -1 -- tests/backtesting/goldens/registry)" -- tests/backtesting/goldens

# measurement (steps 2 and 12)
for case in ma_crossover_baseline composite_majority_three genome_ma_session_gate; do
  uv run python -c "import statistics, time, sys; sys.path.insert(0, 'tests'); \
from backtesting.test_goldens import CANDLE_CASES, run_candle_case; c = CANDLE_CASES['$case']; \
t = [(time.perf_counter(), run_candle_case(c), time.perf_counter()) for _ in range(5)]; \
print('$case', [round(b - a, 3) for a, _, b in t], 'median', round(statistics.median(b - a for a, _, b in t), 3))"
done
uv run python - <<'PY'
import statistics, sys, time
sys.path.insert(0, "tests")
from backtesting.test_goldens import SYMBOL, synthetic_ohlcv
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import FixedQuantitySizer

data = synthetic_ohlcv(50_000)
params = {"short_period": 5, "long_period": 20, "trailing_stop_pct": 0.03}
readings = []
for _ in range(5):
    engine = BacktestEngine(build_strategy("MACrossover", params, SYMBOL), FixedQuantitySizer())
    started = time.perf_counter()
    engine.run(data.copy())
    readings.append(time.perf_counter() - started)
print("50k", [round(r, 3) for r in readings], "median", round(statistics.median(readings), 3))
PY

# human, steps 13 and 14
pnpm tauri:dev   # in q_frontend; Backtests and Optimization workspaces against each backend branch
```

## Handoff

Report the registry-baseline case list with trade and order counts, and confirm
the baseline JSON was not regenerated after step 3. Report the final goldens
`git diff` result and the unchanged-file diffs from step 11. List every engine-level
test file that ran unchanged. Report the three golden cases' and the 50,000-bar
series' individual readings and medians before and after. Report the q_core tag
pinned and confirm `CONTRACTS_REV` did not change. Quote the FINDINGS item 11
triage. Confirm the parity reference now comes from `q_core` and name the
remaining Python callers of the decision consumer and `check_exits`. From the human
steps, report trade counts and headline metrics for the three backtests on both
branches, and the optimization study's wall time and best-trial metrics on both.
