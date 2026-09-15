# Q-031 implementation plan: Execution evaluator on q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-031-execution-evaluator-on-q-core-spec.md`](../specs/Q-031-execution-evaluator-on-q-core-spec.md)  
**Depends on:** Q-028

## Current-system context

After Q-028, backtests run on `q_core.engine.run_candle` through
`backtesting/candle_kernel.py`, and `execution/parity.reference_queued_signals_by_close`
drives `candle_kernel.reference_decisions` with one `q_core.engine.DecisionStep`.
The forward evaluator (`execution/evaluator.py`) is unchanged by Q-028:
`ingest_completed_bars` merges each batch into the pandas `_rolling` window,
recomputes `augment_indicator_frame` and `signal_arrays` for each evaluated bar,
resolves `position`, and `_evaluate_row` calls the Python
`signal_columns.evaluate_queued_signals` (which calls
`strategy.exit_strategy.check_exits`), maps the result with
`_domain_action_from_queued`, and sizes the first entry with
`self.sizer.size_signal(signal, close_price, self.initial_capital, current_data=row)`.
`replay_recovery` runs the same ingest with `_replay_mode` set, so exit-rule state
is rebuilt on `strategy.exit_strategy`. `recovery.build_runtime` is the only place
that sets the evaluator's open trade (`execution_position_to_trade`, id
`exec-<deployment>`); `worker.poll_once` never calls `set_open_trade` after a fill.

The Python exit semantics still live in `ExitStrategy` (`check_exits`,
`_prune_stale_state`, `_rule_state`, `_columns_ready`, `_state`,
`_extreme_prices`) and in every `ExitRule` subclass's `is_enabled`,
`required_columns`, `on_bar` and `should_exit`, plus the module helpers
`legacy._bar_prices`/`_is_long` and `parabolic_sar.update_psar_long`/`_short`.
Their callers after Q-028 are the evaluator path, `augment_indicator_frame`
(`exit_strategy.required_columns()`), `execution/warmup.compute_window_bound_bars`
(`enabled_rules` and `rule.required_columns`), the exit-rule listing for the API
(`list_exit_rules`, metadata only), and tests: `test_exit_rules.py` (asserts on
`_state`, `_extreme_prices`, `update_psar_long`/`_short` directly, and
`rule.is_enabled`), `test_exit_strategy.py` (`_extreme_prices`),
`test_genome_exit_policy.py` (`exit_strategy.check_exits`), and
`test_composite_entry.py` (assigns an `ExitStrategy`). The sizers'
`size_signal`/`max_position_size` are called by the evaluator and by
`test_position_sizing.py`, `test_position_sizing_factory.py` and
`test_tsmom_strategy.py`. After Q-027 the wheel also exposes
`q_core.engine.enabled_rules`, `max_position`, and `DecisionStep.state(trade_id)`
in the backend's state-dict shape. Measured evaluator timings are documented in
`README.md` (indicators ~2-3 ms p50, evaluate ~0.5 ms p50, full path under 50 ms
p95). The gap is a second, Python implementation of section D, exit rules and
sizing, kept alive by the evaluator.

## Interfaces produced

```python
# src/q_backend/backtesting/candle_kernel.py   (changed)
def exit_step(exit_strategy: ExitStrategy) -> Any: ...     # the q_core DecisionStep owned by that exit strategy
def evaluate_bar(
    strategy: TradingStrategy,
    frame: pd.DataFrame,                                  # augmented frame
    signals: SignalArrays,
    position: int,
    open_trades: list[Trade],
) -> tuple[list[Signal], list[Signal]]: ...               # (pending_exits, pending_entries), section D order
def reference_decisions(strategy, frame, signals, open_trades) -> list[tuple[list[Signal], list[Signal]]]: ...  # now loops evaluate_bar
def enabled_rule_ids(params: Mapping[str, object]) -> list[str]: ...
def required_exit_columns(params: Mapping[str, object]) -> list[str]: ...
def size_order(sizing: KernelSizing, signal: Signal, price: float, capital: float,
               current_data: pd.Series | None) -> float | None: ...
def max_position(sizing: KernelSizing, price: float, capital: float) -> float | None: ...
```

