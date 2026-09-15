# Q-024 implementation plan: Columnar strategy signals

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-024-columnar-strategy-signals-spec.md`](../specs/Q-024-columnar-strategy-signals-spec.md)  
**Depends on:** Q-023

## Current-system context

`TradingStrategy` in `backtesting/strategy.py` declares four abstract methods:
`compute_indicators`, `check_entry_conditions(row)` (line 77),
`check_exit_conditions(row, open_trades)` (line 91), and `get_chart_indicators`.
Thirteen classes implement them: `MACrossoverStrategy` (in `strategy.py`),
`MACDStrategy`, `RSIMeanReversionStrategy`, `BollingerReversionStrategy`,
`DonchianBreakoutStrategy`, `VMAStrategy`, `FMAStrategy`, `TRBStrategy`,
`TSMOMStrategy`, `HurstTrendBlendStrategy`, `GatevPairsStrategy` (in
`strategies/`), `CompositeStrategy` (`genome/composite_strategy.py:524-560`),
and `CompositeEntryStrategy` (`composite_entry.py:98-121`). Eleven of them are
registered, the genome interpreter is registered as `CompositeStrategy`, and
`CompositeEntryStrategy` is built by `factory.build_composite_entry`. Four test
files add five test doubles: `test_engine.py`, `test_transaction_costs.py`,
`test_exit_rules.py`, and `test_strategy.py`.

Every one of the thirteen already computes vectorised trigger columns
(`buy_signal`, `sell_signal`, `exit_long_signal`, `exit_short_signal`,
`exit_signal`, `net_stance`, `rebalance`, `signal_strength`, `bar_index`), and
most row methods only re-read them through `current_data.get(col, False)`.
Five decisions exist only in the row methods:

- Long entries win over short ones (`if buy … elif sell`). RSI can raise both.
- TSMOM and Hurst in `rebalance_on_every_bar` mode enter on any rebalance bar
  with non-zero momentum and close on any rebalance bar
  (`tsmom.py:185-230`, `hurst_trend_blend.py:142-187`), while their
  `buy_signal`/`sell_signal` columns hold only the flip triggers.
- Strength comes from `signal_strength` for TSMOM and Hurst and is 1.0
  elsewhere.
- Composite-entry exits compare the trade's side to `net_stance`.
- The fixed holding period (`lai_lau_common.fixed_holding_period_exits:60-84`)
  looks up `trade.entry_time` in `_timestamp_to_bar`, a Series that
  `compute_indicators` stores on the instance. TRB, FMA, and any genome whose
  plan has `fixed_holding_period` use it.

`resolve_symbol` (`strategy.py:14-22`) returns the strategy's default symbol on
every real call path, because rows from `iloc` and `loc` are named by a
`Timestamp`. `GatevPairsStrategy` looks stateful, but its numba state machine
tracks the strategy's own spread position inside `compute_indicators`, so its
`exit_signal` is already a pure column. Tick strategies already follow the
target shape: `TickStrategy.compute_signals` returns aligned arrays, and the
tick kernel reads an `int8`-style direction.

The same per-row decision is consumed in three places:

- `BacktestEngine._run_single_chunk` section D (the block commented
  `# D. Evaluate this (now-closed) bar`; `engine.py:253-290` on `development`
  at `144345a`, about 30 lines earlier once Q-023 replaces the exit-column block
  at `engine.py:125-155` with one call to `augment_indicator_frame`, which Q-023
  moves into the backtesting layer and `execution/indicator_frame.py`
  re-exports) calls `exit_strategy.check_exits`, then
  `check_exit_conditions` for trades whose symbol no exit rule closed, then
  `check_entry_conditions`. The day-trade branch repeats this.
- `execution/signal_eval.evaluate_queued_signals` (lines 13-35) mirrors
  section D for `StrategyEvaluator._evaluate_row` (`evaluator.py:222`). The
  evaluator reaches it from `ingest_completed_bars`, which recomputes the
  augmented frame and takes `augmented.loc[open_time]` (lines 186-216).
- `execution/parity.reference_queued_signals_by_close` (lines 43-61) runs the
  same function to give `test_backtest_live_parity` its reference.

