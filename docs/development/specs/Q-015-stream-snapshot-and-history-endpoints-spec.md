# Q-015: Stream snapshot and history endpoints

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.2, §4.3](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#42-snapshot-then-delta-without-the-race)  
**Depends on:** Q-012  
**Implementation plan:** [`../plans/Q-015-stream-snapshot-and-history-endpoints-plan.md`](../plans/Q-015-stream-snapshot-and-history-endpoints-plan.md)

## Purpose

A stream on its own is not a consistent view. A client that connects sees only
what happens next. A client that falls behind is told it is lagging and has
nowhere to fetch what it missed. A client that reconnects after Redis was
flushed has nothing to resume from. Architecture §4.2's protocol closes these
cases over REST: subscribe, fetch a snapshot whose watermark says exactly which
events it already reflects, then fill any gap from history by sequence.
`q_contracts` Finding 4 records that none of those endpoints exist. This task
adds them for everything that is on the stream after Q-012 and Q-013: history
for durable topics, latest values for ephemeral topics, and a job snapshot
with a race-free watermark. It also records them in the captured contract.
Without this task, the frontend's move off polling (Q-016) would trade a slow,
correct view for a fast one that can be wrong.

## Requirements

### History

- A client can read a durable topic's events from a given sequence number within
  a given epoch, in sequence order, in bounded pages, and learns whether more
  follow.
- A request below the oldest retained event is answered as expired, naming the
  oldest sequence still available. It is never answered as an empty page.
- A request naming an epoch other than the topic's current epoch is answered as an
  epoch mismatch, carrying the current epoch, so the client knows to re-snapshot
  rather than fetch.
- History is served from Postgres and works while Redis is unavailable.
- Requesting history for an ephemeral topic is refused, because ephemeral topics
  have no history on the stream (§4.1).

### Latest

- A client can read the latest entry for every routing key of an ephemeral topic,
  or for one given key, each with its sequence and epoch.
- A latest-value response for a topic with an Arrow payload conforms to the
  contract's replay schema, and its payload decodes to the same batch as on the
  stream.
- While Redis is unavailable, the endpoint answers stream unavailable rather than
  an empty result.

### Job snapshot

- A client can fetch one snapshot of jobs, covering every job that is active and
  every job that reached a terminal state within the last twenty-four hours, with
  a watermark for the terminal topic and, per job, the sequence and epoch of the
  latest progress entry reflected.
- The snapshot and its terminal watermark are mutually consistent. Every terminal
  event at or below the watermark is reflected in the snapshot, and no terminal
  event above it is.
- A job whose durable terminal event exists is reported as terminal, even if its
  non-durable progress record still says it is running.
- Job status in the snapshot uses the stream vocabulary, so a client applies
  snapshot and stream events with one set of rules.

### Contract

- Every new endpoint's responses conform to the replay schemas Q-009 defined
  where one applies, and the captured OpenAPI document in `q_contracts` is
  updated to include them, with the route drift check passing against the
  running API.
- Finding 4 in `q_contracts` is marked resolved for job and market-data topics
  and left open for execution topics.

### Preserved behavior

- No existing REST route or response changes. The existing job status endpoints
  keep serving their current payloads.

## Constraints and non-goals

- **No execution-topic snapshots.** Deployments, orders, fills, risk, and ledger
  snapshots need those topics' producers, which arrive in phase 4. History for
  them works generically from the outbox; snapshots do not exist yet.
- **No history for ephemeral topics, including completed bars.** Completed bars
  after a lag are fetched from the existing market-data REST endpoints by time
  range. A second history path for bars would compete with the lake.
- **No pagination beyond a sequence cursor.** Offset and time-range queries over
  the outbox are not added. The existing execution endpoints already serve time
  ranges for their own tables.
- **No caching layer.** The snapshot is read on connect and on re-snapshot,
  which is rare compared with polling. Any latency problem is measured first.
- **No client.** Consuming these endpoints is Q-016.

## Acceptance criteria

### Agent-verifiable

1. History from sequence 5 with a page size of 3, over a topic holding events 1
   to 10, returns 5 to 7 and signals that more follow. The last page signals
   none.
2. History below the oldest retained event, after pruning, returns an expired
   response naming the oldest retained sequence.
3. History naming a stale epoch returns an epoch mismatch carrying the current
   epoch.
4. History answers while Redis is stopped.
5. History for an ephemeral topic is refused.
6. Latest for quotes, after interleaved publishes on two symbols, returns the last
   entry for each symbol with its sequence and epoch, and the payload decodes to
   the published batch. With a key given, only that symbol is returned.
7. Latest answers stream unavailable while Redis is stopped.
8. A job snapshot taken while a concurrent transaction records a terminal event
   either reflects that job as terminal with a watermark at or above the event's
   sequence, or reflects it as active with a watermark below it, across two
   hundred repeated trials.
9. A Redis-only job whose terminal event is committed, but whose progress key
   still says running, appears in the snapshot as terminal.
10. Every new response validates against its Q-009 schema.
11. The captured OpenAPI document includes the new routes, and the drift check
    passes against the running API.
12. The full validation suite passes, in `q_backend` and in `q_contracts`.

### Human-verifiable

1. With the API, a worker, and the relay running and two backtests active, the
   job snapshot, followed by the terminal events that arrive as the backtests
   finish, is inspected and confirmed to describe the jobs correctly.
   Command: `curl -s localhost:8000/api/v1/stream/jobs/snapshot | jq` then `redis-cli -p 6380 XRANGE q:stream:jobs.terminal - +`
