# Q-011: Redis Streams relay and ephemeral publisher

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.1, §4.3](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#43-redis-retention-and-recovery)  
**Depends on:** Q-010  
**Implementation plan:** [`../plans/Q-011-redis-streams-relay-and-publisher-plan.md`](../plans/Q-011-redis-streams-relay-and-publisher-plan.md)

## Purpose

After Q-010, durable events are committed to Postgres and go nowhere. Ephemeral
data — job progress, quotes, forming bars — has no stream at all. Job progress
lives in a Redis key that is overwritten in place and can only be polled. This
task builds both ways onto Redis Streams. The first is a relay that moves
committed outbox events into their streams in order, at least once, and
recovers from a Redis restart without losing a durable event. The second is a
publishing primitive for ephemeral topics that gives every entry a gapless
per-topic sequence even when many worker processes publish at once. It is the
single path every stream producer in the backend uses. The job-event producer
(Q-012), the market-data publisher (Q-013), and the WebSocket endpoint (Q-014)
all depend on it.

## Requirements

### Durable relay

- Every committed outbox event is appended to its topic's stream in sequence
  order, with no event skipped and none reordered relative to its topic.
- An event may be appended more than once after a crash, but never out of order.
  Consumers deduplicate on topic, epoch, and sequence.
- The relay's progress is recorded in Postgres only after the append is
  confirmed, so a crash between the two can duplicate an event but cannot lose
  one.
- Newly committed events reach their stream within one second while the relay is
  healthy.
- While Redis is unreachable, the relay retries with bounded backoff, records no
  progress, and resumes where it stopped once Redis returns.
- While Postgres is unreachable, the relay retries with bounded backoff and does
  not exit, so a database restart does not require restarting the relay.
- The relay prunes outbox events past retention on a schedule, within the limits
  Q-010 defined.

### Recovering from Redis loss

- A Redis restart is detectable by every stream component through a stream epoch
  value held in Redis, which exists exactly when Redis has not lost its data.
- When the stream epoch changes, the relay republishes each durable topic's
  retained window from Postgres, so a client reconnecting to an empty Redis can
  still replay the recent past.

### Ephemeral publishing

- Any process can publish to an ephemeral topic. Sequence numbers within the
  topic's epoch are gapless and in stream order, however many processes publish
  concurrently.
- An ephemeral topic's epoch changes when, and only when, its sequence counter is
  lost. A subscriber therefore never sees a sequence go backwards within one
  epoch.
- Publishing also maintains the latest entry per routing key, so a client can
  get the current value of a key without reading the stream.
- Publishing to a durable topic through this path is refused. Durable events go
  through the outbox only.
- A failed publish raises to the caller. Whether a producer tolerates that is the
  producer's decision, not this primitive's.

### Retention and encoding

- Each stream is trimmed to approximately its topic's declared retention entry
  count from the vendored topic policy. No retention value is written in backend
  code.
- An entry stores the envelope's header fields and the payload such that an Arrow
  payload is held as raw bytes, never as base64 text.
- An entry read back from Redis reconstructs to an envelope equal to the one
  written.

### Operation

- The relay runs as its own long-lived command, separate from the API and the
  research workers, and shuts down cleanly on a termination signal without
  recording progress for an append it has not confirmed.

## Constraints and non-goals

- **No WebSocket and no consumer.** Reading streams for clients is Q-014.
  Testing here reads Redis directly.
- **No producers.** Job events are Q-012 and market data is Q-013. This task
  proves the paths with test producers.
- **No systemd unit and no readiness notification.** Service units are
  Batch 03. The relay is started by hand in this batch.
- **No replacement of the existing progress keys.** `set_job_progress` and the
  REST status endpoints that read its keys keep working unchanged.
- **No Redis persistence configuration.** Redis stays non-persistent, as
  architecture §4.3 assumes. Making it persistent would hide the recovery path
  this task has to prove.
- **No multi-instance relay or leader election.** There is one relay. A second
  instance is a deployment error, which the relay detects and refuses; it does
  not coordinate.

## Acceptance criteria

### Agent-verifiable

1. Events committed on two durable topics appear in their streams in sequence
   order, and the relay's recorded progress equals the last appended sequence.
2. Killing the relay between append and progress record, then restarting it,
   produces a duplicate of at most that one event and no gap.
3. With Redis stopped, the relay records no progress and does not exit. With
   Redis restarted, all events committed meanwhile are appended in order.
4. After Redis is flushed, the stream epoch changes and the relay republishes
   each durable topic's retained window, bounded by the topic's retention entry
   count.
5. Eight processes publishing one hundred entries each to one ephemeral topic
   produce eight hundred entries whose sequences are exactly one through eight
   hundred in stream order.
6. Flushing Redis gives an ephemeral topic a new epoch, and its sequence restarts.
7. The latest-per-key value after interleaved publishes on two keys equals the
   last entry published for each key.
8. Publishing to a durable topic through the ephemeral path raises.
9. Stream length stays within the approximate retention bound after publishing
   twice the declared entry count.
10. A quotes envelope with an Arrow payload round-trips through Redis to an equal
    envelope, and the stored payload is raw bytes.
11. A second relay instance started while the first is running refuses to run.
12. The full validation suite passes.

### Human-verifiable

1. Relay latency, from commit to stream append, is measured over ten minutes of
   the outbox benchmark's write load, reporting p50, p95, and maximum.
   Command: `uv run q-outbox-relay & uv run python scripts/bench_outbox.py --writers 8 --seconds 600 --measure-relay`
2. Ephemeral publish throughput is measured against the development Redis with
   eight publishing processes for sixty seconds, reporting entries per second and
   p95 publish latency.
   Command: `uv run python scripts/bench_publish.py --processes 8 --seconds 60`