Strategy exits carry `exit_reason=None`, which the engine records as `"SIGNAL"`
and `_domain_action_from_queued` reports as `"exit_rule"`. Only `ExitStrategy`
rule signals carry reasons. `CompositeEntryStrategy._instance_edge_columns`
(lines 47-65) falls back to calling a member's `check_entry_conditions` per row
when the member lacks `buy_signal`/`sell_signal`. The golden suite
(`tests/backtesting/test_goldens.py`, 12 candle cases) pins MACrossover, RSI,
the composite managers, and one genome. It does not pin MACD, Bollinger,
Donchian, VMA, FMA, TRB, TSMOM, Hurst, pairs, or the day-trade path. The gap is
that the decision lives in per-row Python methods that a kernel cannot call.
Nothing locks the behaviour of more than half the strategies before those
methods are removed.

## Interfaces produced

```python
# src/q_backend/backtesting/signal_columns.py   (new)
SIGNAL_ENTRY: Final = "q_signal_entry"            # int8: +1 queue BUY, -1 queue SELL, 0 nothing
SIGNAL_EXIT_LONG: Final = "q_signal_exit_long"    # bool: close every open BUY trade on strategy.symbol
SIGNAL_EXIT_SHORT: Final = "q_signal_exit_short"  # bool: close every open SELL trade on strategy.symbol
SIGNAL_STRENGTH: Final = "q_signal_strength"      # float64: in [0.0, 1.0] where entry != 0; exactly 0.0 where entry == 0
SIGNAL_COLUMNS: Final = (SIGNAL_ENTRY, SIGNAL_EXIT_LONG, SIGNAL_EXIT_SHORT, SIGNAL_STRENGTH)
BAR_INDEX: Final = "bar_index"                    # int64 frame-relative bar counter; required iff holding_period_bars is not None

class SignalContractError(ValueError): ...        # message names the strategy class and the offending column

def write_signal_columns(
    df: pd.DataFrame,
    *,
    entry_long: pd.Series,                        # bool dtype; NaN is rejected, not coerced
    entry_short: pd.Series,                       # bool dtype; long wins where both are True
    exit_long: pd.Series | bool,                  # bool dtype Series, or a scalar False for "never"
    exit_short: pd.Series | bool,
    strength: pd.Series | float = 1.0,            # sampled only where entry != 0
) -> pd.DataFrame: ...                            # mutates and returns df

def validate_signal_columns(frame: pd.DataFrame, *, strategy_name: str, holding_period_bars: int | None) -> None: ...

@dataclass(frozen=True)
class SignalArrays:
    index: pd.DatetimeIndex                       # the indicator frame's index, for entry-bar lookup
    entry: np.ndarray                             # int8, len n
    exit_long: np.ndarray                         # bool, len n
    exit_short: np.ndarray                        # bool, len n
    strength: np.ndarray                          # float64, len n
    bar_index: np.ndarray | None                  # int64, len n; present iff holding_period_bars is not None
    holding_period_bars: int | None               # bars; exit when bar_index[now] - bar_index[entry] >= this

def signal_arrays(strategy: TradingStrategy, frame: pd.DataFrame) -> SignalArrays: ...   # validates, then extracts once per frame

def evaluate_queued_signals(
    strategy: TradingStrategy,
    signals: SignalArrays,
    position: int,                                # row position of the evaluated (closed) bar in the frame
    current_data: pd.Series,                      # that bar's row; read only by ExitStrategy rules
    open_trades: list[Trade],
) -> tuple[list[Signal], list[Signal]]: ...       # (pending_exits, pending_entries), engine section D order
```

```python
# src/q_backend/backtesting/strategy.py   (changed)
class TradingStrategy(ABC):
    symbol: str                                   # every signal and exit filter uses this
    @abstractmethod
    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame: ...   # contract now includes SIGNAL_COLUMNS
    @abstractmethod
    def get_chart_indicators(self) -> List[ChartIndicatorSpec]: ...
    @property
    def holding_period_bars(self) -> int | None: ...                        # default None
# removed: TradingStrategy.check_entry_conditions, TradingStrategy.check_exit_conditions, resolve_symbol
```

```python
# src/q_backend/backtesting/strategies/trb.py, fma.py   (changed)
@property
def holding_period_bars(self) -> int | None: ...  # self.holding_period
# src/q_backend/backtesting/genome/composite_strategy.py   (changed)
@property
def holding_period_bars(self) -> int | None: ...  # self.plan.fixed_holding_period
# src/q_backend/backtesting/strategies/lai_lau_common.py   (changed)
# removed: build_timestamp_to_bar, fixed_holding_period_exits; kept: add_bar_index
```

