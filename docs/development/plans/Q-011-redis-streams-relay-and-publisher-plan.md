# Q-011 implementation plan: Redis Streams relay and ephemeral publisher

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-011-redis-streams-relay-and-publisher-spec.md`](../specs/Q-011-redis-streams-relay-and-publisher-spec.md)  
**Depends on:** Q-010

## Current-system context

Redis is reached through `storage/redis/client.py::get_redis`, which builds a
fresh `redis.Redis.from_url(settings.redis_url, decode_responses=True)` on each
call. With `decode_responses=True`, the client returns `str`, so it cannot hold
raw Arrow bytes. `storage/redis/progress.py` provides `set_job_progress`,
`get_job_progress`, and `delete_job_progress` over `"<namespace>:progress:<id>"`
string keys with a 24-hour TTL (`DEFAULT_PROGRESS_TTL_SECONDS = 86400`).
Dramatiq also uses the same Redis through `tasks/broker.py`. `fakeredis` is a
dev dependency, and `tests/storage/conftest.py::redis_client` supplies a
`FakeRedis(decode_responses=True)`. Settings default `redis_url` to
`redis://localhost:6380/0`. Standalone long-running commands follow
`cli/q_execution.py`: argparse, `init_sentry`, a `_LiveClock`, and a script
entry in `pyproject.toml`. That command's single-instance guard,
`storage/db/execution_repositories.acquire_worker_lease`, is keyed by a foreign
key to `execution_deployments`, so it cannot guard a process that has no
deployment.

After Q-010, `streaming/outbox.py` records events into `stream_outbox` with a
row-locked `stream_outbox_topic_state.last_seq`, and each state row has a
`last_relayed_seq` column that nothing advances. `q_contracts.topics.TOPICS`
exposes retention entries and coalesce keys, and `q_contracts.stream.StreamEnvelope`
is the logical envelope. The gap this task closes:
nothing moves outbox rows into Redis, and nothing can publish an ephemeral entry
with a sequence that is valid across processes.

## Interfaces produced

```python
# src/q_backend/streaming/keys.py
STREAM_EPOCH_KEY = "q:stream:epoch"
def stream_key(topic: str) -> str: ...        # "q:stream:<topic>"
def seq_key(topic: str) -> str: ...           # "q:seq:<topic>"      ephemeral counter
def topic_epoch_key(topic: str) -> str: ...   # "q:epoch:<topic>"    ephemeral epoch
def latest_key(topic: str) -> str: ...        # "q:latest:<topic>"   hash: routing-key → entry id
```

```python
# src/q_backend/streaming/codec.py
def encode_entry(envelope: StreamEnvelope, payload_bytes: bytes | None) -> dict[bytes, bytes]: ...
    """Redis entry fields: b"h" = JSON header (envelope minus payload), b"p" = raw payload bytes."""
def decode_entry(fields: Mapping[bytes, bytes]) -> tuple[StreamEnvelope, bytes]: ...
def routing_key_string(topic: str, routing_key: Mapping[str, str]) -> str: ...
    """Values joined in the topic's coalesce_key order, e.g. "WINZ25|M1"."""
```

```python
# src/q_backend/streaming/redis_binary.py
def get_binary_redis() -> redis.Redis: ...    # decode_responses=False; same URL as get_redis
def ensure_stream_epoch(client: redis.Redis) -> tuple[str, bool]: ...
    """(epoch, created) — SET NX a new epoch if absent; created=True means Redis lost its data."""
```

```python
# src/q_backend/streaming/publisher.py
class EphemeralPublishError(RuntimeError): ...

class EphemeralPublisher:
    def __init__(self, client: redis.Redis, topic: str, *, producer_id: str) -> None: ...
        # raises ValueError for a durable or undeclared topic
    def publish(
        self,
        *,
        routing_key: Mapping[str, str],
        payload_kind: Literal["arrow_ipc", "control"],
        payload_schema: str,
        payload: bytes | Mapping[str, Any],
        origin_ts: datetime | None = None,
    ) -> tuple[str, int]: ...   # (epoch, seq) assigned atomically with the XADD
```