```python
# src/q_backend/backtesting/exit_strategy.py   (changed; public surface kept)
class ExitStrategy:
    params: dict[str, Any]
    def __init__(self, params: dict[str, Any] | None = None, **kwargs: Any): ...
    # properties stop_loss_pct ... atr_period unchanged
    @property
    def _state(self) -> Mapping[str, dict[str, dict[str, Any]]]: ...   # read-only view from DecisionStep.state for open ids
    @property
    def _extreme_prices(self) -> dict[str, float]: ...                 # unchanged derivation from _state
    def check_exits(self, open_trades: List[Trade], current_data: pd.Series) -> List[Signal]: ...  # q_core, one-row columns
    def required_columns(self) -> list[str]: ...                        # q_core
# removed: _prune_stale_state, _rule_state, _columns_ready, the mutable _state dict

# src/q_backend/backtesting/exit_rules/base.py, legacy.py, breakeven.py, chandelier.py, donchian_stop.py,
#     parabolic_sar.py, profit_target_ratchet.py, time_stop.py   (changed)
class ExitRule(ABC):                                     # metadata: id, exit_group, label, description, enable_param,
    def param_specs(self) -> list[StrategyParamSpec]: ...  #   enable_value, param_names, required_param_names
    def is_enabled(self, params: dict[str, Any]) -> bool: ...   # self.id in candle_kernel.enabled_rule_ids(params)
    def required_columns(self, params: dict[str, Any]) -> list[str]: ...  # q_core columns this rule reads, when enabled
# removed: on_bar, should_exit, _bar_prices, _is_long, update_psar_long, update_psar_short, _clamp_psar_*

# src/q_backend/backtesting/exit_rules/registry.py   (changed)
def enabled_rules(params) -> list[ExitRule]: ...         # ordered by q_core's enabled ids
def required_columns(params) -> list[str]: ...            # candle_kernel.required_exit_columns
# unchanged: EXIT_RULES, EXIT_GROUP_ORDER, list_exit_rules, shared_exit_params, all_param_specs

# src/q_backend/backtesting/position_sizing.py   (changed; classes and configs kept)
# FixedQuantitySizer / FixedSafetyMarginSizer / InverseVolatilitySizer.size_signal and max_position_size delegate to candle_kernel

# src/q_backend/backtesting/signal_columns.py   (changed)
# removed: evaluate_queued_signals; kept: column names, write_signal_columns, validate_signal_columns, SignalArrays, signal_arrays

# src/q_backend/execution/signal_eval.py   (deleted)
# src/q_backend/execution/evaluator.py   (changed: _evaluate_row calls candle_kernel.evaluate_bar; public surface unchanged)
```

```
tests/execution/test_evaluator_recovery_state.py      new: restart-and-replay equals uninterrupted evaluation
tests/execution/test_evaluator_q_core.py              new: evaluator decisions come from q_core; tampering fails parity
tests/backtesting/test_exit_rules.py                  psar helper tests drive ExitStrategy instead; expected values unchanged
tests/backtesting/test_indicator_kernels.py           hygiene: no per-bar exit or consumer definitions outside q_core
docs/development/baselines/Q-031/phase-2-acceptance.md  new: Phase 2 acceptance record
docs/development/FINDINGS.md                          item 18 gains triage; new item: evaluator window swap deferred
README.md                                             measured phase timings refreshed
```

## Implementation decisions

- **The `DecisionStep` lives on the `ExitStrategy` instance.** Exit-rule state
  lives on `strategy.exit_strategy` today, and the genome strategy's
  `_refresh_exit_strategy` resets state by replacing that object. Owning the step
  there keeps both behaviours: state persists across the evaluator's bars and
  across `replay_recovery`, and a refreshed exit strategy starts fresh. The
  holding period is passed per `decide` call (Q-027), so one step serves the
  evaluator and direct `check_exits` callers.

- **`evaluate_bar` is the only per-bar decision function, and both the evaluator
  and the parity reference call it.** After Q-028 the reference already used a
  step; routing both through one bridge function makes it impossible for the two
  sides of the parity test to prepare inputs differently, so the test measures what
  it claims: full-frame against windowed, bar-by-bar indicator computation.

- **`evaluate_bar` resolves the open trade's entry bar with
  `frame.index.get_indexer([entry_time])` on the current window.** That is Q-024's
  rule. A trade whose entry has scrolled out of the window gets `None`, as
  `Series.get` returned `None` before, so the holding period does not fire, which
  is today's behaviour.

- **`ExitStrategy.check_exits` stays, answered by `q_core` over one-row columns.**
  `test_genome_exit_policy.py` and research code call it with a row, and keeping
  it lets those tests run unedited. The row becomes length-1 arrays: `close` from
  `row.get("close", 0.0)`, `high`/`low` only if present, each required column
  only if present (NaN kept), decision columns all neutral. This is not a second
  implementation: every decision is `q_core`'s, and the function only adapts a row.

- **`_state` becomes a read-only view built from `DecisionStep.state` for the ids
  of the last `check_exits`/`evaluate_bar` call.** Tests assert
  `trade.id in exit_strat._state` after pruning and read nested keys like
  `["psar"]["sar"]`. Q-027 returns the Python key names and omits keys the Python
  rule would not have set, so those assertions keep their meaning, and
  `_extreme_prices` keeps its derivation.

