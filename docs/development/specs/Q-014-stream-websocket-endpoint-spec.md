# Q-014: Stream WebSocket endpoint

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.2–§4.4, §8.1](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#44-backpressure-per-topic)  
**Depends on:** Q-011, Q-013  
**Implementation plan:** [`../plans/Q-014-stream-websocket-endpoint-plan.md`](../plans/Q-014-stream-websocket-endpoint-plan.md)

## Purpose

After Q-011, events and market data sit in Redis Streams, and no client can
reach them. The API is REST only, and neither the research UI nor the terminal
should talk to Redis directly. This task adds the one stream endpoint both UIs
will use. It accepts subscriptions and forwards entries from a cursor. Each
topic's backpressure policy is applied per client: coalesce what may be
coalesced, and mark as lagging what may not be dropped. It tells clients when a
cursor has expired or an epoch has changed. A slow client degrades only itself.
Every later consumer relies on these guarantees without re-proving them: that a
durable event is either delivered in order or explicitly declared missing, and
that a quote is either the latest or superseded.

## Requirements

### Subscription

- A client subscribes to one or more declared topics, optionally with a resume
  cursor per topic. The server acknowledges each accepted topic with the cursor
  forwarding starts after, the topic's current epoch, and its last sequence.
- A topic that is undeclared is rejected, with a reason, without affecting the
  other topics in the same request.
- A subscription with no cursor starts from entries added after the
  acknowledgement, so a client never receives history it did not ask for.
- A cursor older than the retained stream is answered with an expired-cursor
  notice for that topic and no entries.
- A client can add topics to its connection later. It never receives an entry
  for a topic it has not subscribed to.

### Forwarding and order

- Within a topic, a client receives entries in stream order, and each entry at
  most once per connection.
- Entries are sent in the framing Q-009 defined. Arrow payloads travel as binary
  frames, never base64 text.
- An entry on a durable topic is never coalesced and never silently dropped.
- An entry on a topic that the policy allows to coalesce may be replaced, while
  queued for a slow client, by a newer entry with the same routing key. The newest
  entry for every routing key is always delivered.

### Backpressure

- Each client has a bounded outbound queue per topic, and no client's slowness
  delays another client or grows server memory beyond that bound.
- When a non-coalescing topic's queue overflows, the client receives a lagging
  notice naming the topic and the first sequence it did not receive. A durable
  topic's forwarding stops until the client subscribes again. A non-durable,
  non-coalescing topic continues from the newest entries.
- Coalescing never reorders the surviving entries of a topic.

### Epochs and Redis loss

- When a topic's epoch in the stream differs from the epoch the client was last
  told, the client receives an epoch-changed notice before any entry of the new
  epoch.
- When Redis loses its data, every connected client receives an epoch-changed
  notice for every subscribed topic.
- If Redis is unreachable when a client connects, the connection is refused with
  an HTTP 503 and a stream-unavailable error. If Redis becomes unreachable during
  a connection, the client receives a stream-unavailable rejection and the
  connection closes, so the client's reconnect-and-resnapshot path is the only
  recovery path.

### Cost

- One live symbol's quotes and M1 bars, delivered to two connected clients, add
  less than 10% of one CPU core to the API process, and stay under 50 ms p95 from
  stream append to frame send.
- Serving the stream does not block or measurably slow the API's REST endpoints.

### Preserved behavior

- No existing REST route, response, or middleware behavior changes.

## Constraints and non-goals

- **No snapshot or history over the WebSocket.** Snapshots and gap fills are REST
  (Q-015), as architecture §4.2 specifies. Serving history over the socket would
  make one connection both a live feed and a bulk download, with competing
  backpressure needs.
- **No authentication or authorization.** The API binds to loopback. Auth, if it
  ever comes, applies to REST and the stream together.
- **No per-message compression.** Arrow IPC for quotes is already dense, and
  permessage-deflate costs CPU on the hottest path. Whether it pays off is
  measured before it is added, not assumed.
- **No server-side filtering by symbol.** A client subscribes to topics. Symbol
  subscriptions are a later protocol extension, once a surface needs more than a
  handful of symbols.
- **No client libraries.** The TypeScript client is part of Q-016. The terminal's
  Rust client is a terminal task.
- **No separate stream process.** The endpoint lives in the API process, which
  architecture §3.1 places there. A separate process is a measured decision for
  later.

## Acceptance criteria

### Agent-verifiable

1. Subscribing to two declared topics and one undeclared topic yields an
   acknowledgement for the two and a rejection naming the third, and entries then
   arrive for the two.
2. Entries appended after an acknowledgement arrive in stream order. Entries
   appended before it do not arrive unless a cursor was given.
3. A resume cursor yields exactly the entries after it. A cursor older than the
   stream's first entry yields an expired-cursor notice.
4. A quotes entry arrives as a binary frame that decodes, under Q-009's framing,
   to the envelope that was published.
5. A client that stops reading while quotes for two symbols are published at high
   rate, then resumes, receives the newest entry for each symbol, and server-side
   queue length never exceeds its bound.
6. A client that stops reading while more durable entries are appended than its
   queue holds receives a lagging notice with the first undelivered sequence, and
   nothing further on that topic until it subscribes again.
7. A second, fast client connected at the same time as the stalled one receives
   every entry, with no added delay beyond the test's tolerance.
8. Flushing Redis during a connection produces an epoch-changed notice for each
   subscribed topic.
9. Connecting while Redis is unreachable yields an HTTP 503 with a
   stream-unavailable error body. Losing Redis mid-connection yields a
   stream-unavailable rejection and a close.
10. A REST health request issued while a client is receiving a full-rate
    synthetic quote stream answers within its usual latency bound.
11. The full validation suite passes.

### Human-verifiable

1. With the market-data publisher streaming one live symbol, two clients connected
   for ten minutes record append-to-send latency and the API process's CPU,
   reported as p50 and p95 latency and mean CPU.
   Command: `uv run python scripts/stream_soak.py --clients 2 --topics quotes,bars.forming --minutes 10 --pid <API process pid>`
2. The endpoint is exercised from a browser's devtools WebSocket inspector, and
   binary quote frames are confirmed to show as binary with a readable header.
   Command: `pnpm dev` in `q_frontend`, then `new WebSocket("ws://127.0.0.1:8000/api/v1/stream")` in the console