```python
# src/q_backend/execution/signal_eval.py   (changed: re-export only)
from q_backend.backtesting.signal_columns import evaluate_queued_signals, signal_arrays

# src/q_backend/execution/parity.py   (changed; signature unchanged)
def reference_queued_signals_by_close(strategy: TradingStrategy, data: pd.DataFrame, *, timeframe: str,
                                      open_trade: Optional[Trade] = None) -> list[tuple[datetime, list[Signal], list[Signal]]]: ...
```

```
tests/backtesting/test_goldens.py                CandleCase gains parallel_mode and day_trade fields; six cases added
tests/backtesting/goldens/                       six new golden files; the existing 13 untouched
tests/backtesting/test_signal_baseline.py        new: per-bar queued signals vs committed JSON
tests/backtesting/goldens/signals/               new: one JSON per baseline case
tests/backtesting/test_signal_columns.py         new: writer, validator, consumer unit tests
tests/backtesting/test_signal_contract.py        new: per-strategy column mapping and dtypes
```

## Implementation decisions

- **The contract is four new `q_signal_*` columns. It does not reuse
  `buy_signal`/`sell_signal`.** In `rebalance_on_every_bar` mode, TSMOM's and
  Hurst's `buy_signal` holds the flip trigger, not the entry the strategy
  actually queues. `CompositeEntryStrategy` reads its members' flip triggers,
  and the `composite_*` goldens depend on that. Redefining `buy_signal` would
  change composite results. The prefix avoids every name in use today:
  `signal_strength` (TSMOM, Hurst), `sig_1..3` (Hurst),
  `entry_long_signal`/`exit_long_signal` (genome), `g_*` (genome nodes), and
  `e0__*` (composite).

- **The entry is one `int8` column, not two booleans.** RSI raises both
  triggers when the previous RSI is at or below the oversold level and the
  current RSI is above the overbought level. Today `if buy … elif sell` resolves
  that silently. One column with long written last makes the tie impossible to
  express, so neither the Python consumer nor the Q-027 kernel has to repeat the
  tie-break. `int8` matches the tick kernel's direction encoding and Q-025's
  declared column types.

- **Exits are two booleans keyed by the side of the open trade, not a target
  stance.** GatevPairs' `exit_signal` and rebalance-every-bar close trades of
  both sides on the same bar. A stance column cannot say "close whatever is
  open", and exits never depend on anything about the trade except its side and
  symbol.

- **Strength is `float64`, exactly 0.0 on bars without an entry, and in [0, 1]
  on entry bars.** `Signal.strength` is a Python float that sizers multiply into
  quantity, so `float32` would move quantities in the tenth decimal and break the
  goldens. On bars without an entry, TSMOM's `signal_strength` is NaN during
  t-stat warm-up. Writing 0.0 there makes the column NaN-free for the kernel, and
  it reads as "no entry, no strength". The validator rejects NaN, negative, and
  greater-than-one values on entry bars.

- **There is no exit-reason column and no symbol column.** Every strategy exit
  today has `exit_reason=None`, and `resolve_symbol` falls back to
  `strategy.symbol` on every call path. Columns for either would repeat a
  constant on each row. `TradingStrategy` declares `symbol: str`, which all
  thirteen classes already set. Exit-rule reasons keep coming from
  `ExitStrategy`.

- **The fixed holding period is a declared scalar (`holding_period_bars`) and
  not a column. The consumer finds the trade's entry bar by exact lookup of
  `trade.entry_time` in the frame index with `DatetimeIndex.get_indexer`, and
  closes when `bar_index[position] - bar_index[entry] >= holding_period_bars`.
  A lookup of -1 never closes.**
  - The rule needs the bar where the trade actually filled, and no column can
    know that. An entry signal does not imply a fill: the sizer can return
    `None`, the position cap can skip the order, and the parity test's open
    trade was never opened by a signal.
  - `get_indexer` matches `Series.get` for the cases that matter, which was
    checked against the installed pandas. An aware timestamp that is present
    matches. A naive or off-grid timestamp returns no match, so the live quirk
    is preserved.
  - A scalar per strategy is the parameter Q-027 takes, and a per-row column
    would allow per-row variation the rule does not have.
  - A strategy that declares a holding period writes all-False exit columns, and
    the validator enforces it. TRB and FMA never used their triggers to exit, and
    a genome returns early from its holding-period branch. Columns that claimed
    exits the consumer ignores would mislead the next reader.

