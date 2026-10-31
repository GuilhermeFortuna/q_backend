# Q-145 — Market replay and simulated broker for the live stack

**Status:** implementation in progress; review follow-ups, full-stack and human terminal verification pending.
The Q project board is the status of record.
**Batch:** 42 — Market replay.
**Owner:** q_backend. **Repositories:** q_backend, q_contracts, q (launcher), q_terminal (demo deployment path).
**Depends on:** Q-144 (released incremental execution primitives), Q-136 and Q-137 (both Done).
**Plan:** [Implementation plan](../plans/Q-145-market-replay-and-simulated-broker-plan.md).

## Intent and success

Deploy a supported strategy from q_terminal, watch it trade a historical market session through the real
live-stack publisher, execution worker, control API, ledger, outbox and streams, and inspect orders,
positions and reconciliation in the normal terminal. The market continues moving while the strategy computes,
the worker submits, the broker accepts and executes, and confirmation travels back. Configured latency changes
the market available at execution and the time the stack learns the result.

Replay is an external market/broker environment. Its controls do not belong in q_terminal or the live control
API, and the strategy/evaluator has no replay branches. The simulator runs as its own process, with separate
listeners for the existing gateway and execution-edge protocols and an external administrative API/CLI.

### Approaches and proposed choice

1. An in-process replay provider is economical but misses HTTP boundary behavior and encourages special
   execution paths. It is insufficient for the requested live-stack test environment.
2. A standalone gateway/edge simulator in q_backend reuses current clients, exercises the operational stack,
   and keeps lifecycle and controls independent. **Recommended design.** Shared numerical rules live in q_core.
3. A new repository/service product adds release and deployment overhead without a current independent
   consumer. The simulator package must have clear boundaries so it can move later if needed.

The user selected an external simulated market/broker with independent controls and realistic latency.
The concrete API, clock model, first-release scope and task decomposition below are proposed engineering
decisions for review, not previously approved implementation details.

## Existing system and required seams

- `streaming/market/publisher.py` polls `RemoteMt5Client` with its own clock. `BarCoordinator` separately
  polls historical bars for execution. Replacing only a stream producer would miss the execution data path.
- `cli/q_execution.py` constructs `_LiveClock`, `MarketDataService`, `EdgeClient`, `EdgeQuoteSource`,
  `EdgeBroker`, `PaperBroker`, the coordinator and execution service. These are useful composition boundaries.
- `ExecutionWorker.clock` currently feeds market decisions and worker heartbeats/leases. They must be
  separated before historical/pause/speed clocks can be safe and consistent.
- `api/services/execution.py` uses wall time for lease health and activation-related operations. Deployment
  cutoffs, warmup, reconciliation windows, day-trade/session rules and quote/bar freshness need an explicit
  time-domain audit. Replacing only the worker clock is insufficient.
- The paper broker refuses pending orders and broker-held protections. Use the normal `mt5_live` edge route
  to the simulator's demo account; do not extend paper into a second replay broker.
- q_terminal currently creates paper deployments and has live command restrictions. It needs a general,
  capability-driven demo deployment path, preserving its normal live activation gates. No replay controls.
- Existing `stream_replay` endpoints replay transport history; they are unrelated to market simulation.

## Service boundaries and contracts

New package: `src/q_backend/replay/`; new command: `q-replay`. It does not import the execution worker,
strategy implementation, ledger models or live API services. It can reuse market storage readers, contracted
wire codecs and q_core. No production runtime imports this package to make trading decisions.

The service exposes:

| Listener | Default loopback port | Interface |
| --- | --- | --- |
| Market source | 18912 | Existing gateway `/v1/health`, symbol/search metadata, available range, OHLCV/recent, ticks/page and trades |
| Broker edge | 18913 | Existing edge health, account, quote, check, submit, lookup, orders, positions and deals |
| Administrative | 18914 | `/replay/v1/runs`, per-run status, commands and journal |

No live gateway or execution-edge endpoint exposes create/pause/step/seek/speed commands. Simulator transport
identifies its implementation honestly in permitted metadata; account mode is `demo` with a dedicated numeric
login. It never proxies, imports MetaTrader5 or falls back to a real gateway or broker.

