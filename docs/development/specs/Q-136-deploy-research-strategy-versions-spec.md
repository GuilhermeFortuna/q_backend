# Q-136: Deploy research strategy versions

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 38 — Research strategies on MT5
**Depends on:** Q-135
**Repositories:** q_backend (owner), q_contracts, q_terminal
**Implementation plan:** [Plan](../plans/Q-136-deploy-research-strategy-versions-plan.md)

## Purpose

Q-134 freezes every stack run of a strategy file into an immutable version. This task deploys such a run from
`q_terminal` to the MT5 demo account. The forward evaluator runs the version's own hooks on each completed bar,
and the deployment trades with market orders inside the run's day-trade session. M5 becomes a supported forward
timeframe, so the IND and WIN session strategies can run as they were tested.

## Current system

- `identity_from_saved_backtest` (`src/q_backend/execution/validation.py`) builds a deployment identity from a
  saved run's config. `build_strategy_from_compiled` (`execution/strategy_build.py`) hydrates registry
  strategies only, and rejects everything else.
- `StrategyEvaluator` (`execution/evaluator.py`) keeps a rolling window. For each completed bar it computes
  `signal_arrays` and calls `candle_kernel.evaluate_bar`. It never calls `ResearchStrategy` hooks: those run
  only through the kernel's `runtime_callback` in backtests (`research/adapter.py`).
- `validate_timeframe` rejects timeframes below M15. Nothing in `execution/` enforces a day-trade session.
- `research/strategy_files.py` imports every bundle as package `q_strategies`, and evicts the previous bundle from
  `sys.modules` on each load.
- A strategy-file run stores `strategy_version_id`, its parameters, and the run's symbol, timeframe, quantity,
  costs, exit parameters and day-trade settings in its config (Q-134). It does not record which order kinds the
  strategy used.
- `q_terminal`'s `DeployDialog.qml` offers a catalog source and a saved-run source, and creates `paper`
  deployments only ("mt5_live locked").

## Behaviour

### Bundles side by side

- Each bundle is imported under its own package name, `q_strategies_<first 12 hash characters>`. Loading one
  bundle no longer evicts another, so several versions live in one process. Strategy files use relative
  imports, so they are unaffected. `load_strategy()` keeps working as before.

### Order kinds of a run

- A strategy-file stack run records the order kinds its hooks returned during the run, as
  `result_summary.order_kinds`, a sorted list drawn from:
  - `market_entry` and `market_close`;
  - `protective_levels` (an entry with `stop_loss` or `take_profit`);
  - `amend`, `resting_stop`, `resting_limit` and `cancel`;
  - `priced_entry` and `priced_close`.

  A version whose file uses tick hooks adds `tick_hooks`. The run detail payload exposes the list.
- Runs from before this task have no list. To deploy them, re-run them first.

### Creating a deployment from a strategy-file run

- `POST /api/v1/execution/deployments` with `source_backtest_run_id` accepts a strategy-file run. The deployment
  takes from the run:
  - its version, symbol and timeframe;
  - its parameters and exit parameters;
  - its quantity, as fixed-quantity sizing;
  - its day-trade settings.

  The source is recorded as `strategy_version`, with the version id and bundle hash as provenance. The
  compiled config and its hash are computed by the backend, as for other sources.
- Creation is refused with a clear `422` when:
  - the run has no `order_kinds`;
  - it used a kind outside the **deployable set**, which in this task is `market_entry` and `market_close`
    (Q-137 extends it). `priced_entry`, `priced_close` and `tick_hooks` are never deployable;
  - its timeframe is below M5;
  - day trading is off. Overnight positions are out of this task's scope.
- `PATCH .../configuration` validates new parameters against the version's parameter specs. The version, symbol,
  timeframe and day-trade session cannot change.
- With Q-135's capability `available`, the terminal defaults the broker mode to `mt5_live` with live activation
  on. `paper` remains selectable.

### Forward evaluation