```lua
-- src/q_backend/streaming/lua/publish_ephemeral.lua
-- KEYS: seq_key, topic_epoch_key, stream_key, latest_key
-- ARGV: candidate_epoch, header_json_without_seq_epoch, payload, maxlen, routing_key_string
-- SET epoch NX; INCR seq (if epoch was just created, seq was absent → 1);
-- header gains seq+epoch; XADD MAXLEN ~ maxlen; HSET latest routing_key_string → id
-- returns {epoch, seq, id}
```

```python
# src/q_backend/streaming/relay.py
@dataclass(frozen=True)
class RelayConfig:
    poll_interval_s: float = 0.2
    batch_size: int = 500
    prune_interval_s: float = 3600.0
    max_backoff_s: float = 30.0

class OutboxRelay:
    def __init__(self, session_factory, client: redis.Redis, config: RelayConfig, clock) -> None: ...
    def run_once(self) -> int: ...                 # events appended this pass
    def republish_retained(self) -> dict[str, int]: ...   # on stream-epoch creation
    def run_forever(self, stop: threading.Event) -> None: ...

# src/q_backend/cli/q_outbox_relay.py   (project script "q-outbox-relay")
def main(argv: list[str] | None = None) -> int: ...
```

## Implementation decisions

- **Ephemeral sequence and epoch are assigned by a Lua script in Redis, in the
  same atomic step as the `XADD`, rather than by a counter local to the
  publisher.** Architecture §4.1 says "publisher-local monotonic counter", which
  assumes one publisher process per topic. `jobs.progress` is written from every
  Dramatiq worker (`worker_processes` defaults to 28). Per-process counters would
  interleave several epochs on one topic, and a client would see an epoch change
  on nearly every entry. Inside the script, `INCR` and `XADD` cannot be
  separated, so stream order and `seq` order are identical. The counter lives in
  Redis and disappears exactly when Redis loses its data, which is the one event
  that should change an ephemeral epoch. This keeps the guarantees §4.1 intends
  and is reported as a wording deviation, as Q-010 does for durable sequences.

- **The epoch key is created with `SET NX` inside the script, and the counter is
  only incremented after it.** If a flush lands between two publishes, the next
  publish finds no epoch, creates a new one, and `INCR` on the absent counter
  yields 1. Epoch and sequence reset together. There is no state where a new
  epoch continues an old count.

- **An entry stores two fields: `h`, the JSON header, and `p`, the raw payload.**
  Q-009's framing sends Arrow payloads as raw bytes. Base64 in Redis would mean
  encoding on publish and decoding before every WebSocket send, on the quotes
  path. Keeping the header separate lets Q-014 read routing and `seq` without
  touching the payload. Control payloads are stored in `p` as UTF-8 JSON.

- **A separate `decode_responses=False` client is used for streams, and
  `get_redis` is untouched.** Changing `get_redis` would change the return type
  seen by every caller of `get_job_progress` and by the Dramatiq broker setup.

- **The relay reads with `SELECT ... WHERE topic = :t AND epoch = :e AND seq >
  last_relayed_seq ORDER BY seq LIMIT batch_size`, one topic at a time.**
  Ordering is only defined per topic. One global query ordered by
  `recorded_at` would reorder entries whenever two transactions' clocks
  disagree. The batch size bounds memory and pipeline size after a long outage.

- **The relay polls every 200 ms rather than using `LISTEN/NOTIFY`.** A
  notification received while the relay is reconnecting is lost, so a
  NOTIFY-driven relay still needs a polling catch-up path, and that path is the
  one that must be correct. At the durable topics' event rates (job terminals
  now, per-bar execution events later), 200 ms comfortably meets the one-second
  requirement. Human criterion 1 measures the real figure.

