# Q-145 — Market replay and simulated broker implementation plan

> **For implementation agents:** Read the linked specification and each affected repository's instructions.
> After human plan approval and Q-144's release, launch `./work start Q-145 --agent <agent> --worktree` from q.
> Use superpowers:executing-plans task by task. This document authorizes no implementation on its own.

**Goal:** run supported strategies through the live stack against an independently controlled historical market
and simulated demo broker, including continuous market movement during configurable broker latency.
**Architecture:** one external simulator process owns the market timeline, gateway/edge listeners, broker
state and durable journal. Normal stack components use those boundaries and generic market-clock injection;
the external CLI owns replay controls. Numerical execution primitives come from Q-144's released q_core API.
**Tech stack:** Python, existing FastAPI/httpx/NumPy/Arrow/Parquet, stdlib SQLite, q_core; current Qt terminal,
systemd and Compose launcher infrastructure.
**Spec:** [Q-145 specification](../specs/Q-145-market-replay-and-simulated-broker-spec.md).
**Status:** proposed; awaiting human plan approval.

## Status (implementation session)

Done and committed: step 1 (contracts, except the COMPAT row); step 2 core (config, dataset, store,
timeline, controller, admin API, runtime, `q-replay`); step 3 causal gateway; step 4 broker and edge;
step 5 clock domains and composition; step 6 backend demo capability; step 7 launcher lifecycle; step 8
edge stack and reproducibility tests and the operations document.

Completed since: TickStore import (read-only, UTC conversion, checksums); scheduler isolation proof
(a stuck history query does not stall the market); single active mt5_live owner per account and symbol
(opt-in setting, catalog and identity sources); PostgreSQL edge scenario (tests/replay/test_stack_integration.py,
run against an isolated postgres:16).

Open: a full live-stack run (API, Redis, outbox and execution worker deciding on bars served by the replay
gateway and filling through the replay edge). The configuration hooks exist (Q_MT5_EDGE_URL, Q_MT5_GATEWAY_URL,
runtime data_source=remote, Q_MARKET_CLOCK_*), but this path has not been executed. Also open: the saved-run
ownership source, and the human terminal inspection, which needs a person at the UI.

## Global constraints

- Q-144 must be Done with a pushed date tag before the backend pins the capability. No local native override.
- `./work start` prepares q_backend. Create the same printed task branch from local `development` in
  q_contracts, q and q_terminal under `.worktrees/`; do not switch normal checkouts or an inspection session.
- Read q_terminal's binding `BOUNDARY.md`; add only ordinary demo deployment support, never replay controls.
- New contracts originate in q_contracts; commit them before updating `CONTRACTS_REV` and vendoring via
  `make contracts CONTRACTS_REPO=<contracts-worktree>`. Never hand-edit generated outputs.
- Use environments/targets/wheels on `/mnt/build`; create Rust target links before compiling. Tests use public
  fixtures; do not read or publish q_strategies modules. No Wine/MT5/GPU needed.
- The market never waits for the stack. Same recorded ingress journal is deterministic; independent interactive
  runs are not promised identical results. Do not introduce a hidden lockstep barrier.
- Run focused checks per step. Full CI remains the pre-push gate; agents do not push, merge or mark Todo/Done.

## Review focus

- All history/trade/cache paths must obey delayed visibility, including available range and warmup.
- Unknown-intent lookup must not close an order while delayed simulator work can still fill it.
- Operational leases and activation/session market time must not share one historical `now`.
- Pause/end-of-data and crashed HTTP clients must not deadlock event execution or invent fills.
- Multi-symbol/config-generation isolation must include terminal settings, shared MT5 ownership and caches.

## Ordered implementation

### 1. Contract and integration seams

**Files (q_contracts):** new `schema/api/replay/{run-config,run-state,command,journal}.schema.json`,
`schema/edge/clock/snapshot.schema.json`; modify `schema/api/openapi.yaml`, `schema/edge/` protocol descriptions,
existing execution deal examples (signed `commission`/`fee` already exist), `COMPAT.md`, `tools/generate.py`, emitters, examples
and generated output. Add `tests/test_replay_contracts.py`.
**Interfaces:** administrative create/status/commands; generic `GET /v1/clock`; demo execution capabilities.
Run configuration/state fields, durations, state transitions and command revision semantics are those in the spec.