- A strategy-version deployment is evaluated by a research evaluator with the same contract as
  `StrategyEvaluator`: `seed_window`, `ingest_completed_bars`, `replay_recovery` and `set_open_trade`. For each
  completed bar, it:
  1. computes `compute_indicators` on the rolling window through the research adapter;
  2. calls `exit_strategy`, then `entry_strategy`, with the frame through that bar and the deployment's open
     position as a `ResearchPosition`;
  3. maps the returned orders to the existing decision actions: buy, sell, close or hold.
- Warm-up and window size: the rolling window holds at least the bars of the run's longest session plus
  `Q_EXECUTION_RESEARCH_WARMUP_BARS` (default 2,000). A strategy that needs more history declares it with a
  class attribute `warmup_bars`, which the version records.
- A returned order outside the deployable set records a risk event naming the kind and pauses the deployment.
  Nothing is submitted.
- Hooks that raise record a risk event with the exception and pause the deployment.

### Day-trade session

- A deployment with day trading refuses entries whose decision bar closes after `day_trade_end_time` or before
  `day_trade_start_time`. It submits a market close on the first completed bar whose close time is at or after
  `day_trade_close_time`. This is the backtest's rule; the operator picks a close time inside the market's
  trading hours.
- At start or resume, a position held past the close time of its session is flattened at once.

### Timeframes

- M5 and M10 become supported forward timeframes, and M1 stays refused. The bar coordinator and the worker loop
  are unchanged.

### Terminal (`q_terminal`)

- The deploy dialog's saved-run source lists strategy-file runs. It shows the strategy id, the short bundle
  hash, the order kinds and the day-trade session, and greys out runs that cannot be deployed, giving the
  reason.
- Parameter fields render `bool` and `time` specs, for configuration edits.
- When `mt5_orders.capability` is `available`, the dialog offers MT5 and selects it, with the account login and a
  `DEMO` or `REAL` badge. Otherwise it explains why MT5 is unavailable.
- The deployment list and detail show the broker mode, with the badge, and the strategy version.

### Parity

- On fixture bars, the decisions of the research evaluator equal the queued decisions of the research backtest
  for the same version and parameters, bar by bar. This includes the day-trade close. The test lives beside the
  existing parity tests.

## Acceptance criteria

1. Two different bundles load side by side in one process, and each strategy keeps its own `_lib` helper.
2. A stack run of a market-only fixture strategy records `["market_close", "market_entry"]`. Runs of fixtures
   that use protective levels, resting orders and priced closes record those kinds.
3. Creating a deployment from the market-only run succeeds with source `strategy_version`, M5 and day trading.
   Runs without `order_kinds`, with a non-deployable kind, below M5, or without day trading are refused with
   `422`.
4. Forward decisions equal the backtest's on fixture bars, including entries refused outside the session and the
   close at `day_trade_close_time`.
5. A hook returning a resting order in a live deployment pauses it with a risk event, and the fake edge receives
   nothing.
6. After a worker restart mid-session, recovery replays without emitting orders, and the next decision equals
   the uninterrupted run's.
7. Against the fake edge and a Q-135 demo account, the deployment submits its entry and its day-trade close.
8. `q_terminal` tests against its fake server:
   - strategy-file runs are listed, and the undeployable ones are greyed out with the reason;
   - `bool` and `time` parameter fields edit and submit correctly;
   - MT5 is selected when available, with the `DEMO` badge, and explained when locked.
9. Existing evaluator, parity, worker and terminal tests pass unchanged. `make contracts-check` passes in
   `q_backend` and `q_terminal`.

## Out of scope

- Protective levels, amendments, resting orders and cancellation (Q-137).
- Positions held overnight, and tick hooks.
- Deploying from a strategy file or version without a stack run.
- Real-money activation.

## Human inspection

The reviewer runs a port of `LateSessionTrendStrategy` from the Backtests workspace on `IND$N` M5 under
`./dev research`. Its session must close inside trading hours (for example 18:15). They deploy that run from
`q_terminal` to the MT5 demo account and let it trade one session. They then compare its entry, exit and PnL
with the research run for that day.