- **Progress is advanced in a Postgres transaction after the `XADD` pipeline
  returns, never before.** That is what "at least once" means. Reversing the
  order would turn a crash into a silent loss of a durable event.

- **After `ensure_stream_epoch` reports that it created the epoch, the relay
  republishes from `max(oldest_retained_seq, last_relayed_seq -
  retention_entries + 1)` for each durable topic, then continues normally.**
  A flushed Redis is empty, and clients resuming from a cursor get
  `cursor_expired` and fetch history over REST, so nothing is lost without the
  republish. The republish makes the common case — a Redis restart during a
  session — resumable from the stream instead of from REST, at a cost bounded by
  the retention count. Durable entries keep their outbox epoch. The stream epoch
  is a separate signal that Q-014 turns into `epoch_changed`.

- **The single-relay rule is a session-level Postgres advisory lock
  (`pg_try_advisory_lock` on a constant key), held on a dedicated connection for
  the life of the process.** A Redis lock disappears exactly when Redis restarts,
  which is the moment two relays republishing at once would do the most damage.
  The execution lease cannot be reused, because it is keyed to a deployment. An
  advisory lock needs no table and no heartbeat, and Postgres releases it when
  the holding connection dies, so a killed relay never leaves a stale lock
  behind for its replacement.

- **`MAXLEN ~` uses `TOPICS[topic].retention_entries`, and duration-based
  retention is not enforced here.** Redis trims by count or minimum id, and
  trimming by id means computing ids from timestamps on every append. The
  architecture states that retention values are "tuned by measurement". Count is
  the bound that protects memory, and the declared durations are reported
  against the observed stream span in the handoff.

- **Pruning runs from the relay every hour through Q-010's `prune_relayed`.** The
  relay is the only process that advances `last_relayed_seq`, so it is the only
  process that knows pruning is safe.

- **The relay does not exit when Postgres or Redis is unavailable.** It backs
  off exponentially, doubling from 0.5 s up to 30 s, and logs every state
  transition. Exiting would make every database restart a relay restart, and in
  this batch there is no systemd unit to perform it.

## Ordered implementation

1. Create the branch `Q-011-redis-streams-relay-and-publisher-spec` in
   `q_backend` from `development`, after Q-010 is merged.
2. Write failing tests in `tests/streaming/test_codec.py`: encoding a quotes
   envelope with 64 bytes of IPC payload yields fields `h` and `p` with `p` equal
   to those bytes; decoding returns an equal envelope; the routing key string for
   `bars.forming` with `{"timeframe": "M1", "symbol": "WINZ25"}` is `"WINZ25|M1"`.
   Confirm they fail, implement `keys.py` and `codec.py`, confirm they pass.
   Commit.
3. Write failing tests in `tests/streaming/test_publisher.py` against
   `fakeredis` with Lua support: the first publish on `jobs.progress` returns
   `seq` 1 and a new epoch; three publishes return 1, 2, 3 with one epoch; after
   `flushall` the next publish returns `seq` 1 and a different epoch; the latest
   hash after publishes on keys A, B, A points at the third entry for A;
   constructing a publisher for `jobs.terminal` raises `ValueError`. Confirm they
   fail. Implement the Lua script and `EphemeralPublisher`. Confirm they pass.
   Commit.
4. Write a failing `integration` test against the real Redis: 8 processes
   (`multiprocessing`) each publish 100 entries to `jobs.progress`; `XRANGE`
   returns 800 entries whose header `seq` values are exactly 1..800 in order.
   Confirm it fails with a naive non-atomic implementation, and passes with the
   script. Commit.
5. Write a failing test that publishing `2 × retention_entries` entries to
   `jobs.progress` leaves `XLEN` at most `retention_entries × 1.1` (approximate
   trim). Confirm it fails without `MAXLEN`, implement, confirm it passes.
   Commit.
