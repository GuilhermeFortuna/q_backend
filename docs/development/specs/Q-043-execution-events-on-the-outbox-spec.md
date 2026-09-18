# Q-043: Execution events on the outbox

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.1, §4.2, §4.3, §4.4, §9 invariants 2 and 7, §10 Phase 4](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#42-snapshot-then-delta-without-the-race)  
**Depends on:** Q-039  
**Implementation plan:** [`../plans/Q-043-execution-events-on-the-outbox-plan.md`](../plans/Q-043-execution-events-on-the-outbox-plan.md)

## Purpose

The outbox, the relay, the stream endpoint and generic history have carried
job events since batch 02. Execution, the reason durable topics exist, still
writes nothing to them. The frontend's execution workspace polls eleven
endpoints every one to two seconds, and a terminal fed only by the stream would
see nothing. This task makes every execution state change produce its Q-039
event in the same transaction as the change, and adds the execution snapshot
that §4.2 needs: one consistent read of execution state with the watermark of
every execution topic. After this task a client can hold an exact, live copy of
execution state with no polling.

## Requirements

### Events

- Every committed change to a deployment, decision, order, fill, net position,
  ledger entry, risk event or the kill switch produces exactly one event per
  changed entity on its topic, in the same transaction as the change. A rolled
  back change produces no event.
- This holds for every writer: the worker's bar path, flatten, recovery that
  marks orders unknown, automatic and manual reconciliation, and every API
  command that changes execution state.
- Each event carries the entity's full state after the change, in the Q-039
  shape, with the routing key values the contract declares. Every payload is
  validated against its contract model before it is written, and a payload that
  does not validate fails the transaction.
- A fill event carries the deployment's net position after the fill, and a
  ledger event carries the account's balances after the entry.

### Snapshot

- `GET /api/v1/stream/execution/snapshot` returns the execution snapshot of Q-039. It
  is read in one repeatable-read transaction, and its watermark for each
  execution topic is the highest sequence number visible to that transaction.
- The snapshot's recent windows use the limits the contract declares. Older
  rows remain available from the existing paged routes.
- With Postgres unavailable the route answers `503`, like every other
  persistence route.

### Consistency

- A client that follows §4.2 against this snapshot and the stream ends with the
  same state as a fresh snapshot taken afterwards, for any interleaving of
  worker and API writes. A test proves it.

### Contract record

- The OpenAPI capture in `q_contracts` is refreshed with the snapshot route, and
  the API FINDINGS entry for the missing execution snapshot is closed.

## Constraints and non-goals

- **No change to execution behaviour or to existing routes.** The paged routes
  stay, because `q_frontend` uses them until Q-050, and the terminal uses them
  for older rows.
- **No per-deployment filtering on the stream endpoint.** The routing keys make
  it possible later.
- **No new topics.** Positions ride on fills, and balances ride on ledger
  entries, as Q-039 decided.
- **No change to the relay, retention or backpressure.**

## Acceptance criteria

### Agent-verifiable

1. For each writer path listed above, a test commits a change and finds exactly
   the expected events with the next sequence numbers. A forced rollback leaves
   no event and no consumed sequence number.
2. Every event written in the test suite validates against its vendored
   contract schema, and a deliberately invalid payload fails its transaction.
3. A fill event's `position_after` equals the net position row after the
   commit, and a ledger event's `account_after` equals the account row.
4. The snapshot validates against the contract. Its watermark equals the
   maximum outbox sequence per topic visible to its transaction, including when
   a concurrent write commits during the read.
5. A property-style test interleaves random worker and API writes with a client
   that subscribes, snapshots and applies deltas per §4.2. Its final state equals
   a fresh snapshot, over at least 200 seeded runs.
6. The snapshot answers `503` with Postgres down.
7. The recaptured OpenAPI contains the route, and `make contracts-check`
   passes.
8. Existing execution and API tests pass with unchanged expected values.
9. The full validation suite passes.

### Human-verifiable

1. With `./research` and a paper deployment running, a stream subscription to
   `decisions`, `orders`, `fills` and `ledger` shows one decision per completed
   bar, and an order, a fill and a ledger event for each trade. Their contents
   match the paged routes.
   Command: `websocat ws://127.0.0.1:8000/api/v1/stream` with
   `{"type":"subscribe","topics":["decisions","orders","fills","ledger"]}`, and
   `curl -s http://127.0.0.1:8000/api/v1/stream/execution/snapshot | jq`
