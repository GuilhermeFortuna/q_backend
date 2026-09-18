# Q-041 implementation plan: Evaluator follows positions while running

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-041-evaluator-follows-positions-while-running-spec.md`](../specs/Q-041-evaluator-follows-positions-while-running-spec.md)  
**Depends on:** Q-031

## Current-system context

`execution/recovery.py::ExecutionRecovery.build_runtime` reads
`get_open_net_position`, converts it with
`position_adapter.execution_position_to_trade`, and constructs the
`StrategyEvaluator` with `open_trade`. It is the only caller that sets the open
trade. `execution_position_to_trade` names every trade `exec-<deployment_id>`.
`StrategyEvaluator.set_open_trade` exists and has no production caller.
`_evaluate_row` passes `[self._open_trade]` to `candle_kernel.evaluate_bar`,
and exit-rule state lives on the `q_core` `DecisionStep` owned by
`strategy.exit_strategy`, keyed by trade id (Q-031).

`execution/worker.py::ExecutionWorker.poll_once` reconciles pending orders
(`OrderReconciler.reconcile_deployment`), processes pending flattens
(`service.flatten_deployment`), and then, for each running deployment, calls
`runtime.evaluator.ingest_completed_bars(new_bars)`. That evaluates every new
bar first, and only afterwards loops the results through
`service.process_completed_bar`. Fills are applied inside the service through
`ExecutionLedger.apply_fill`, which updates the net position through
`upsert_open_net_position`. Reconciliation applies fills through
`reconciliation.apply_filled_resolution`.

`tests/execution/test_worker.py`, `test_service.py` and
`test_evaluator_recovery_state.py` (Q-031) are the neighbouring tests.
`test_evaluator_recovery_state.py` builds evaluators directly with an open long
at bar 40.

## Interfaces produced

```python
# src/q_backend/execution/position_adapter.py   (changed)
def position_trade_id(deployment_id: UUID | str, opened_at: datetime) -> str: ...
    # "exec-<deployment_id>-<opened_at as UTC epoch ms>"
def execution_position_to_trade(...) -> Optional[Trade]: ...   # uses position_trade_id

# src/q_backend/execution/recovery.py   (changed)
def open_trade_for_deployment(session, deployment, *, point_value: float) -> Optional[Trade]: ...
    # the single place that turns the durable net position into the evaluator's open trade

# src/q_backend/execution/evaluator.py   (changed)
class StrategyEvaluator:
    def ingest_completed_bar(self, bar: pd.DataFrame) -> Optional[ForwardEvaluationResult]: ...  # one bar
    # ingest_completed_bars stays for recovery and tests; it loops ingest_completed_bar

# src/q_backend/execution/worker.py   (changed)
class ExecutionWorker:
    def _sync_open_trade(self, session, runtime) -> None: ...   # after reconcile, flatten, and each processed bar
```

```
tests/execution/test_worker_position_sync.py   new: criteria 1–6
docs/development/FINDINGS.md                   item 18 resolved
```

## Implementation decisions

- **Sync from the durable position, not from the fill.** After every step that
  can change a position, the worker re-reads `get_open_net_position` and
  calls `set_open_trade(open_trade_for_deployment(...))`. The ledger is the
  authority. Deriving the trade from the fill would reimplement position
  arithmetic that `ExecutionLedger` already owns, and would drift on partial
  closes and reversals.

- **Bar-at-a-time in the worker.** `poll_once` calls `ingest_completed_bar` per
  bar, then `process_completed_bar`, then `_sync_open_trade`, then moves on to
  the next bar. Recovery keeps using `ingest_completed_bars`, because replay
  has no fills to learn between bars: it reconstructs state for a position that
  already exists.

- **Trade id includes `opened_at`.** The `DecisionStep` keys exit-rule state by
  trade id. With a constant id, a position opened on the bar after another
  closed could inherit its trailing extreme. `opened_at` is durable on
  `ExecutionNetPosition`, so recovery derives the same id. Epoch
  milliseconds keep the id free of timezone formatting.

- **The sync happens in the worker, not the service.** The service has no
  evaluator. It is a per-bar transaction boundary shared with flatten, and
  giving it one would couple persistence to evaluation state. The worker owns
  runtimes and is the single place that calls all three position-changing
  paths.

- **A test that pinned the old behaviour is changed only with a listed
  reason.** Candidates are worker tests that assert no exit fires after an
  in-run entry. Any such test is changed in its own commit, with the reason in
  the message.

## Ordered implementation

- [x] 1. Work on the branch `Q-041-evaluator-follows-positions-while-running` in
   `q_backend`, created from `development` by `./work start`.
- [x] 2. Write `tests/execution/test_worker_position_sync.py` with criteria 1–6,
   using the paper broker, a fake coordinator that yields scripted bars, and
   MACrossover with `trailing_stop_pct`. Confirm that criteria 1–5 fail on
   today's code and 6 passes. Commit.
- [x] 3. Add `position_trade_id` and use it in `execution_position_to_trade`. Update
   `test_evaluator_recovery_state.py` only if it hard-codes the old id, in its
   own commit. Confirm criterion 5's identity half and `tests/execution` pass.
   Commit.
- [x] 4. Add `open_trade_for_deployment` to `recovery.py` and use it in
   `build_runtime`. Commit.
- [x] 5. Add `ingest_completed_bar` and make `ingest_completed_bars` loop it. Confirm
   the evaluator, q_core and recovery-state tests pass unchanged. Commit.
- [x] 6. Restructure `poll_once` to go bar at a time, and add `_sync_open_trade`
   after reconciliation, after flatten, and after each processed bar. Confirm
   criteria 1–4 and 6 pass. Commit.
- [x] 7. Run `tests/execution`, the parity test and goldens. List and justify any
   changed test. Commit.
- [x] 8. Mark FINDINGS item 18 resolved by Q-041, and note that item 3 remains open
   and still affects holding-period exits live. Commit.
- [x] 9. Run `scripts/ci.sh`. Fix, re-run, commit.
- [ ] 10. **Human:** human-verifiable criterion 1.

## Validation

- **Unit:** trade id derivation; open trade from a flat, long and short position.
- **Integration:** worker with paper broker through entry and in-run exit;
  flatten; reconciliation-filled; two bars in one poll; restart mid-position.
- **Regression:** `tests/execution`, `test_backtest_live_parity`, goldens.
- **Manual:** a paper deployment exits on a trailing stop without a restart.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/execution tests/backtesting/test_goldens.py -q
uv run pytest tests/execution/test_worker_position_sync.py -v
```

## Handoff

Report each criterion's test and confirm that criteria 1–5 failed before the
change. List every changed existing test, with its reason. Quote the FINDINGS
update. From the human step, give the entry and exit decision times and the
exit reason.