- **The fixed holding period is not folded into `TimeStopRule`.** Section D runs
  `on_bar` on the fill bar, so `TimeStopRule` counts one there and fires at the
  entry bar plus N minus 1. The holding period fires at the entry bar plus N. The
  time stop would also label the exit `time_stop` and take precedence over
  strategy exits. Each difference changes trades, and exit rules belong to
  Q-026.

- **The consumer lives in `backtesting/signal_columns.py`, and
  `execution/signal_eval.py` re-exports it under the existing name.** The engine
  must not import the `q_backend.execution` package. That package's
  `__init__` imports `StrategyEvaluator`, which imports `strategy_build`, which
  imports `backtesting.factory`, which reimports the package that is importing
  the engine. Q-023 resolves the same problem for `augment_indicator_frame` in
  the same way, moving it into the backtesting layer and re-exporting it from
  `execution/indicator_frame.py`, so both engine dependencies point one way.
  A re-export keeps the evaluator, `parity.py`, and their tests on
  the name they already import, and it keeps one definition.

- **Columns are validated and pulled into arrays once per indicator frame by
  `signal_arrays`, and `evaluate_queued_signals` indexes them by position.**
  The engine computes indicators once per chunk and the evaluator once per
  ingested bar. Both call `signal_arrays` right after, so validation sits on the
  one path Q-023 leaves, and the decision is never a pandas `.get` per bar.
  Arrays indexed by position are the input shape Q-027's kernel takes.

- **`evaluate_queued_signals` keeps section D's order exactly:**
  1. `exit_strategy.check_exits(open_trades, current_data)`.
  2. `closed_symbols` from those exits.
  3. Strategy exits, one `CLOSE(symbol=strategy.symbol)` per open trade not in
     `closed_symbols`, in `open_trades` order.
  4. At most one entry.

  `signals_equal` in `parity.py` compares list lengths, so a trade-count
  difference would fail parity. The goldens record exit reasons, so exits keep
  `exit_reason=None` and the engine keeps writing `"SIGNAL"`.

- **The row is still passed alongside the arrays.** `ExitStrategy` rules read
  rows, and so does `size_signal`'s `current_data`. Both stay row-based until
  Q-026.

- **Engine section D keeps its day-trade gating (`current_time < close_t`, not
  last bar of the day, entry window) and calls `evaluate_queued_signals` in
  both branches.** Clearing entries outside the window is engine policy, not a
  strategy decision. Calling one function removes the duplicated
  exit-then-entry sequence, which is the code the kernel will replace.

- **The evaluator finds `position` as the last row whose index equals
  `open_time`.** Today `augmented.loc[open_time]` takes `.iloc[-1]` when a
  lookup returns several rows. Resolving the position the same way keeps
  duplicate-timestamp behaviour identical.

- **`check_entry_conditions`, `check_exit_conditions`, `resolve_symbol`,
  `fixed_holding_period_exits`, `build_timestamp_to_bar`, and every
  `_timestamp_to_bar` attribute are deleted.** Invariant 1 forbids a second
  implementation of shared semantics. A row method kept "for tests" would be a
  second definition of each strategy's decision, and nothing would keep it equal
  to the columns. Test doubles are rewritten to write the four columns. Tests
  that asserted on row-method output assert on column values instead, with the
  same expected bars.

- **Legacy trigger columns keep their names, values, and dtypes.**
  - `CompositeEntryStrategy` reads members' `buy_signal`/`sell_signal`.
  - The genome `ind.tsmom` node exposes `buy_signal` as a port
    (`node_specs.py:440`).
  - `test_genome_parity.py` asserts on `buy_signal`, `sell_signal`, and the
    exit columns.
  - The causality guardrail compares every added column.

