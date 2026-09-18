# Q-040: Wine execution edge

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.1, §6.2, §6.3, §8, §9 invariants 3 and 4, §10 Phase 4](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#63-edge-process-contract)  
**Depends on:** Q-039  
**Implementation plan:** [`../plans/Q-040-wine-execution-edge-plan.md`](../plans/Q-040-wine-execution-edge-plan.md)

## Purpose

The execution edge is the only process that will ever call `order_send`. Its
wire contract has existed since Q-004, but nothing implements it. The backend's
order path today is `MetaTraderBroker`, which imports `MetaTrader5` into a
Linux-side process, in breach of invariant 3. On Linux it can only run
against a stub. This task builds the edge: a standard-library process inside
the Wine prefix, next to the existing data gateway, that implements the
execution contract exactly. It submits at most once per intent, never
retries, and reports ambiguity as ambiguity. It runs as a systemd user unit
bound to the terminal. Q-042 then moves the worker onto it and removes the
Linux-side MetaTrader import.

## Requirements

### The process

- The edge is a single file outside `src/`, beside the data gateway, and
  imports only the standard library, `MetaTrader5` and `numpy`. It can never
  import `q_backend`.
- It binds to loopback only and refuses to start if asked to bind anything
  else.
- It serves `/v1/health` with `status`, `schema_version`, `mt5_connected` and
  `terminal_build`. Health answers 200 whether or not the terminal is
  connected.
- It refuses a request whose declared schema major differs from its own, with
  `schema_major_mismatch`.
- It initializes MetaTrader lazily and serializes every MetaTrader call behind
  one lock, because the module is not thread-safe.

### Operations

- `quote` returns bid, ask, last, the raw quote time, and a quote age computed
  from the edge's own clock.
- `check` runs the terminal's order pre-check and reports its return code and
  margin figures without submitting.
- `submit` places a market order for the intent, deriving `magic` and `comment`
  from the intent identifier by the contract's formula. It refuses a request
  whose supplied `magic` or `comment` disagree with the derivation. It answers
  exactly one of accepted, rejected or indeterminate.
- `lookup` searches active orders, open positions and deal history within the
  given window for the intent's `magic` and `comment`. It answers exactly one of
  filled with deal detail, rejected, not found, or unavailable, and states
  whether the answer closes the intent. It never submits.
- `positions` and `deals` return the terminal's open positions and the deal
  history in a window, in the contract's shapes.
- `account` returns the logged-in account's login, server, currency, trading
  permissions, balance, equity and free margin, or unavailable when the
  terminal is disconnected.

### Safety

- **At most one submission per intent per process lifetime.** The edge records
  an intent identifier before it calls `order_send`, not after. A second submit
  for a recorded intent is answered `duplicate_intent` and never reaches the
  terminal, even when the first is still in flight.
- **No retry and no invented outcome.** An `order_send` that returns nothing,
  raises, or returns an ambiguous code is answered indeterminate. The edge does
  not call `order_send` again, and does not turn indeterminate into accepted or
  rejected by any inline check.
- A terminal that is disconnected answers `lookup` with unavailable, never with
  not found. Unavailable leaves the intent open.
- The edge keeps nothing across restarts. Its intent table lives only in
  memory, and the documentation says so where an operator will read it.

### Operation

- A systemd user unit runs the edge under the Wine Python. It is bound to the
  terminal unit, and becomes ready when health reports `mt5_connected: true`.
- The Wine setup script installs nothing new. The edge runs on the interpreter
  and packages the data gateway already uses.
- The operator guide covers the edge alongside the gateway: its port, its
  unit, its logs, and how to confirm health.

### Conformance

- A test drives every operation through a real HTTP server with a fake
  `MetaTrader5` module, and validates every response against the vendored
  contract schemas.
- A test proves the route table equals the contract's endpoints, by reading the
  edge's source rather than importing it.

## Constraints and non-goals

- **No worker change.** The worker keeps `PaperBroker` and `MetaTraderBroker`
  until Q-042.
- **No pending or limit orders, no stop-loss or take-profit on submit.** Tier A
  sends market orders. The contract permits more fields, and the edge refuses
  any request that uses them, with `invalid_request`.
- **No authentication token.** Loopback binding is the edge's boundary, as the
  contract states.
- **No persistence of the intent table.** Durable deduplication is the ledger's
  job (§6.2).
- **No change to the data gateway.**
- **No tier B.** No EA, no signed limits, no heartbeats.

## Acceptance criteria

### Agent-verifiable

1. The edge's imports, read by parsing its source, are only standard-library
   modules, `MetaTrader5` and `numpy`.
2. Starting the edge with a non-loopback host exits non-zero with a message.
3. For every contract operation, a request against the running edge with a
   fake `MetaTrader5` returns a response that validates against the vendored
   schema, for each outcome the fake can produce.
4. Two concurrent submits with one intent identifier cause exactly one
   `order_send` call. The second submit is answered `duplicate_intent`, and so
   is a third submit after the first has finished.
5. `order_send` returning `None`, raising, or returning each ambiguous return
   code yields `indeterminate`, with exactly one `order_send` call.
6. A submit whose `magic` or `comment` disagree with the derivation is refused
   with `intent_field_mismatch` and makes no terminal call. The derived values
   equal the contract vectors.
7. `lookup` with a disconnected terminal answers `unavailable` with
   `closes_intent: false`, and with a matching deal answers `filled` with that
   deal.
8. A request with schema major 2 is refused with `schema_major_mismatch`.
9. The edge's route table equals the contract's endpoints.
10. The unit file passes the existing unit-structure tests, and declares
    `BindsTo=` and `After=` on the terminal unit.
11. The full validation suite passes.

### Human-verifiable

1. Under Wine, with a broker demo account logged in, the edge reports
   `mt5_connected: true` and the terminal build. `quote` for `WIN$N` returns an
   age under two seconds during market hours. `positions` and `deals` match the
   terminal's own tabs.
   Command: `systemctl --user start mt5-edge && curl -s 127.0.0.1:18813/v1/health | jq && curl -s '127.0.0.1:18813/v1/quote?symbol=WIN$N' | jq`
2. With the demo account, one minimum-volume market submit returns accepted.
   Resubmitting the same intent returns `duplicate_intent`, and the terminal
   shows one order. A lookup for that intent returns filled with the deal. The
   position is then closed by hand in the terminal.
   Command: `curl -s -X POST 127.0.0.1:18813/v1/submit -d @submit.json | jq` (twice), then `/v1/lookup`
