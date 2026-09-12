# Q-010: Transactional outbox

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.1–§4.3](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#4-data-events-and-streaming)  
**Depends on:** Q-009  
**Implementation plan:** [`../plans/Q-010-transactional-outbox-plan.md`](../plans/Q-010-transactional-outbox-plan.md)

## Purpose

Every state change a client must never miss — a job reaching a terminal state
today, an order or a fill later — is currently written to Postgres and then
announced, if at all, by a separate best-effort Redis write that can fail after
the commit. A client told "completed" by a poll and nothing by a push cannot
tell a lost event from an event that never happened. This task gives the backend
a transactional outbox. A durable event is committed in the same transaction as
the state change it describes, with a per-topic sequence number that has no gaps
and a stable epoch. The snapshot-then-delta protocol the terminal and the
research UI will rely on is only correct if these three properties hold.

## Requirements

### Atomicity

- A durable event is recorded in the same database transaction as the state
  change it describes. If the transaction rolls back, the event does not exist;
  if it commits, the event exists.
- Recording an event requires no network call other than the database, so
  recording cannot fail for a reason the state change itself would not also
  fail for.

### Sequence and epoch

- Within a topic, events are numbered consecutively in commit order, starting
  after the last number ever issued in the current epoch.
- A rolled-back transaction does not consume a number. Clients treat a gap as a
  lost event and fetch it, so a gap that is not a real event would send them
  looking for something that does not exist.
- Two transactions recording events on the same topic at the same time receive
  distinct numbers, and the order of the numbers matches the order in which the
  events become visible.
- Every event carries the outbox epoch. The epoch changes only through a
  deliberate, logged operator action, never as a side effect of a restart,
  migration, or deploy.

### Topic discipline

- Only topics the vendored topic policy declares durable can be recorded. An
  attempt to record an ephemeral topic, or an undeclared one, fails loudly and
  rolls back with the caller's transaction.
- Every recorded event is a valid stream envelope under the pinned contracts,
  including the routing values its topic's coalesce policy requires. This is
  checked when the event is recorded, not when it is relayed.

### Watermark

- Inside a repeatable-read transaction, the backend can read the highest
  sequence number visible to that transaction for any durable topic, and the
  value is exactly the last event reflected in the state that transaction reads.

### Retention

- Events older than thirty days can be pruned. Pruning never removes an event
  that has not yet been relayed, and never renumbers what remains.
- After pruning, the oldest retained sequence number of each topic can be read,
  so a history request below it can be answered as expired rather than as empty.

### Preserved behavior

- No existing table, repository function, API response, or job behavior changes.
  This task adds a capability that nothing calls yet.
- The vendored contracts become importable by backend code and are pinned to the
  commit Q-009 produced, with the drift check passing.

## Constraints and non-goals

- **No relay and no Redis.** Moving events to Redis Streams is Q-011. An outbox
  that already publishes cannot be tested for atomicity on its own, and
  atomicity is what this task has to prove.
- **No producers.** No job manager, execution component, or router records an
  event here. The first producer is Q-012, so that "the outbox is correct" and
  "the job managers call it correctly" are not proven in the same change.
- **No execution topics wired in.** Recording orders and fills inside the
  fail-closed ledger transactions changes safety-critical code and belongs with
  the execution work in phase 4.
- **No history endpoint.** Reading ranges over REST is Q-015.
- **No scheduled pruning.** The prune operation exists and is tested. Running it
  on a schedule belongs to the relay process in Q-011.
- **No epoch-reset tooling beyond the one operation.** No UI and no API. An
  operator action is a command line invocation.

## Acceptance criteria

### Agent-verifiable

1. An event recorded in a transaction that is then rolled back is absent, and the
   next committed event on that topic takes the number the rolled-back one would
   have taken.
2. Two concurrent transactions recording on the same topic against a real
   Postgres receive consecutive, distinct numbers, and no number is skipped
   across one hundred concurrent recordings.
3. Recording an ephemeral topic or an undeclared topic raises, and the caller's
   transaction rolls back.
4. Recording an event whose envelope is invalid under the pinned contracts, or
   that lacks required routing values, raises.
5. A repeatable-read transaction that reads the watermark, while a concurrent
   transaction commits a new event, still sees the earlier watermark.
6. Pruning removes relayed events older than thirty days, keeps unrelayed ones
   of any age, and reports the oldest retained number per topic.
7. The epoch is unchanged across a migration run and an application restart, and
   changes only through the operator command, which logs the old and new values.
8. The vendored contracts are pinned to Q-009's commit and the drift check passes.
9. The migration upgrades and downgrades cleanly against an empty database and
   against a database at the previous head.
10. The full validation suite passes.

### Human-verifiable

1. Recording throughput under contention is measured against the real
   development Postgres: eight writers on one topic for sixty seconds, reporting
   events per second and the p95 recording latency, so later producers know
   what the serialization costs.
   Command: `uv run python scripts/bench_outbox.py --writers 8 --seconds 60`