- **`write_signal_columns` rejects non-bool trigger inputs rather than calling
  `fillna(False)`.** Row code tested Python truthiness, and `bool(nan)` is
  `True`. A NaN trigger meant an entry per row but would mean no entry after
  `fillna`. The baseline step asserts that every legacy trigger column is `bool`
  dtype today on the baseline frames, which proves the two readings agree
  instead of assuming it.

- **Per-strategy mapping, which the contract tests assert column for column:**

  | Strategy | `q_signal_entry` +1 / -1 | exit long | exit short | strength | `holding_period_bars` |
  |---|---|---|---|---|---|
  | MACrossover, MACD, RSIMeanReversion, DonchianBreakout, VMA | `buy_signal` / `sell_signal` | `sell_signal` | `buy_signal` | 1.0 | None |
  | BollingerReversion | `buy_signal` / `sell_signal` | `exit_long_signal` | `exit_short_signal` | 1.0 | None |
  | FMA, TRB | `buy_signal` / `sell_signal` | False | False | 1.0 | `holding_period` |
  | TSMOM, HurstTrendBlend, `rebalance_on_every_bar=False` | `buy_signal` / `sell_signal` | `sell_signal` | `buy_signal` | `signal_strength` | None |
  | TSMOM, HurstTrendBlend, `rebalance_on_every_bar=True` | `rebalance & momentum > 0` / `rebalance & momentum < 0` | `sell_signal \| rebalance` | `buy_signal \| rebalance` | `signal_strength` | None |
  | GatevPairs | `buy_signal` / `sell_signal` | `exit_signal` | `exit_signal` | 1.0 | None |
  | CompositeEntryStrategy | `net_long_signal` / `net_short_signal` | `net_stance == -1` | `net_stance == 1` | 1.0 | None |
  | CompositeStrategy, no fixed-holding side | `entry_long_signal` / `entry_short_signal` | `exit_long_signal`, False if absent | `exit_short_signal`, False if absent | 1.0 | None |
  | CompositeStrategy, either side `exit.fixed_holding` | `entry_long_signal` / `entry_short_signal` | False | False | 1.0 | `plan.fixed_holding_period` |

  NaN momentum compares False, which reproduces the `pd.isna(mom)` guard in
  TSMOM and Hurst. A genome with an `exit.rebalance` side has no exit column and
  never closes on that side, and a `middle_band` long exit drives both sides.
  Both are today's behaviour, and the table reproduces them.

- **`CompositeEntryStrategy`'s fallback for members without
  `buy_signal`/`sell_signal` reads the member's `q_signal_entry` (+1 as buy, -1
  as sell).** That is what the per-row `check_entry_conditions` loop produced.
  The `manager.combine` loop over rows stays, because it runs once per frame
  (spec non-goal).

- **Behaviour is locked before anything changes, in two layers.**
  - Six golden cases cover what the goldens miss: `trb_fixed_holding`,
    `bollinger_band_exits`, `tsmom_trend_rebalance_every_bar_strength`
    (strength-scaled fixed quantity), `hurst_rebalance_every_bar_strength`,
    `gatev_pairs` (a data override adding `close_a`, `close_b`, `open_a`,
    `open_b`, as in `test_strategy_causality.py`), and
    `ma_crossover_day_trade` (`day_trade=True`, `ParallelMode.DAY_TRADE`).
  - A queued-signal baseline per bar covers the 13 classes and the branches in
    the mapping table. Each case runs with no open trade, a long, and a short
    opened at bar 40. Holding-period cases also run a long whose `entry_time` is
    bar 40 plus 30 minutes. Each record stores exit
    `(action, symbol, exit_reason)` and entry `(action, symbol, strength)`, with
    floats normalised by `test_goldens._normalize`.

  Goldens alone pin trades, which can hide a changed signal that the sizer or
  the position cap swallows. The baseline pins the decision itself, including
  strength, which `signals_equal` does not compare. The baseline test's helper
  that produces per-bar signals is the only line that changes when the consumer
  signature changes, and the JSON files are never regenerated after step 4.

- **New golden and baseline cases are recorded with `--regen-goldens` from
  unmodified strategy code. A new case that fails
  `test_backtest_live_parity` or `test_candle_cases_actually_trade` on today's
  code is not added.** Parameters are shortened (for example TSMOM
  `lookback_bars=48, rebalance_bars=12`) so the 400-bar series trades. A
  pre-existing parity failure is a finding to report, not something to absorb
  into this task.

