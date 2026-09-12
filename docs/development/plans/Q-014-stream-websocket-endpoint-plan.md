# Q-014 implementation plan: Stream WebSocket endpoint

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-014-stream-websocket-endpoint-spec.md`](../specs/Q-014-stream-websocket-endpoint-spec.md)  
**Depends on:** Q-011, Q-013

## Current-system context

The API is a FastAPI app (`api/main.py`) with fourteen routers, each included
with `app.include_router`, a permissive CORS middleware, and a `lifespan`
(`api/lifespan.py`) that initializes market data and reconciles orphaned jobs.
There is no WebSocket route. Every route is synchronous or thread-pooled, and the
only Redis client is the synchronous `storage/redis/client.py::get_redis`
(`decode_responses=True`). The lockfile pins `starlette 1.2.0`, whose
`WebSocket.send_denial_response` sends an HTTP response in place of the
handshake; `uvicorn 0.48.0`, whose websockets implementation advertises the
`websocket.http.response` extension that makes that denial possible;
`websockets 16.0`, present transitively and not declared; `redis 7.4.1`, which
ships `redis.asyncio`; and `fakeredis 2.36.1`, which supports async and streams.
`storage/health.py::check_redis` is the existing Redis probe. Tests use
`fastapi.testclient.TestClient`, which supports `websocket_connect`.

After Q-011, streams live at `streaming/keys.stream_key(topic)` with entries
`{h: header JSON, p: raw payload}`. `streaming/codec.decode_entry` rebuilds an
envelope and `routing_key_string` produces coalesce keys. The stream epoch is at
`STREAM_EPOCH_KEY`, and per-topic ephemeral epochs are at `topic_epoch_key`.
`q_contracts.topics.TOPICS` gives each topic's class, `coalesce_key`, and
`on_overflow`. Q-009 added `cursors` to the subscribe frame, the `rejected` frame,
and the binary framing in `schema/stream/framing.md`. The gap: nothing reads a
stream on a client's behalf.

## Interfaces produced

```python
# src/q_backend/api/routers/stream.py
router = APIRouter(tags=["stream"])

@router.websocket("/api/v1/stream")
async def stream_socket(websocket: WebSocket) -> None: ...
```

```python
# src/q_backend/streaming/ws/frames.py
def text_frame(obj: Mapping[str, Any]) -> str: ...                  # control frames, control envelopes
def binary_frame(header: Mapping[str, Any], payload: bytes) -> bytes: ...   # u32 LE len + JSON + IPC
def parse_client_frame(raw: str) -> SubscribeFrame: ...              # raises FrameError
```

```python
# src/q_backend/streaming/ws/queue.py
@dataclass
class QueuedEntry:
    stream_id: bytes
    seq: int
    epoch: str
    routing_key: str | None
    frame: str | bytes

class TopicQueue:
    """Bounded per-client, per-topic outbound queue applying the topic's overflow policy."""
    def __init__(self, topic: str, policy: TopicPolicy, capacity: int) -> None: ...
    def offer(self, entry: QueuedEntry) -> OfferResult: ...   # ACCEPTED | COALESCED | OVERFLOW
    def pop(self) -> QueuedEntry | None: ...
    def __len__(self) -> int: ...
    lagging_from_seq: int | None                          # set on OVERFLOW for lag topics
```

```python
# src/q_backend/streaming/ws/session.py
QUEUE_CAPACITY: Mapping[str, int]   # per topic; durable 1024, quotes 64 keys, bars.* 256
XREAD_BLOCK_MS = 1000
XREAD_COUNT = 256

class StreamSession:
    def __init__(self, websocket: WebSocket, redis: redis.asyncio.Redis, clock) -> None: ...
    async def run(self) -> None: ...          # reader, writer, and receiver tasks; returns on close
    async def _receive_loop(self) -> None: ...  # subscribe frames
    async def _read_loop(self) -> None: ...     # one XREAD over all subscribed streams
    async def _write_loop(self) -> None: ...    # round-robin pop across TopicQueues
    async def _watch_epochs(self) -> None: ...  # stream epoch + per-topic epochs, every XREAD pass