- [x] Write schema fixtures for valid configs, negative/noninteger latency, nonfinite/out-of-range speed,
  duplicate symbols, malformed manifests, invalid transitions, old-client defaults and mismatched generations.
- [x] Define read-only clock snapshots and administrative routes separately from the existing gateway/edge
  operation surfaces. Add a generic execution capability response identifying the available demo account and
  allowed creation/activation operations; real-account paths retain their existing restrictions.
- [x] Run `uv run python tools/validate.py`, `uv run pytest tests/test_replay_contracts.py` and format/lint
  for touched generator code. Regenerate using documented commands and commit the source/generated changes.
- [x] Pin/vendor the resulting local contract commit in q_backend and q_terminal and run their
  `make contracts-check CONTRACTS_REPO=<contracts-worktree>`. Pin Q-144's release in backend and update uv.lock.
  (q_backend done; q_terminal pending step 6.)

### 2. Durable timeline, immutable input and external controller

**Files (q_backend, new):** `src/q_backend/replay/{config,dataset,timeline,journal,controller,admin}.py`,
`src/q_backend/cli/q_replay.py`, `tests/replay/test_{dataset,timeline,journal,controller}.py`, fixture ticks under
`tests/replay/fixtures/`; modify `pyproject.toml` for `q-replay`.
**Interfaces:** `ReplayTimeline.now_us() -> int`, `schedule(at_us: int, priority: int, event: ScheduledEvent)
-> int`, `advance_to(target_us: int) -> None`; `ScheduledEvent` is a serializable tagged event with durable
sequence, run generation and payload. `ReplayRunStore` atomically commits event/state/cursor changes and
recovers paused. Controller exposes `create`, `pause`, `resume`, `set_speed`, `step`, `stop`, `status` and
`journal` with command UUID/expected revision; signatures mirror the generated contract models.

- [x] Write failing tests for source checksum mutation, warmup omission, stable duplicate/multi-symbol order,
  exclusive expiry tie priority, zero-delay scheduling, speed anchors, paused step, stale command revision,
  command UUID replay, queue/resource limits and crash before/after event commit.
- [x] Run `uv run pytest tests/replay/test_dataset.py tests/replay/test_timeline.py tests/replay/test_journal.py
  tests/replay/test_controller.py -q`; confirm missing implementations fail, then implement and pass them.
- [x] Keep one state owner, bounded input pages and transactional cursor advancement. Read-only HTTP snapshots
  use immutable/copy snapshots; disk reads/encoding and response waits execute outside the scheduler owner.
- [ ] Add manifest preparation from explicitly selected TickStore files without syncing; emit metadata and
  contract/core/config revisions. Implement CLI/admin endpoints and authenticated loopback binding. Commit.

### 3. Causal market gateway

**Files:** new `src/q_backend/replay/{market,gateway}.py`, `tests/replay/test_gateway.py`; reuse existing
columnar/aggregation helpers from market_data and q_core without importing live service lifecycle.
**Interfaces:** `ReplayMarket.snapshot(at_us: int) -> MarketSnapshot`; gateway routes implement the existing
`RemoteMt5Client` contract, clamping requested history to the visibility cursor. The clock route reads the
canonical cursor; data visibility follows its independently configured delay.

- [x] Write failing tests using actual `RemoteMt5Client` over loopback HTTP for ticks/pages/trades, exact
  duplicates, metadata/search, warmup, future range clamps, forming OHLCV, final completed bar and DST history.
  Include a delayed-data request attempting to reveal future state through every history endpoint.
- [x] Implement source preparation/aggregation and route adapters, preserving current NPZ dtypes, timezones,
  flags, pagination limits and coverage facts. Ensure arbitrary timeframes requested by supported strategies
  get causal bars, including completed coarser context.
- [ ] Run `uv run pytest tests/replay/test_gateway.py tests/streaming/test_market_publisher.py -q`; prove a
  slow gateway response cannot stall timeline advancement. Commit.

### 4. Broker state, latency, edge routes and outcome visibility