- **The four `update_psar_long`/`_short` tests are the only test bodies that
  change.** They call a helper that is deleted. Each is rewritten to feed the same
  highs, lows and entry to `ExitStrategy(psar_af_start=0.02, ...)` through
  `check_exits` and compare `_state[id]["psar"]` with the same expected numbers.
  Keeping a Python PSAR helper for tests would be exactly the duplicate this task
  removes.

- **Rule metadata stays Python; enablement and columns come from `q_core`.**
  Labels, groups, specs, presets and `list_exit_rules` feed the UI and the
  optimizer and are not semantics. `is_enabled`, `enabled_rules` and
  `required_columns` decide what runs and what is computed, so they delegate. Each
  rule's own `required_columns(params)` filters `q_core`'s list to its column
  prefix, which `compute_window_bound_bars` iterates.

- **Sizers keep their classes and delegate.** Sizers are constructed from configs
  in many places and their `max_position_size`/`size_signal` are part of tests. The
  bridge's `size_order` reads `current_data.get("volatility")` with the
  `_read_volatility` coercion and calls `size_entry`; `max_position_size` calls
  `max_position`. The `Order` object is still built in Python.

- **The evaluator's rolling window stays pandas.** The spec's non-goal explains
  why; the plan records the follow-up in FINDINGS with the condition that would
  justify it (a consumer whose frames carry only contracted columns, or a
  benchmark that shows the window as the bottleneck).

- **FINDINGS item 18 (open trade never updated) is triaged, not fixed.** The fix
  (calling `set_open_trade` from the service after fills) changes live trading
  behaviour, and it needs its own spec, tests against the paper broker, and
  review. The triage note proposes that task and blocks live activation on it.

- **The acceptance record follows the spec-driven template and is written before
  it is filled.** Its manual section has one entry per human-verifiable criterion
  of Q-026 to Q-031 in task order, each with the command from that task's spec
  and the figure to compare against, so the human can fill it from the tasks'
  handoff reports without reconstructing anything.

- **Costs are measured against the documented figures.** The benchmark and the
  worker's benchmark run three times before and after; medians are reported per
  fixture, and README's table is refreshed with the after figures.

## Ordered implementation

1. Work on the branch `Q-031-execution-evaluator-on-q-core` in `q_backend`,
   created from `development` by `./work start`. Confirm Q-028 is merged and the
   pinned `q_core` exposes `enabled_rules`, `max_position` and
   `DecisionStep.state`. If not, set the task blocked and stop.
2. Measurement before. Run the evaluator benchmark and the worker benchmark three
   times each with the commands below and record every reading. Nothing to commit.
3. Write `tests/execution/test_evaluator_recovery_state.py` against today's code:
   for MACrossover with `trailing_stop_pct=0.03`, with `psar_af_start=0.02`, and
   with `max_bars_in_trade=15`, an evaluator with an open long at bar 40 ingests
   bars 0-199 one at a time; a second evaluator seeds bars 0-120, replays to bar
   159's close with `replay_recovery`, then ingests bars 160-199. Assert the
   decisions for bars 160-199 are equal, including exit reasons, and that skipping
   the replay makes at least one decision differ. Confirm it passes on today's code.
   Commit.
4. Write failing tests in `tests/execution/test_evaluator_q_core.py`: patching
   `candle_kernel.evaluate_bar` to raise makes `ingest_completed_bars` raise; with
   `evaluate_bar` wrapped to rewrite one exit reason to `"fixed_sl"` for
   `ma_crossover_trailing`, the parity comparison fails. Confirm they fail. Add
   `exit_step` and `evaluate_bar` to the bridge, switch `_evaluate_row`, and switch
   `reference_decisions` to loop `evaluate_bar`. Confirm the new tests,
   `test_backtest_live_parity`, the recovery-state test and `tests/execution` pass.
   Commit.
5. Delegate sizing. Add `size_order` and `max_position` to the bridge and make the
   three sizers call them. Confirm `test_position_sizing.py`,
   `test_position_sizing_factory.py`, `test_tsmom_strategy.py` and
   `tests/execution` pass unchanged. Commit.
6. Delegate exit enablement and columns: `is_enabled`, `enabled_rules`,
   `required_columns` and per-rule `required_columns`. Add a test that
   `compute_window_bound_bars` for every `EXIT_PRESETS` entry combined with default
   MACrossover params equals the value recorded from `development` in the test.
   Confirm `test_warmup.py`, `test_exit_rules.py` registry tests and
   `augment_indicator_frame` tests pass. Commit.
7. Rewrite `ExitStrategy` on the step: `check_exits` over one-row columns,
   read-only `_state`, unchanged `_extreme_prices`. Rewrite the four PSAR helper
   tests as described, with their expected numbers unchanged. Confirm
   `test_exit_rules.py`, `test_exit_strategy.py`, `test_genome_exit_policy.py` and
   `test_composite_entry.py` pass. Commit.