```

```python
# src/q_backend/streaming/redis_binary.py  (addition)
def get_async_binary_redis() -> redis.asyncio.Redis: ...
```

```
pyproject.toml             declare websockets>=16 explicitly (currently transitive)
scripts/stream_soak.py     human criterion 1
tests/streaming/ws/        TestClient + fakeredis async; integration variants on real Redis
```

## Implementation decisions

- **One `XREAD` per connection over all of its subscribed streams, not one
  reader task per topic.** A single `XREAD BLOCK` with several stream keys is one
  Redis round trip per wake-up. With a task per topic, a client subscribed to
  five topics would hold five blocking connections. The reader keeps the last
  delivered stream id per topic and passes the set to each call.

- **The reader, writer, and receiver are three tasks joined by the
  `TopicQueue`s, and the reader never awaits the socket.** If the task that reads
  Redis also sends on the socket, a slow client stalls its own reads, and entries
  pile up in Redis-client buffers outside any bound. Separating the tasks puts
  every byte waiting for a slow client inside a `TopicQueue`, where the bound and
  the overflow policy apply.

- **Coalescing replaces, in place, the queued entry with the same routing key,
  keeping the older entry's queue position.** Appending the new entry and removing
  the old one would move a symbol's latest quote behind other symbols' older
  ones each time it updates. A hot symbol could then starve a quiet one. Replacing
  in place keeps round-robin fairness across keys and satisfies "coalescing never
  reorders survivors". An `OrderedDict` keyed by routing key gives O(1) replace.

- **Capacities are per topic class, not one global number.** Durable queues hold
  1024 entries because a durable overflow forces the client to re-snapshot, and
  job-terminal bursts are small. Coalescing topics are bounded by distinct keys
  (64 symbols for quotes), because after coalescing the queue can never hold more
  than one entry per key. Their bound is really the key limit, and a 65th key is
  an overflow. Q-013's handoff reports observed entry rates, and the capacities are
  checked against them in the handoff.

- **A durable overflow sends `lagging {topic, from_seq}`, drops that topic's
  queue, and stops reading it for this connection. `bars.completed` overflow
  sends `lagging` and then continues from the stream's newest entry.**
  Architecture §4.4 distinguishes the two cases. Durable topics are replayable,
  so the client re-runs the snapshot protocol and loses nothing. `bars.completed`
  has no snapshot, so continuing from the newest entry, with the gap named, is
  the most useful thing to do. `from_seq` is the `seq` of the first entry not
  queued.

- **Epoch checks compare each entry's header epoch with the epoch last sent to
  the client for that topic, and the stream epoch is re-read on every `XREAD`
  wake-up.** Per-entry comparison catches an ephemeral epoch reset (Q-011) on the
  first entry of the new epoch, which is the exact point where the client must be
  told first. The stream epoch check catches a flushed Redis even on topics that
  are idle. Reading a string key once a second per connection costs nothing next
  to the `XREAD` itself.

- **Expired cursors are detected by comparing the client's cursor with the
  stream's first entry id (`XINFO STREAM`), and a missing stream counts as
  expired only when a cursor was given.** A cursor below the first id means
  entries between them were trimmed, so forwarding would silently skip them. A
  fresh subscription to an empty stream is not expired; it is idle.

- **The subscription acknowledgement's `cursor` is the stream's current last id
  (`XINFO STREAM last-generated-id`), read before the reader starts, and
  `last_seq` is the `seq` in that entry's header.** Starting the reader from that
  id guarantees no entry appended after the acknowledgement is missed. Q-015's
  snapshot watermark then covers everything up to it. This is the race-free
  ordering architecture §4.2 requires: subscribe first, then snapshot.

- **The handshake is refused with `send_denial_response` returning HTTP 503 and
  a JSON `stream_unavailable` error when the Redis ping fails.** Architecture
  §8.1 specifies a 503. Accepting and then closing with a close code would make a
  client treat an outage as a protocol error rather than a service state, and
  browsers hide close reasons. Mid-connection loss sends Q-009's `rejected
  {reason: stream_unavailable}` and closes with 1011, because by then the HTTP
  channel is gone.

- **The endpoint uses `redis.asyncio` with a dedicated connection pool, sized
  from settings, not the synchronous client in a thread.** A blocking `XREAD`
  held in a thread pool per connection would exhaust Starlette's default thread
  limiter (40) with a handful of clients and starve the synchronous REST routes.
  That is what the "does not slow REST" requirement guards against, and
  criterion 10 tests it.

- **Frames are built once per entry, at read time, and shared by reference with
  every queue that receives them.** Connections are independent, so there is no
  cross-connection fan-out cache in this task. The frame is still built at most
  once per entry per connection, and the payload bytes from Redis are passed
  through without copying into a new buffer. If the soak shows serialization is
  the CPU cost, that is where a shared cache would go.

- **`websockets>=16` is declared in `pyproject.toml`.** It is present today only
  as a transitive dependency. Without a WebSocket implementation uvicorn silently
  serves no WebSocket routes, and a dependency tidy-up elsewhere would break the
  endpoint with no error at startup.

## Ordered implementation

1. Create the branch `Q-014-stream-websocket-endpoint-spec` in `q_backend` from
   `development`, after Q-011 and Q-013 are merged. Q-013 is a dependency because
   the queue capacities are sized from its observed entry rates, and the soak
   (human criterion 1) runs against its live publisher.
2. Declare `websockets>=16`, and add `get_async_binary_redis`. Write failing
   tests in `tests/streaming/ws/test_frames.py`: `binary_frame` of a header and 10
   payload bytes starts with `len(header_json)` as u32 little-endian, and decoding
   it with the Q-009 test helper returns the header and bytes; `parse_client_frame`
   of `{"topics": []}` raises `FrameError`; a frame with `cursors` parses. Confirm
   they fail, implement, confirm they pass. Commit.
3. Write failing tests in `tests/streaming/ws/test_queue.py`:
   - quotes (coalesce by symbol) with capacity 2: offer A1, B1, A2 → pop yields
     A2, then B1 (A kept its position); offer C1 → `OVERFLOW`
   - durable `jobs.terminal` with capacity 3: offer seq 1..4 → the 4th returns
     `OVERFLOW` and `lagging_from_seq == 4`
   - `bars.completed` with capacity 2: offer 1, 2, 3 → `OVERFLOW` with
     `lagging_from_seq == 3`
   Confirm they fail, implement, confirm they pass. Commit.
4. Write failing tests in `tests/streaming/ws/test_subscribe.py` using `TestClient`
   and async `fakeredis`: subscribe `["jobs.progress", "jobs.terminal", "nope"]`
   yields `subscribed` with two topics and `rejected {reason: unknown_topic,
   topic: "nope"}`; entries published before the acknowledgement are not received;
   three published after it arrive in `seq` order; a later subscribe adding
   `quotes` yields a second acknowledgement and quotes entries. Confirm they fail.
   Implement the router, `StreamSession`'s receive and read loops, and the writer.
   Include the router in `api/main.py`. Confirm they pass. Commit.
5. Write failing tests for cursors: publish 10 entries, subscribe with the 5th
   entry's id as cursor, and receive exactly entries 6–10; trim the stream to its
   last 3 entries, subscribe with the 1st entry's id, and receive `cursor_expired`
   and no entries. Confirm they fail, implement, confirm they pass. Commit.
6. Write a failing test for binary delivery: publish a quotes entry with a 3-row
   IPC payload; the client receives `bytes`; decoding yields the published header
   and IPC bytes equal to the published payload. Confirm it fails, implement,
   confirm it passes. Commit.
7. Write failing `integration` tests on real Redis for backpressure. A client
   subscribes to quotes and stops reading (the test holds the receive side) while
   2,000 entries across symbols A and B are published; resuming, the next frames
   are exactly the latest A and latest B, and a probe asserts queue length ≤ 2
   throughout. A second client subscribes to `jobs.terminal` and stops reading
   while 2,000 are published; it receives `lagging` with the first unqueued
   `seq`, then no further `jobs.terminal` frames within 2 s. A third fast client
   connected alongside receives all 2,000 with p95 inter-arrival delay under the
   stated tolerance. Confirm they fail before the queue is wired in, implement,
   confirm they pass. Commit.
8. Write failing `integration` tests for epochs: subscribe to `jobs.progress` and
   `jobs.terminal`, `FLUSHALL`, publish one entry on each, and receive
   `epoch_changed` for both topics before any entry of the new epoch. Confirm they
   fail, implement `_watch_epochs`, confirm they pass. Commit.
9. Write failing tests for unavailability: with the async Redis pointed at a
   closed port, `websocket_connect` fails with HTTP 503 and body `{"code":
   "stream_unavailable", ...}`; with Redis stopped mid-connection (real Redis,
   integration), the client receives `rejected {reason: stream_unavailable}` and
   close code 1011. Confirm they fail, implement, confirm they pass. Commit.
10. Write a failing `integration` test for REST isolation: while one client
    receives a synthetic 500-entries-per-second quotes stream for 10 s, 100
    sequential `GET /api/v1/system/health` requests have p95 latency within 2× the
    p95 measured with no stream client. Confirm it fails with a thread-pooled
    synchronous reader (temporarily substituted), and passes with the async
    reader. Commit.
11. Write `scripts/stream_soak.py`: N WebSocket clients subscribe to the given
    topics for M minutes. They record per-frame append-to-send latency (header
    `origin_ts` is producer time, so the script uses the Redis stream id's
    millisecond component as append time) and sample the API process's CPU with
    `/proc/<pid>/stat` (utime + stime deltas; `psutil` is not a dependency and is not added for a script). The script prints p50 and p95 latency and mean CPU. Commit.
12. Human step, matching human-verifiable criterion 1: run the market-data
    publisher on one live symbol, then the soak with 2 clients for 10 minutes on
    `quotes,bars.forming`. Record the figures.
13. Human step, matching human-verifiable criterion 2: from the research UI's dev
    server console, open the socket, subscribe to `quotes`, and confirm in the
    devtools frames view that quote frames are binary and their header decodes.
14. Run the full validation suite. Commit.

## Validation

- **Unit:** framing; queue policies (coalesce in place, key-bound overflow, durable
  lag, non-durable lag continue); subscribe parsing.
- **Integration:** subscribe, reject, and add topics; order and no pre-ack
  delivery; resume and expired cursors; binary delivery; stalled clients bounded
  and isolated from a fast client; epoch change on flush; 503 at handshake and
  rejection mid-connection; REST latency unaffected.
- **Regression:** the full existing API suite passes, and no router other than the
  new one changes.
- **Manual:** step 13.
- **Measurement:** 10-minute soak, 2 clients, live symbol: p50 and p95
  append-to-send latency and mean API CPU.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/streaming/ws -v
uv run dev &
uv run q-market-publisher --symbols 'WIN$N' --timeframes M1 &
uv run python scripts/stream_soak.py --clients 2 --topics quotes,bars.forming --minutes 10
```

## Handoff

Report the backpressure test's observed maximum queue length per stalled client,
the `lagging` frame received verbatim, and the fast client's p95 inter-arrival
delay alongside the stalled clients. Report the REST isolation figures: health p95
with and without the stream, and with the thread-pooled reader that the test
rejected. Report the 10-minute soak: p50 and p95 append-to-send latency and mean
API CPU, and state whether the spec's 50 ms and 10% bounds were met. If they were
not, say so plainly and do not tune in this task. Report the queue capacities
chosen and the Q-013 entry rates they were checked against.