New cross-process contracts originate in q_contracts: `schema/api/replay/` for run configuration, state,
commands and journal metadata; `schema/edge/clock/` for read-only market-clock snapshots; additive control API
capability/configuration fields needed for selecting an available demo account. Update OpenAPI, examples,
version records, generator/export tooling and consumer vendoring. Keep existing data/edge operations wire
compatible and honor their pagination, NPZ columns, duplicate occurrence identities and intent obligations.
No uncontracted replay flag travels on trading requests, strategy configs or execution events.

## Market data and causal visibility

One run contains one or more explicitly selected symbols and a UTC `[start, end)` interval. Inputs are local,
immutable tick files selected through an explicit manifest. The manifest records each file checksum, source,
symbol metadata, time units, exchange timezone and known coverage/gaps. Importing an existing research
TickStore is an explicit preparation operation: freeze/checksum its selected files, never silently sync or
mutate them while replaying. Warmup files/range must be supplied and validated before starting.

- Keep tick order stable using `(time_msc, source occurrence)`; merge symbols by timestamp then manifest
  symbol order then occurrence. Never deduplicate equal records. Reject out-of-order input unless preparation
  explicitly performs a stable sort and records the new artifact identity.
- One canonical broker timeline advances continuously. Gateway visibility may lag it by `market_data_ms`.
  Quotes, tick pages, trades, available range and historical/forming bars all respect their listener's
  visibility watermark. A future query cannot reveal a final candle or a future tick. Broker quote reads have
  their own response delay but never reveal future canonical state.
- Derive forming bars from observed ticks using existing shared aggregation semantics. Include only valid
  trade records in trade OHLCV; quote-only records still update bid/ask. Preserve instrument volume units and
  timezone conventions; do not synthesize trades from missing prices or repair gaps with future information.
- Completed bars become available at their market-time boundary, including the final session bar when the
  clock crosses its close. Reuse a bounded warmup prefix without publishing it as newly occurring ticks.
- Explicitly missing bid/ask, metadata or warmup fails preparation for a requested execution profile. Known
  gaps are surfaced; stale quotes cannot produce fills. No order-book data or real liquidity is inferred.
- Caches and persistent trade snapshots are scoped by run generation and effective data source. Never serve
  current live/Research data or a previous run's final candle from cache. Disable unsuitable caches in the
  composition configuration, or include source/generation in their keys.

## Time, speed, pause and consumer behavior

Retain historical event timestamps; do not rewrite a past session onto today's calendar. Market time is UTC
with explicit exchange-local conversion. A read-only contracted `GET /v1/clock` returns current market time,
rate, running/paused state and generation, together with a snapshot sequence. A generic `MarketClock` client
is configured in the API, publisher and execution composition; ordinary live operation defaults to UTC wall
time. This is an infrastructure time source, not a replay API dependency in strategy/business logic.

Market time drives session/day-trade boundaries, forming/completed bars, activation cutoffs, decision age,
quote freshness, order eligibility/expiry and broker lookup windows. Operational UTC/monotonic time drives
worker leases, health freshness, HTTP deadlines, watchdogs, process lifecycle, stream retention and retries.
Keep operational audit timestamps and market event timestamps distinct in storage and diagnostics. The same
`now` value must not serve both domains. An unavailable/stale clock fails market actions closed; it cannot
silently switch the market back to today's date. Test real-clock default behavior unchanged.

- Interactive default is 1x; positive finite speeds from 0.1x through 10x are supported. Market time advances
  from a monotonic anchor, not from request count. Changing rate preserves current time and event order.
- There is **no consumer acknowledgment barrier**: the market never waits for evaluation, broker HTTP
  handlers, stream delivery or chart rendering. At high speed the stack may fall behind; normal stale-data and
  risk behavior applies, with lag reported externally. Acceleration is a stress tool, not a parity guarantee.
- Pause freezes market time and pending market-time events, while health/leases/timeouts continue normally.
  An already scheduled request may exceed its real HTTP timeout while paused; its intent remains unresolved
  until the run advances and lookup confirms the result. Do not fabricate success to make stepping convenient.
- `step` is accepted only while paused and advances by a positive `market_ms` duration, processing every due
  market/broker/delivery event. It does not wait for the live stack to process the step. Commands received
  afterward are scheduled at the new cursor and cannot be backdated. Rejected/paused requests have explicit
  state; the controller reports outstanding work so operators understand step granularity.
- No backward seek or in-place reset. A fresh run uses a new generation and fresh isolated operational state.
  Simulator restart recovers a run paused; it never jumps forward by downtime or silently resets the broker.