- **Quirks are recorded in the handoff, not fixed:**
  - A genome `exit.rebalance` exit never closes a trade.
  - A genome `middle_band` long exit overrides its short exit reference.
  - The live holding-period exit needs `opened_at` to equal a bar open.
  - The evaluator labels strategy exits `"exit_rule"`.
  - RSI can raise both triggers on one bar.

  Each fix would change results that the goldens hold still.

- **Costs are measured, not assumed.** Deleting three `.get` calls per bar
  should make the engine faster. Recomputing `signal_arrays` for every
  evaluator bar adds four column pulls and a validation over at most
  `window_bound` rows. Both claims are checked:
  - The evaluator benchmark runs three times before and three times after, and
    the medians of its `indicator_p50_ms` and `evaluate_p50_ms` are reported per
    fixture.
  - Engine wall time is taken for `ma_crossover_baseline`,
    `composite_majority_three`, and `genome_ma_session_gate` as the median of
    five runs, before and after.

  Both are reported whichever direction they move.

## Ordered implementation

1. [x] Work on the branch `Q-024-columnar-strategy-signals` in `q_backend`, created
   from `development` by `./work start`. Confirm that Q-023 is merged: engine
   step 1 is a single call to the backtesting-layer `augment_indicator_frame`,
   `execution/indicator_frame.py` only re-exports it, and no ATR or Donchian
   completion block remains in `engine.py`. Every engine reference below is to
   that post-Q-023 file.
2. [x] Measurement before. Run the evaluator benchmark three times and the engine
   timing command five times per case, as in the command block below. Record
   every reading. Nothing to commit.
3. [x] In `test_goldens.py`, add `parallel_mode: ParallelMode = SEQUENTIAL`,
   `day_trade: bool = False`, and an optional data override to `CandleCase`, and
   pass them through `run_candle_case`. Add the six cases named in the decisions.
   Run `uv run pytest tests/backtesting/test_goldens.py --regen-goldens`, then
   confirm with `git status` that only six new files appeared and that the 13
   existing files are byte-identical. Run the suite without the flag. Confirm
   that `test_candle_cases_actually_trade` and `test_backtest_live_parity` pass
   for the new cases, and drop any case that fails on today's code. Commit.
4. [x] Write `test_signal_baseline.py`.
   - Build each baseline case's strategy and frame through the
     backtesting-layer `augment_indicator_frame`, the same call the engine and
     the evaluator make after Q-023.
   - Assert that every legacy trigger column the mapping table reads is `bool`
     dtype.
   - Drive today's `evaluate_queued_signals(strategy, row, open_trades)` over
     every bar for each open-trade scenario.
   - Compare against `goldens/signals/<case>.json`, which honours
     `--regen-goldens`.

   Cases:
   - MACrossover, plain and with `trailing_stop_pct=0.03`.
   - MACD, RSIMeanReversion, BollingerReversion, DonchianBreakout, and VMA with
     registry defaults.
   - FMA `{period 20, band_pct 0.5, ema, holding 10}` and TRB `{20, 0.0, 10}`.
   - TSMOM sign in both rebalance modes, and TSMOM trend with cap 2.0.
   - Hurst in both rebalance modes, with `signal_lag_bars=2` in the flip mode.
   - GatevPairs `{formation 100, trading 50, threshold 1.0}`.
   - The eight `REGISTRY_GENOME_FIXTURES` with `REGISTRY_DEFAULT_PARAMS`.
   - `CTX_GENOME`.
   - `MA_CROSSOVER_GENOME` with its exits replaced by `exit.rebalance`, and with
     its exits replaced by `exit.opposite_signal`.
   - `CompositeEntryStrategy` with OR, AND, and majority, as in the goldens.

   Record, confirm that a second run is green, and confirm that tampering one
   strength value fails with a diff. Commit.