6. Write failing `integration` tests for `OutboxRelay.run_once` against Postgres
   and Redis: 3 events recorded on `jobs.terminal` and 2 on `deployments` appear
   in their streams in `seq` order, and `last_relayed_seq` becomes 3 and 2; a
   second `run_once` appends nothing. Confirm they fail, implement, confirm they
   pass. Commit.
7. Write a failing `integration` test for crash safety: patch progress recording
   to raise after the pipeline executes for event 2 of 3; restart with a fresh
   relay; the stream holds 1, 2, 2, 3 (one duplicate) and `last_relayed_seq` is
   3. Confirm it fails if progress is recorded before the append. Commit.
8. Write failing `integration` tests for outages: with the Redis client pointed
   at a closed port, `run_forever` for 3 s records no progress, does not raise,
   and logs backoff; pointed back, it appends everything committed meanwhile.
   Repeat with an unreachable database URL. Confirm they fail, implement the
   backoff, confirm they pass. Commit.
9. Write a failing `integration` test for Redis loss: relay 10 events, `flushall`,
   run once; the stream epoch key has a new value, and the stream holds the
   retained window (all 10 here). With `retention_entries` monkeypatched to 4, it
   holds 7–10. Confirm it fails, implement `republish_retained`, confirm it
   passes. Commit.
10. Write the `q-outbox-relay` CLI with SIGTERM handling and the advisory lock.
    Write a failing `integration` test that a second relay started while a
    first holds the lock exits non-zero with a message saying another relay is
    running, and that after the first process is killed with SIGKILL a new relay
    acquires the lock within one second. Confirm it fails, implement,
    confirm it passes. Commit.
11. Write `scripts/bench_publish.py`, and add `--measure-relay` to
    `scripts/bench_outbox.py`, which records commit time in the payload and reads
    the stream to compute commit-to-append latency. Commit.
12. Human step, matching human-verifiable criterion 1: run the relay and the
    outbox benchmark for ten minutes, and record p50, p95, and maximum
    commit-to-append latency.
13. Human step, matching human-verifiable criterion 2: run the publish benchmark
    with 8 processes for 60 seconds, three times, and record entries per second
    and p95 latency, individual values and medians.
14. Run the full validation suite. Commit.

## Validation

- **Unit:** codec round-trip and raw bytes; routing key strings; publisher
  sequence, epoch reset, latest hash, and durable refusal (fakeredis).
- **Integration:** 8 × 100 concurrent publishes are gapless; approximate trim;
  relay order and progress; crash duplicates without loss; Redis and Postgres
  outages; Redis flush republish; single-instance advisory lock, including release on SIGKILL.
- **Regression:** the existing suite, including `tests/storage/test_redis_progress.py`,
  passes unchanged. `get_redis` behavior is unchanged.
- **Measurement:** commit-to-append p50/p95/max over 10 minutes; publish
  throughput and p95, 8 processes × 60 s × 3 runs.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/streaming -v
uv run q-outbox-relay &
uv run python scripts/bench_outbox.py --writers 8 --seconds 600 --measure-relay
uv run python scripts/bench_publish.py --processes 8 --seconds 60
```

## Handoff

Report the gapless-sequence test result: the entry count and the minimum and
maximum `seq`, and what the naive implementation produced before the Lua script,
so the atomic path is shown to be necessary. Report the crash test's stream
contents verbatim. Report the Redis-flush test's old and new stream epochs and
the republished range per topic. Report commit-to-append latency (p50, p95, max)
and publish throughput with individual runs and medians. Report, for
`jobs.progress` and `quotes`, the time span actually covered by a stream trimmed
at its declared entry count under the benchmark load, next to the declared
retention duration, so the count-only trimming decision is checked against the
duration the policy intends. State both architecture wording deviations
(Redis-assigned ephemeral sequence, count-only trimming) for amendment in
`q_contracts`.
