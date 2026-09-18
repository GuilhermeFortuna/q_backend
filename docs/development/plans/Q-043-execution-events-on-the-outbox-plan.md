# Q-043 implementation plan: Execution events on the outbox

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-043-execution-events-on-the-outbox-spec.md`](../specs/Q-043-execution-events-on-the-outbox-spec.md)  
**Depends on:** Q-039

## Current-system context

`streaming/outbox.py::record_event(session, topic, payload, *, payload_schema,
producer_id, routing_key=None, origin_ts=None)` validates topic, routing key and
payload (through `PAYLOAD_MODELS`, today only the two job schemas), increments
`stream_outbox_topic_state.last_seq` for the topic under a row lock, and adds an
`OutboxEvent` to the caller's session. It calls `session.rollback()` on any
validation failure. Its only production caller is `streaming/jobs.py`
(`producer_id=f"job-{kind}-{job_id}"`).

`streaming/snapshot.py::read_job_snapshot` is the pattern for a race-free
snapshot. It opens a connection with `REPEATABLE READ` (`SERIALIZABLE` on
SQLite), reads `read_watermark(session, topics)` and the state in one
transaction, and serializes with `job_snapshot_to_response`.
`api/routers/stream_replay.py` serves `GET /api/v1/stream/jobs/snapshot`, and
generic history serves any durable topic at
`GET /api/v1/stream/{topic}/history`. Tests are in `tests/streaming/`
(`test_job_snapshot_race.py` is the race-test pattern).

Every execution state change goes through
`storage/db/execution_repositories.py`: `create_paper_account`,
`update_paper_cash_balance`, `create_execution_deployment`,
`transition_deployment_lifecycle`, `set_pending_deployment_action`,
`clear_pending_deployment_action`, `update_deployment_last_bar_close`,
`create_execution_decision`, `update_execution_decision_outcome`,
`create_execution_order_intent`, `transition_execution_order`,
`mark_incomplete_orders_unknown`, `record_reconciliation_attempt`,
`finalize_order_reconciliation`, `create_execution_fill`,
`upsert_open_net_position`, `append_ledger_entry`, `record_risk_event`,
`set_kill_switch`. `ExecutionLedger.apply_fill` (`execution/ledger.py`) composes
fill, position, ledger and balance updates. The worker commits through
`ExecutionService._commit`, and the API services in `api/services/execution.py`
commit per request. Lease and audit-event functions change no state that
Q-039 publishes.

After Q-039 (pinned by this task) the vendored contracts carry the six
execution payload models, `ExecutionSnapshot`, and the topic policy pointers.

## Interfaces produced

```python
# src/q_backend/streaming/execution_events.py   (new)
def deployment_state(row: ExecutionDeployment) -> dict: ...          # Q-039 shapes, one per entity
def decision_state(row: ExecutionDecision) -> dict: ...
def order_state(row: ExecutionOrder) -> dict: ...
def fill_event(row: ExecutionFill, position: ExecutionNetPosition | None) -> dict: ...
def ledger_event(row: ExecutionLedgerEntry, account: PaperAccount) -> dict: ...
def risk_rejection_event(row: ExecutionRiskEvent) -> dict: ...
def kill_switch_event(row: ExecutionControlState, *, actor: str | None) -> dict: ...
def account_state(row: PaperAccount) -> dict: ...
def position_state(row: ExecutionNetPosition) -> dict: ...
def emit(session: Session, topic: str, payload: dict, *, producer_id: str) -> OutboxEvent: ...
    # record_event with the contract schema path and the routing key derived from the payload

# src/q_backend/storage/db/execution_repositories.py   (changed)
# every state-changing function above calls execution_events.emit after its flush,
# taking producer_id from a new keyword `producer: str` (default "api"); the worker passes its worker id

# src/q_backend/streaming/snapshot.py   (changed)
EXECUTION_TOPICS = ("decisions", "orders", "fills", "risk", "ledger", "deployments")
def read_execution_snapshot(session_factory, *, limits: SnapshotLimits) -> dict: ...

# src/q_backend/streaming/outbox.py   (changed)
PAYLOAD_MODELS += six execution payload models