5. [x] Write failing tests in `test_signal_columns.py`:
   - `write_signal_columns` with both triggers True on a bar gives
     `q_signal_entry == 1` there. Strength 0.4 on a bar without an entry is
     stored as 0.0. A float trigger Series raises `SignalContractError`.
   - `validate_signal_columns` raises with a message containing the strategy
     name and `q_signal_exit_short` when that column is missing. It also raises
     for `float32` strength, for strength 1.5 on an entry bar, for NaN strength
     on an entry bar, for a True exit when `holding_period_bars=10`, and for a
     missing `bar_index` when a holding period is declared.
   - `evaluate_queued_signals` on arrays with `exit_long[5]=True`:
     - An open BUY on the strategy symbol gives one `CLOSE` with
       `exit_reason is None`.
     - An open SELL gives nothing.
     - A BUY on another symbol gives nothing.
     - With an enabled stop rule that fires, the rule's `CLOSE` comes first and
       no strategy `CLOSE` is added for that symbol.
   - Holding period 2 with entry at bar 3: closes at position 5, not at
     position 4. An entry time 30 minutes off the grid never closes.
   - `entry[7]=-1` with strength 0.25 gives one `SELL` with strength 0.25.

   Confirm they fail. Implement `signal_columns.py`, leaving existing callers
   untouched. Confirm they pass. Commit.
6. [x] Write failing tests in `test_signal_contract.py`, parametrised over the
   baseline cases:
   - The four columns exist with dtypes `int8`, `bool`, `bool`, `float64`.
   - Each column equals the mapping table's expression over the legacy columns.
   - `holding_period_bars` is 10 for FMA, TRB, and the TRB and FMA genome
     fixtures, with both exit columns all False.
   - A crafted RSI frame whose RSI goes from 25 to 75 in one bar yields
     `q_signal_entry == 1` on that bar.
   - `CompositeEntryStrategy`'s four columns are unchanged over prefixes 205,
     230, and 399 (prefix causality).

   Confirm they fail. Add `holding_period_bars` to `TradingStrategy`, TRB, FMA,
   and `CompositeStrategy`, and call `write_signal_columns` at the end of each
   of the thirteen `compute_indicators`. Keep the row methods for now. Confirm
   that the contract tests, the baseline, the goldens, and
   `test_strategy_causality.py` pass. Commit.
7. [x] Switch the consumers. Move `evaluate_queued_signals` to the new signature in
   `signal_columns.py` and re-export it from `execution/signal_eval.py`.
   - In the engine, build `signal_arrays` once per chunk immediately after the
     single `augment_indicator_frame` call, and in section D call the consumer
     with `i` in both branches.
   - In `StrategyEvaluator.ingest_completed_bars`, build `signal_arrays` from
     each augmented frame, resolve `position`, and pass it through
     `_evaluate_row`.
   - In `reference_queued_signals_by_close`, build the arrays once and iterate
     positions.
   - Point `CompositeEntryStrategy`'s fallback at `q_signal_entry`.

   Change only the baseline test's per-bar helper to the new call. Confirm that
   the goldens, the baseline, `test_backtest_live_parity`, `tests/execution`,
   `test_lai_lau_strategies.py`, `test_tsmom_strategy.py`, and
   `test_hurst_trend_blend.py` pass unchanged. Commit.
8. [x] Write a failing test in `test_strategy.py`: a subclass that implements only
   `compute_indicators` and `get_chart_indicators` instantiates, and
   `IncompleteStrategy` still raises `TypeError`. Confirm it fails.
   - Delete `check_entry_conditions`, `check_exit_conditions`, `resolve_symbol`,
     `fixed_holding_period_exits`, `build_timestamp_to_bar`, and
     `_timestamp_to_bar` from source.
   - Rewrite the test doubles in `test_engine.py`, `test_transaction_costs.py`,
     and `test_exit_rules.py` to write the four columns with `symbol = "DUMMY"`
     or `"TEST"`. `SingleEntryStrategy` writes +1 on the first row only.
   - Rewrite the row-method assertions in `test_strategy.py`,
     `test_strategy_library.py`, `test_composite_entry.py`, and
     `test_genome_exit_policy.py` as column assertions with the same expected
     bars and counts.

   Confirm that `grep -rn "check_entry_conditions\|check_exit_conditions\|resolve_symbol" src tests`
   is empty and that the suite passes. Commit.
9. [x] Regression. Confirm that `git diff <step-3 commit> -- tests/backtesting/goldens`
   is empty. Confirm that `git diff development --` over
   `test_strategy_causality.py`, `genome/test_node_causality.py`,
   `test_composite_genome_causality.py`, `test_genome_parity.py`, and
   `tests/execution/test_evaluator.py` is empty. Confirm that the
   `test_goldens.py` diff against `development` is only the step 3 additions.
   Commit any fixes.
