# Q-013: Live market data publisher

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.1, §4.4, §4.6, §9](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#46-visualization-data-path)  
**Depends on:** Q-011  
**Implementation plan:** [`../plans/Q-013-live-market-data-publisher-plan.md`](../plans/Q-013-live-market-data-publisher-plan.md)

## Purpose

The terminal's first vertical slice is one live symbol with forming bars, fed
only by the stream. Nothing puts market data on the stream today. The research
UI polls REST market-data endpoints every two to three seconds. On Linux, the
only live source is the Wine data gateway, which serves tick and bar ranges but
has no push and no "latest quote" call. The execution edge's quote endpoint is
specified but not implemented. This task adds one publisher process that turns
the gateway into the three market-data topics — quotes, forming bars, and
completed bars — as columnar Arrow payloads, for a configured set of symbols. It
exists so that the slice after the terminal skeleton is blocked only by the
terminal, not by the absence of data to render.

## Requirements

### What is published

- For each configured symbol, every tick the gateway reports is published on the
  quotes topic exactly once and in tick order, including ticks that share a
  millisecond timestamp.
- For each configured symbol and timeframe, the forming bar is published whenever
  any of its values change, and not otherwise.
- When a bar closes, it is published once on the completed-bars topic with the
  values the gateway reports for it once it is no longer forming, before the next
  forming bar for that symbol and timeframe is published.
- Every entry carries the routing values its topic requires, and its payload
  conforms to the Arrow schema the topic policy names, with timestamps under the
  lake's existing naive Brasília wall-clock convention.

### Source and semantics

- All market data comes from the data gateway's existing contract. No Linux-side
  code reads the terminal directly, and no process on this path imports the
  MetaTrader module.
- Bars are the terminal's own bars, as the gateway reports them. The publisher
  does not aggregate ticks into bars, because bar aggregation is a shared
  semantic that belongs in the Rust core and must not gain a Python version.
- Market data moves through the publisher as columns. No per-tick or per-bar
  object is created on the publishing path.

### Freshness and cost

- While the gateway is healthy and the market is trading, a tick is published
  within one second of its tick timestamp at the 95th percentile.
- The publisher's request rate to the gateway is bounded by configuration, and
  the gateway's existing disk cache is not written on the publishing path.

### Degraded behavior

- While the gateway is unreachable or reports the terminal disconnected, nothing
  is published, the publisher keeps running and retries with bounded backoff,
  and the condition is logged once per transition rather than once per attempt.
- After an outage, publishing resumes from the current market. Missed ticks are
  not backfilled onto the stream: ephemeral topics are not a history, and REST
  market-data endpoints already serve the past.
- When there are no new ticks, as outside trading hours, nothing is published and
  the gateway is still polled at the configured rate.

### Operation

- Symbols, timeframes, and poll rates are configured through the backend's
  existing settings mechanism, and the publisher refuses to start with an empty
  symbol set.
- The publisher runs as its own long-lived command, separate from the API, the
  relay, and the workers, and shuts down cleanly on a termination signal.

## Constraints and non-goals

- **No bar aggregation from ticks.** It is the obvious way to get sub-second
  forming bars, and it is a second implementation of a `q_core` semantic
  (invariant 1). Forming bars update at the bar poll rate until `q_core` has
  aggregation.
- **No execution-edge quotes.** `/v1/quote` exists only as a contract until
  phase 4. Building the publisher on it would block this task on the execution
  edge.
- **No change to the Wine gateway.** A push or long-poll endpoint in the gateway
  would be better, but it is a wire contract change under `q_contracts/edge`,
  and this task must work with the gateway that runs today.
- **No WebSocket delivery, no latest-value REST endpoint.** Those are Q-014 and
  Q-015. This task is proven by reading Redis.
- **No dynamic symbol subscription.** The set is fixed at start. Choosing symbols
  from the terminal is a later surface.
- **No change to the research UI's market-data polling.** It moves off polling in
  a later task, if at all. Q-016 covers job progress only.
- **No systemd unit.** That is Batch 03.

## Acceptance criteria

### Agent-verifiable

1. Against a fake gateway replaying a recorded tick sequence with repeated
   millisecond timestamps, across poll boundaries, every tick is published
   exactly once and in order.
2. Against a fake gateway whose newest bar changes, stays the same, and then rolls
   to a new bar, forming entries are published only on change, and exactly one
   completed entry precedes the first forming entry of the new bar.
3. Quote and bar payloads decode as Arrow IPC to batches whose schemas equal the
   contract's tick and bar Arrow schemas, and every entry carries the required
   routing values.
4. No module imported by the publisher imports the MetaTrader package, verified by
   importing the publisher in a fresh interpreter and inspecting loaded modules.
5. The publishing path creates no per-row tick or bar objects, verified by a test
   that fails if the row-mapping helpers are called.
6. The tick disk cache is not written during publishing.
7. With the fake gateway down, the publisher publishes nothing, keeps running,
   logs one transition, and resumes publishing current data when it returns.
8. The publisher exits non-zero with a clear message when no symbols are
   configured.
9. The full validation suite passes.

### Human-verifiable

1. During a B3 trading session, with the Wine gateway and terminal running, the
   publisher streams one liquid futures symbol for ten minutes. The tick
   timestamp to stream append latency is reported as p50, p95, and maximum, and
   the published tick count is compared with the gateway's tick range for the
   same window.
   Command: `uv run q-market-publisher --symbols WIN\$N --timeframes M1 & uv run python scripts/measure_quote_latency.py --symbol WIN\$N --minutes 10`
2. Forming M1 bars on the stream are compared by eye with the MT5 terminal's own
   chart for the same symbol over five bar closes.
   Command: `redis-cli -p 6380 XREVRANGE q:stream:bars.forming + - COUNT 5`
3. The gateway's CPU use under the publisher's default poll rate is observed and
   reported, so that the rate is confirmed not to degrade the terminal.
   Command: `top -p $(pgrep -f mt5_gateway.py)`