8. Delete the Python per-bar semantics: `ExitRule.on_bar`/`should_exit` and every
   override, `_bar_prices`, `_is_long`, the PSAR helpers,
   `signal_columns.evaluate_queued_signals`, and `execution/signal_eval.py`.
   Extend the hygiene test to fail if `def on_bar`, `def should_exit` or
   `def evaluate_queued_signals` appears under `src/`. Confirm the full backtesting
   and execution suites pass. Commit.
9. Regression. Confirm `git diff development -- tests/backtesting/goldens` is empty,
   the registry and queued-signal baselines pass, and `git diff development --`
   over `tests/execution` (except the two new files), `test_goldens.py`,
   `test_exit_strategy.py`, `test_genome_exit_policy.py`, `test_position_sizing.py`,
   `test_position_sizing_factory.py` and `test_warmup.py` is empty. Commit any
   fixes.
10. Write the triage beside FINDINGS item 18, and add the evaluator window-swap
    deferral as a new item with the condition that would justify it. Commit.
11. Measurement after. Repeat step 2. Confirm the worker full-path p95 is under
    50 ms, and update README's measured timings table. Commit.
12. Write `docs/development/baselines/Q-031/phase-2-acceptance.md` from the
    acceptance template: environment; automated validation groups for exit rules,
    candle kernel, decision-step parity, tick kernel and bars, candle and tick
    backtests on `q_core`, and the evaluator, each with its reproducing command;
    derived figures from Q-027, Q-028, Q-029, Q-030 and this task's automated
    measurements with their methods; one pending manual entry per human-verifiable
    criterion of Q-026 to Q-031 with command and comparison figure; and deferred
    follow-ups (evaluator window swap, evaluator open-trade update, candle and tick
    day-boundary mismatch, `numba` in candle strategies, duplicated momentum math),
    each with the measurement or event that would justify it. Record the commit it
    is written against. Commit.
13. Human step, matching human-verifiable criterion 1: the paper deployment with
    a restart while the position is open.
14. Human step, matching human-verifiable criterion 2: fill the acceptance record's
    manual entries and mark Phase 2 accepted.
15. Run the full validation suite. Commit.

## Validation

- **Unit:** bridge sizing and maximum position against today's sizer outputs;
  enablement and required columns delegation; one-row `check_exits` adaptation;
  read-only state view.
- **Integration:** evaluator decisions from `q_core` for every golden case with and
  without an open trade; restart-and-replay equality for trailing, PSAR and time
  stop; window bounds for every exit preset.
- **Regression:** goldens, registry and queued-signal baselines unchanged;
  execution, sizing, exit-strategy, genome exit-policy and warm-up tests unchanged;
  PSAR tests' expected numbers unchanged.
- **Manual:** paper deployment through an exit after a restart; Phase 2 acceptance
  record completed.
- **Measurement:** evaluator benchmark and worker full-path benchmark, three runs
  before and after, medians per fixture.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/execution tests/backtesting/test_exit_rules.py tests/backtesting/test_exit_strategy.py \
  tests/backtesting/test_genome_exit_policy.py tests/backtesting/test_position_sizing.py \
  tests/backtesting/test_position_sizing_factory.py tests/backtesting/test_goldens.py \
  tests/backtesting/test_engine_registry_baseline.py tests/backtesting/test_signal_baseline.py \
  tests/backtesting/test_indicator_kernels.py -q
grep -rn "def on_bar\|def should_exit\|def evaluate_queued_signals" src || echo "no python per-bar semantics"

# measurement (steps 2 and 11)
for run in 1 2 3; do
  uv run pytest tests/execution/test_evaluator_benchmark.py tests/execution/test_worker.py -k benchmark -s -q | grep -i benchmark
done

# human, step 13
uv run q-execution --log-level INFO run 2>&1 | tee worker.log
curl -s http://127.0.0.1:8000/api/v1/execution/deployments/<id>/decisions | jq
```

## Handoff

Report the evaluator benchmark's indicator and evaluate medians per fixture and
the worker full-path p95 for every run before and after, and the refreshed README
table. Report the recovery-state test's three cases and confirm the no-replay
control fails. List every test file whose body changed (only `test_exit_rules.py`
is expected) and confirm expected values were carried over. Confirm the grep for
per-bar Python semantics is empty and that `execution/signal_eval.py` is gone.
Report the window bound for each exit preset. Quote FINDINGS item 18's triage and
the new window-deferral item. Give the acceptance record's path and commit, and list its pending
manual entries. From the human steps, report the deployment's entry and exit
decisions with reasons and the restart time, and the Phase 2 acceptance status.