## Latency and continuously moving market

All configured durations are nonnegative finite integer milliseconds in **market time**. The minimum first
release supports fixed values, all defaulting to zero: `market_data_ms`, `order_transport_ms`,
`broker_accept_ms`, `broker_execution_ms`, `broker_response_ms`. Do not add an artificial strategy delay: the
real evaluator's computation, worker polling, request serialization and local transit already consume time.
The configured transport component is additional modeled delay after simulator request ingress.

For request ingress at market time `t0`:

- broker arrival = `t0 + order_transport_ms`;
- acceptance/check completion = arrival + `broker_accept_ms`;
- a market order becomes executable = acceptance + `broker_execution_ms`;
- confirmation becomes visible no earlier than outcome time + `broker_response_ms`.

Capture ingress time before any await/work and enqueue events under one serialized owner. HTTP handlers can
await responses, but cannot hold the scheduler lock or sleep on its thread. A waiting submit, a slow history
query and a disconnected client must not stop market advancement. Client timeout does not cancel a broker
event; lookup can later discover a fill. Response-delay rules apply consistently to confirmation reads so a
lookup cannot reveal a fill before its modeled visibility time. Broker matching uses canonical state even
when the client is still seeing an old quote.

For account/order/position/deal/lookup reads, capture a snapshot at request ingress of broker state whose
visibility time is <= that ingress cursor, and deliver that immutable snapshot after `broker_response_ms`.
An old request never includes a later fill. Effects become visible at outcome time + response delay; reads
can add their own return-trip delay, intentionally conservatively. Submit returns its outcome only at its
visibility time. `/check` takes transport + acceptance + response delay and remains advisory, never reserving
margin or guaranteeing a later fill. Quote reads snapshot canonical prices at ingress and deliver after the
response delay. No account or history endpoint bypasses visibility by reading raw canonical broker tables.

**Acceptance example:** last ask is 100 at ingress 10:00:00.000; asks become 101 at +100 ms and 103 at +200 ms.
Transport=50, acceptance=50, execution=150 gives execution at +250 ms and buy fill 103 (zero slippage), not 100.
Response=100 makes confirmation visible at +350 ms. The market has advanced during all four stages. A limit
buy at 101 cannot pay 103. With 1x/2x speed, the configured market-time delays stay identical; wall duration
changes. Actual request ingress may change between independent live-stack runs.

Pending entry placement becomes effective at acceptance; an eligible stop/limit schedules execution after
`broker_execution_ms`. Stops latch into market orders on trigger. Limits recheck their bound at execution;
if no longer executable they remain working. Trigger scheduling must not enqueue duplicate executions. Stop
and target protections included with an entry activate atomically with its fill, and can react only to that
event's post-fill state or later events, never earlier ticks. Amend/remove commands take effect after transport
and acceptance delay. A triggered stop is already committed as a market order and cannot be cancelled,
amended or expired afterward, even before its delayed execution. A triggered limit remains cancellable and
expirable until filled; cancellation invalidates its scheduled execution. Removing/replacing protection
cannot retract a stop already committed to execution. Broker-held protection
remains active while the execution worker is blocked or disconnected. Q's existing worker owns OCO sibling
cancellation; the simulator must allow both legs to fill before a delayed cancellation, exercising the existing
double-fill guard. It must not turn client-managed OCO into a fictitious atomic broker guarantee.

Event ordering is deterministic: `(market_time_us, priority, sequence)`. At equal timestamps process expiry
first, then recorded market events (in stable source order, including triggers for existing orders), then
execution events, then broker command/acceptance events (ingress sequence order), then response-visible events.
Thus expiry is exclusive; a cancellation tied with an execution loses. A cancellation tied with a stop trigger
also loses because that stop is committed. A cancellable limit triggered at t with execution at t+150 ms can
be cancelled at t+50 ms. Zero-delay executions created by a command run immediately after that command before
the next command at the same timestamp. A new command never trades against an earlier unseen tick. Resting activation
can evaluate the current quote; rejected/expired orders never create a fill.

## Broker model and durability

Use Q-144's explicit `quote` execution model: buy market/stop at ask, sell at bid, adverse configured slippage;
limits may improve and never violate their bound. Protect a long with sell-side stop/limit rules and a short
with buy-side rules. Existing trade-based research assumptions differ (including strict-through targets);
document the difference rather than claiming replay must exactly equal every backtest.