**Files:** new `src/q_backend/replay/{broker,latency,edge}.py`, `tests/replay/test_{broker,latency,edge}.py`;
modify generic `execution/brokers/edge.py` and reconciliation fee mapping where the new deal fields require it.
**Interfaces:** `ReplayBroker.receive(intent_id: UUID, payload: ExecutionOrder, received_us: int) -> None`
persists once and schedules broker arrival/acceptance/execution/visibility. `lookup` returns generated existing
edge outcomes, including indeterminate until queued effects/visibility permit a conclusive answer.
Numerical calls consume `q_core.execution` from Q-144. Account/deal/order snapshots obey their documented
response delay and the existing edge schemas. No broker action calls the execution worker.

- [x] Write failing fixture tests for the exact +250 ms fill=103/+350 ms visibility example; zero latency;
  quote gaps/staleness; finite costs; insufficient funds; long/short protection; triggered stop that later
  retreats; a limit no longer executable at its due time; expiry and cancel/amend ties; same-time duplicate
  ticks; net reductions/reversals; both OCO legs filling before a delayed worker cancellation; cancellation
  of a triggered limit versus refusal for a committed stop; exclusive end cutoff and bounded response drain.
- [x] Write timeout/crash tests: disconnected submit client, repeated intent with conflicting payload,
  restart after durable execution before response, and pending lookup must never answer definitive not-found.
- [x] Implement matching orchestration/state/fees/margin and HTTP routes; await response futures outside the
  scheduler. Actual handler arrival is an input, not a timestamp derived from the strategy signal.
- [x] Exercise actual `EdgeClient` and `EdgeBroker` with a fake clock plus a short real HTTP scheduling test
  proving the market advances while submit waits. Map broker fees through the normal ledger fill path,
  including recovered and protective exits. Assert `fee = -(commission + fee)` from signed deal cash flows,
  zero for missing fields and correct rebates. Test immutable ingress snapshots on all read routes, including
  a request started before a fill. No simulator-only execution result types.
- [x] Run `uv run pytest tests/replay/test_broker.py tests/replay/test_latency.py tests/replay/test_edge.py
  tests/execution/test_edge_broker.py tests/execution/test_edge_protective_broker.py
  tests/execution/test_reconciliation.py -q`; commit after the focused tests pass.

### 5. Generic market clocks and source isolation in ordinary stack composition

**Files:** new `src/q_backend/time/{__init__,clocks}.py`, `tests/execution/test_clock_domains.py`;
modify `cli/q_execution.py`, `cli/q_market_publisher.py`, `execution/{worker,bar_coordinator,service,recovery,
reconciliation,quote_source}.py`, `api/services/execution.py`, API dependency wiring,
`storage/db/execution_repositories.py`, `storage/settings.py`, `market_data/service.py`,
`streaming/market/{publisher,trade_history,trades}.py` and cache key/storage paths as needed.
**Interfaces:** `MarketClock.now() -> datetime` (aware UTC), `SystemMarketClock`,
`RemoteMarketClock(url, expected_generation, operational_clock)` and a separate `OperationalClock` with
UTC `now()` and monotonic elapsed time. Configuration uses explicit source URLs/generation/cache roots.
No runtime switch inside the evaluator; all historical timestamps remain historical.

- [x] Write failing tests for historical deployment activation and warmup, market-age rejection, recovery
  lookup windows, session flatten, delayed/coarser bars, paused healthy leases, stale/unreachable clock failure,
  speed changes and run-generation cache isolation. Verify real-clock defaults against current behavior.
- [x] Audit each `now` used by worker/service/API/recovery/lease code and assign its domain. Keep DB-owned
  timestamps and worker lease/heartbeat health on operational UTC; pass market time explicitly for market
  semantics. Test both domains together rather than replacing every datetime globally.
- [x] Wire one market-clock provider into every market-facing process; validate clock freshness with real
  elapsed time and reject cross-generation sources. Disable cache paths that cannot carry causal source
  identity. Restrict same account/symbol deployment ownership for this supported execution profile.
- [x] Run the new clock tests, `tests/execution/test_worker_heartbeat.py`, `test_bar_coordinator.py`,
  `test_worker_on_edge.py`, `tests/cli/test_q_execution.py`, `tests/cli/test_q_market_publisher.py` and affected
  execution API/cache tests with `uv run pytest <paths> -q`. Commit.

### 6. Ordinary demo deployment from q_terminal