10. [x] Measurement after. Repeat step 2 on the same machine with the same commands.
    Confirm that the benchmark medians stay under 250 ms (indicators) and 50 ms
    (evaluate). Nothing to commit.
11. [ ] Human step, matching human-verifiable criterion 1. With the backend on
    `development`, run MACrossover on WIN$N M15, TSMOM on PETR4 D1 (trend rule,
    `rebalance_on_every_bar=true`, inverse-volatility sizing), and TRB on PETR4
    D1 from the Backtests workspace. Record trade counts and headline metrics.
    Switch the backend to the task branch, rerun the same configurations, and
    compare.
12. [x] Run the full validation suite. Commit.

## Validation

- **Unit:** tie-break, strength zeroing, and non-bool rejection in the writer;
  every validator failure mode names the strategy and column; consumer side and
  symbol filtering; exit-rule precedence; holding-period boundary (N-1 no, N
  yes) and off-grid entry; strength passthrough.
- **Integration:** per-strategy column mapping for all 13 classes and every
  branch; engine sequential and day-trade paths, evaluator, and parity reference
  all through one consumer; composite-entry prefix causality.
- **Regression:** 19 golden files byte-identical (13 existing and 6 recorded
  before the migration); per-bar queued-signal baseline for every case and
  open-trade scenario; determinism, backtest-to-live parity, genome parity, and
  three causality suites with unchanged test bodies; holding-period engine tests
  in `test_lai_lau_strategies.py` unchanged.
- **Manual:** step 11, real-lake backtests identical before and after.
- **Measurement:** evaluator benchmark medians over three runs and engine wall
  time medians over five runs for three cases, before (step 2) and after
  (step 10). Individual readings and medians are reported.
- **Pins:** `CONTRACTS_REV` is unchanged, and no `q_contracts` change.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/backtesting/test_goldens.py tests/backtesting/test_signal_baseline.py \
  tests/backtesting/test_signal_columns.py tests/backtesting/test_signal_contract.py -v
uv run pytest tests/backtesting/test_strategy_causality.py tests/backtesting/genome/test_node_causality.py \
  tests/backtesting/test_composite_genome_causality.py tests/backtesting/test_genome_parity.py \
  tests/backtesting/test_lai_lau_strategies.py tests/execution -q
grep -rn "check_entry_conditions\|check_exit_conditions\|resolve_symbol" src tests
git diff --stat "$(git log --format=%H -1 -- tests/backtesting/goldens/trb_fixed_holding.json)" -- tests/backtesting/goldens

# measurement (steps 2 and 10)
for run in 1 2 3; do uv run pytest tests/execution/test_evaluator_benchmark.py -s -q | grep BENCHMARK; done
for case in ma_crossover_baseline composite_majority_three genome_ma_session_gate; do
  uv run python -c "import statistics, time, sys; sys.path.insert(0, 'tests'); \
from backtesting.test_goldens import CANDLE_CASES, run_candle_case; c = CANDLE_CASES['$case']; \
t = []; [t.append((time.perf_counter(), run_candle_case(c), time.perf_counter())) for _ in range(5)]; \
print('$case', [round(b - a, 3) for a, _, b in t], 'median', round(statistics.median(b - a for a, _, b in t), 3))"
done

# human, step 11
pnpm tauri:dev   # in q_frontend; Backtests workspace, same configuration on both backend branches
```

## Handoff

Report the six golden cases added and each one's trade count. Report the
baseline case count and scenario count, and confirm that the baseline JSON was
not regenerated after step 4. Report the final `git diff` result for the goldens
directory and for the unchanged test files named in step 9. List every test
file changed in step 8 and state, for each, that the expected bars and counts
were carried over. Report the evaluator benchmark `indicator_p50_ms` and
`evaluate_p50_ms` per fixture for every run before and after, with medians, and
the engine wall-time readings and medians for the three cases. Report every new
case dropped in step 3 because today's code failed parity or produced no
trades. Restate the quirks recorded but not fixed: genome rebalance exit,
middle-band override, live holding-period timestamp match, `"exit_rule"` label,
and RSI double trigger. From the human step, report the three strategies'
trade counts and headline metrics on both branches.