First release: full fills at available top-of-book quotes, fixed fees/slippage, validated tick/volume steps,
instrument contract value, cash/equity/positions/deals, market and pending entry orders, attached protections,
amendments, cancellation and expiry. Reject unsupported instrument/account modes explicitly. Support the
single-account, per-symbol netting model used by the live execution route; reductions and reversals must
produce correct deals and remaining cost basis. Refuse a second active deployment for the same account/symbol
in this profile until independent ownership is supported. No partial fills, queue position, depth/impact,
stochastic jitter, exchange margin/stop-out emulation or matching an undocumented broker's every rule.

Provide a configured initial cash balance and simple deterministic insufficient-funds rejection based on
the declared instrument margin-per-contract, with released margin on close; mark the simplified model in
the run manifest. Native fill/cost rules stay in q_core; Python owns scheduling and broker state only.

Use the already contracted deal `commission`/`fee` fields with MT5's signed cash-flow convention: charges
are negative, rebates positive; simulated swap is zero. Map `FillRecord.fee = -(commission + fee)` through the
generic EdgeBroker deal mapping for immediate, reconciled, pending and broker-protective fills. Missing fields
default to zero; do not clamp rebates or count a charge twice. Terminal/ledger fees must agree with simulator
cash. Slippage is reflected in the actual fill price; retain zero for a separate slippage statistic when the
broker protocol supplies no reliable reference price, rather than fabricating an attribution.

The simulator uses a per-run SQLite journal/state store outside the Q ledger, with one transactional owner.
Persist ingress, scheduled events, intent identity/payload hash, accepted/rejected outcomes, broker tickets,
fills, cancellations, response visibility and cursor progress atomically at event boundaries. Never acknowledge
an effect before durable commit. Keep the edge's duplicate-intent refusal and never resubmit; simulator
restart cannot create a duplicate fill. On lookup, an in-flight request is indeterminate, not definitive
`not_found`. A confirmed negative lookup is legal only after pending work has been excluded. Reconstruct paused
state after a crash; replay a recorded ingress journal to verify deterministic broker results.

## External controller and isolated stack lifecycle

`q-replay` provides `prepare`, `create`, `status`, `pause`, `resume`, `speed`, `step`, `stop` and `journal`
commands against the administrative listener. CLI is the initial external control surface; no web controller
or new research UI is required for this task; Q-146 adds a Research UI page on top of it, and q_terminal and
the live control API stay free of replay controls. Create config includes manifest, interval/warmup, account/instrument settings,
latencies, rate and resource limits. After creation, latency/account/data changes require a new run. Commands
carry a command UUID and expected state revision, giving idempotent results and conflict on stale revisions.
States: prepared → paused ↔ running → completed/stopped, or failed. Only one active run per simulator instance.
Status exposes canonical and visible cursors, generation, revision, source coverage, queue depth, lag and
outstanding intents. Journals/export include code/contract/core revisions, config hash and dataset checksums.

Proposed workspace entry points: `./dev replay --config <path>`, `./dev down replay`,
`./dev logs replay:<service>`, `./dev restart replay:<service>` and replay entries in `./dev status`/shell.
The launcher starts dependencies and simulator paused, configures normal live-stack services against its
listeners and generic clock, waits for readiness, then opens q_terminal. Resume is an external explicit
command after deployment setup. Services persist when the UI closes; restarting the simulator requires
external resume after recovery. `down all` includes replay; `down live`/`down research` do not stop it.

Use a separate `q-replay` Compose project, Postgres 5436, Redis 6382, control API 8002, dedicated systemd
instances/environment files and terminal workspace/config root. Defaults must be configurable and collisions
refused. Do not overwrite `backend.env`, live/research runtime config, live terminal workspace or MT5 units.
Avoid starting Wine, real gateways, Research workers or GPU machinery. The simulator cannot connect to real
brokers, and replay composition refuses mixed gateway/edge/clock generations or endpoints outside its configured
instance. Stopping replay leaves live and Research intact. Configure remote data-source selection explicitly;
never use `auto` fallback. Isolate lake/history/trade caches, logs, ledger and kill switch by instance/run.