**Files (q_terminal):** `src/execution/{commands,enablement}.rs`, `src/execution_controls.rs`,
`src/execution_models.rs`, `qml/DeployDialog.qml`, `qml/ConfirmDialog.qml`, the existing execution API client/models,
capability tests and focused command/enablement tests; update `BOUNDARY.md`/operations documentation for
general demo support.
**Interfaces:** consumes step 1's generic account capabilities; creates an `mt5_live` deployment to that
demo account using the normal activation request/confirmation. It does not call administrative replay routes.

- [ ] Write failing Rust tests for demo capability available/absent, account allowlist/activation refusals,
  no automatic real-account enablement, deployment selection and commands carrying the correct account.
- [ ] Add the demo choice/confirmation and capability-driven enablement. Preserve ordinary paper behavior,
  kill-switch and unknown-order handling. No speed, step, pause-market or run selector in the terminal.
- [ ] Run the affected Rust tests, crate formatting/clippy and `qmllint` for touched QML using repository
  tooling; capture and inspect affected gallery states per AGENTS.md. Do not run a full terminal check/build
  matrix unless required by an actual changed integration. Commit.

### 7. Isolated replay composition and lifecycle

**Files (q):** `dev`, new `tools/dev/replay.sh`, `tools/dev/shell.sh`, `tools/tests/test-dev`,
new `tools/tests/test-replay`, `README.md`. **Files (q_backend):** dedicated replay Compose config and
`deploy/systemd/replay/` unit templates/env examples; do not rewrite normal live units or environment files.
**Interfaces:** `./dev replay --config <path>`, status/log/restart/down aliases as specified; all services
receive the same validated run generation. State is provisioned before normal stack services start.

- [ ] Write failing shell mocks for all aliases, repeated start, persistent lifecycle, pause-on-start,
  port collision, absent core capability, mixed endpoints, live/Research coexistence, generation replacement
  refusal, scoped down and down-all. Assert no Wine/MT5/GPU commands or writes to existing environments.
- [ ] Implement dedicated instances, run-scoped DB/cache/Redis/data and terminal settings, readiness and
  external resume instructions. A new generation requires stopped replay services and fresh operational
  roots; preserve old artifacts. Keep the simulator unable to proxy a broker.
- [ ] Run `bash -n dev tools/dev/replay.sh tools/dev/shell.sh`, `bash tools/tests/test-replay` and the relevant
  existing launcher mock tests. In q_backend, test unit/config rendering without starting host services.
  Commit in both repositories.

### 8. Whole-path fixture, documentation and handoff

**Files (q_backend):** `tests/replay/test_stack_integration.py`, `tests/replay/test_journal_reproduction.py`,
`docs/operations/market-replay.md`, `README.md` and the batch document.

- [x] Add one opt-in integration scenario with isolated PostgreSQL/Redis, the simulator, the ordinary API,
  publisher/worker, a public fixture strategy and normal activation. Assert intent → delayed fill → ledger
  → outbox/stream events plus timeout/reconciliation, fee persistence and one fill after restart. Inject
  actual request delay; do not bypass HTTP with a direct test broker or replace the production evaluator.
- [x] Re-run recorded ingress against a fresh simulator journal and compare canonical broker outcomes;
  record why two independently scheduled interactive runs may differ.
- [x] Run `uv run pytest tests/replay -q -m 'not integration'`; then run only the new opted-in integration
  scenario against dedicated dependencies. Run changed-file Black/Ruff and contract checks in consumers.
- [x] Document preparation/config, defaults/overrides, capability requirements, model assumptions, endpoint
  separation, pause/step/timeouts, end-of-data/open positions, source coverage and historical clock behavior.
- [ ] Perform one human terminal inspection with the fixture: deploy while paused, resume externally, observe
  orders and positions and delayed fill, pause/step externally, then stop replay and confirm the other stacks
  are unchanged. Record actual results or a concrete unavailable-tool blocker; never claim UI verification
  based only on API tests.
- [x] Commit focused changes per repository and run `./work board set Q-145 in-review -m "<per-repository
  changes; exact checks/results; terminal inspection evidence; model limits/follow-ups>"`. No push or merge.

## Self-review coverage

Spec acceptance 1 maps to steps 3–8; 2 to steps 2, 3 and 5; 3–5 to steps 2 and 4; 6 to steps 2 and 5;
7 to steps 5 and 7; 8 to steps 2 and 8; 9 to contract checks, focused regressions and documentation throughout.
The two native numerical models are owned and released by Q-144. No temporary fake gateway is a deliverable.