# src/q_backend/api/routers/stream_replay.py   (changed)
GET /api/v1/stream/execution/snapshot -> ExecutionSnapshotResponse
```

```
tests/streaming/test_execution_events.py          new: per-writer-path emission, rollback, validation (criteria 1–3)
tests/streaming/test_execution_snapshot.py        new: shape, watermark, 503 (criteria 4, 6)
tests/streaming/test_execution_snapshot_race.py   new: §4.2 convergence over seeded interleavings (criterion 5)
CONTRACTS_REV → Q-039 commit (or later batch-07 pin)
q_contracts: schema/api/openapi.yaml, schema/api/FINDINGS.md Finding 4, COMPAT.md   on branch Q-043-execution-events-on-the-outbox
```

## Implementation decisions

- **Emit in the repository layer.** It is the one layer that every writer
  already goes through, worker and API alike. Emitting there makes "every
  change produces its event" true by construction. A service-layer hook would
  have to be repeated in the worker, the reconciler, recovery and five API
  services, and a missed call site would silently desynchronize every client.

- **Emit after `session.flush()`, inside the same transaction.** The payload is
  built from the flushed row, so server defaults and timestamps are the
  committed values. `record_event` locks the topic counter row last, as its
  docstring requires. Emission is therefore the final statement of each
  repository function.

- **Composite changes emit per entity, in dependency order.** `apply_fill`
  produces an `orders` event (status filled), a `fills` event carrying
  `position_after`, and a `ledger` event per entry, each carrying
  `account_after`. Each is a full entity state, so their relative order across
  topics does not matter to a replacing consumer.

- **`record_event`'s internal rollback is kept.** An invalid execution
  payload is a programming error, and rolling back the whole business change
  is the fail-closed choice: better no fill recorded than a fill with no event.
  The worker already maps a failure after `apply_fill` to an unknown order,
  which the reconciler resolves by lookup.

- **`mark_incomplete_orders_unknown` emits one `orders` event per order it
  changes.** It is a bulk update today. It becomes select-then-update, so that
  each order's post-state can be emitted. The recovery tests pin its effect.

- **`producer_id`** is the worker id (`settings.execution_worker_id`) on worker
  paths, and `api` on API paths. Repository functions take a `producer`
  keyword that defaults to `api`, and worker call sites pass their id.

- **The snapshot reuses `read_job_snapshot`'s isolation recipe,** and reads
  `read_watermark(session, EXECUTION_TOPICS)` first, then the state. Under
  `REPEATABLE READ` both see the same snapshot, so the order does not change
  correctness. Reading the watermark first matches the jobs code.

- **The route lives under `/api/v1/stream/`,** beside the jobs snapshot, because
  it is the snapshot half of the stream protocol, not an execution command
  surface.

- **The convergence test is the acceptance test.** It uses the
  `test_job_snapshot_race.py` harness: a writer thread performing a seeded
  random sequence of repository calls, and a client that subscribes, snapshots,
  buffers, discards at or below the watermark, and replaces entities. Both use
  the real outbox and relay against the test Redis. It asserts equality with a
  final snapshot on every seed.

## Ordered implementation

- [x] 1. Work on the branch `Q-043-execution-events-on-the-outbox` in `q_backend`,
   created from `development` by `./work start`. Confirm Q-039 is merged, set
   `CONTRACTS_REV`, run `make contracts` and `make contracts-check`. Commit.
- [x] 2. Register the six payload models in `PAYLOAD_MODELS`. Write
   `execution_events.py`'s shape functions with a unit test per entity that
   validates against the vendored schema, using rows built by the existing
   execution test factories. Commit.
- [x] 3. Write failing tests in `test_execution_events.py`: one per repository
   function (event topic, count, sequence, payload equals the post-commit
   row), `apply_fill` composite, rollback leaves nothing, invalid payload
   fails the transaction. Confirm they fail. Commit.
- [x] 4. Add `emit` calls and the `producer` keyword to each repository function,
   convert `mark_incomplete_orders_unknown` to per-order updates, and pass the
   worker id from worker, service, recovery and reconciler call sites. Confirm
   step 3's tests and all of `tests/execution` and `tests/api` pass unchanged.
   Commit.
- [x] 5. Write failing tests for the snapshot: contract validation; watermark
   equals the per-topic maximum; a concurrent commit during the read does not
   enter the snapshot or the watermark; `503` with Postgres down. Implement
   `read_execution_snapshot` and the route. Confirm they pass. Commit.
- [x] 6. Write `test_execution_snapshot_race.py` (200 seeds in CI, 2 000 under an
   environment flag) and fix anything it finds. Commit.
- [x] 7. Update `README.md` (execution topics are live on the stream; the snapshot
   route). Commit.
- [x] 8. Recapture the OpenAPI in `q_contracts` on the branch
   `Q-043-execution-events-on-the-outbox`. Close FINDINGS Finding 4 there and
   update `COMPAT.md`. If another batch-07 recapture merged first, rebase and
   capture again. Commit in `q_contracts`.
- [ ] 9. Run `scripts/ci.sh`. Fix, re-run, commit.
- [ ] 10. **Human:** human-verifiable criterion 1.

## Validation

- **Unit:** payload shape per entity against the vendored schemas.
- **Integration:** emission per writer path; composite fill; rollback; snapshot
  watermark under a concurrent commit; `503`.
- **Property:** §4.2 convergence over seeded interleavings.
- **Regression:** `tests/execution`, `tests/api`, `tests/streaming` unchanged;
  `make contracts-check`.
- **Manual:** live paper deployment observed over the stream and compared with
  the paged routes.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/streaming/test_execution_events.py tests/streaming/test_execution_snapshot.py \
  tests/streaming/test_execution_snapshot_race.py tests/execution tests/api -q
Q_RACE_SEEDS=2000 uv run pytest tests/streaming/test_execution_snapshot_race.py -q
```

## Handoff

List each repository function with the events it emits. Report the convergence
test's seed count and runtime, and anything it found. Give the snapshot's size
for the human run. Report the recaptured OpenAPI commit and the `COMPAT.md`
row. From the human step, give one bar's decision event and one trade's
order, fill and ledger events, with their sequence numbers.