Fresh run means fresh operational state: the launcher stops only the replay instance, provisions new run-scoped
database/schema, Redis namespace and data/cache roots, rebinds services, and then reopens the terminal. Old runs
and journals remain inspectable; no automatic destructive reset. Refuse run replacement while replay services
still refer to the previous generation. Stop cancels unexecuted simulator work with journaled terminal state;
it does not invent flatten fills. At `end`, no execution or new submission is allowed, including an execution
scheduled exactly at that exclusive bound. Journal cancellation of untriggered pending orders and rejection
of accepted-but-unexecuted requests as end-of-data outcomes using existing edge outcome shapes/reasons.
Retain all prior fills/open positions for inspection and publish valid bar boundary closures. Drain only
scheduled response-visibility events through `end + broker_response_ms`; never execute against a quote carried
past the dataset's end. Completed runs retain read-only account/journal access; post-completion reads return
their frozen visible state without requiring a future cursor advance. Explicit stop uses the same cutoff
at the current cursor. No automatic flatten price is invented.

The terminal selects/deploys an existing supported strategy version to an available **demo** account through
normal execution capability contracts and activation flow. Preserve normal allowlisting and deployment gates;
do not globally enable real-account trading. General demo UI support is allowed; no replay-specific broker
mode, speed/pause/step controls, special fill events, strategy editor or result browser in q_terminal.

## Reproducibility and limits

Same frozen dataset, configuration and recorded ingress/event journal produce identical broker outcomes and
ordering; exclude operational wall timestamps and generated transport envelope IDs from that comparison.
Two independent interactive runs need not produce identical orders: real CPU/network scheduling changes
ingress, particularly under acceleration. Preserve and report that measurement, rather than freezing the
market behind a worker acknowledgment. Broker response delay can legitimately trigger the live stack's
unknown-intent/reconciliation path. A deterministic whole-stack virtual scheduler is outside this release.

## Acceptance criteria

1. External create/prepare/resume launches an isolated historical session; q_terminal deploys a public fixture
   strategy through the normal demo edge path and shows evolving quotes, forming/completed bars, orders,
   positions, deals, fees and ledger/outbox events. No MT5/Wine/GPU or private strategy is required.
2. All gateway history/snapshot endpoints, data-source caches and multi-symbol warmup stay causal; duplicate
   timestamps, session boundaries, no-trade periods and historical timezone offsets retain correct ordering.
3. The +250/+350 ms latency example above passes with virtual time. An HTTP test proves quotes keep advancing
   during a pending submit and that its eventual fill uses execution-time pricing. A slow history call also
   cannot block the scheduler. Market-data delay never leaks via a history or lookup shortcut.
4. Pending orders, stops/targets, delayed stop execution, limits that cease to be executable, expiry,
   cancel/amend races, net reductions/reversals and client-managed OCO races follow the specified model.
5. Lost/late response, timeout and restart after a durable fill reconcile to one fill without resubmission.
   Crash before commit has no acknowledged effect; startup is paused and intent lookup remains truthful.
6. Pause/resume/step/rate changes preserve event order and market time while leases/watchdogs/health use real
   time. Historical activation, recovery/session close and stale quote/bar checks use the correct domain.
7. New generations cannot reuse old cache/ledger/trade state. Starting/stopping/restarting replay cannot alter
   live/Research units, config, data, UI preferences or shared MT5 services. Mixed endpoint generations fail.
8. Replaying a recorded journal reproduces broker outcomes; independent wall-time runs are reported honestly.
   Invalid manifests/config, resource limits and absent capability fail before trading starts.
9. Normal live and Research defaults remain unchanged. Contract checks and focused regression suites pass;
   external controller and architecture/model limits are documented with reproducible fixture commands.

## Verification scope and exclusions

Use fake monotonic time and small fixture ticks for scheduler/matcher tests; loopback HTTP with real clients
for boundary tests; targeted execution/API tests for clock and demo changes; mocked launcher lifecycle tests.
One focused opt-in PostgreSQL/Redis integration scenario is justified to verify the real ledger/outbox route.
Document one human terminal session inspection as the task's end-to-end UI criterion; no broad benchmark,
GPU, Wine or full repository CI is required during implementation. Full CI runs before push normally.
No order-book reconstruction, random latency model, live recording daemon, chart control panel, strategy
optimization, full-exchange emulator, backward seek, strategy porting or private strategy publication.

## Delivery and review

Specs/plans live in their owner repositories; launcher and terminal changes are made on the same task branch
in every affected repository after approval. Q-144 must first be released. Launch Q-145 through `./work start`
only when Todo; never mark it Todo as an agent. No implementation is authorized by this planning document.
