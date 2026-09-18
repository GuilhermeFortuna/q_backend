# Q-042: Execution worker on the edge

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.1, §3.3, §6.1, §6.2, §8.1, §9 invariants 3 and 4, §10 Phase 4](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#62-intent-identity-and-idempotency)  
**Depends on:** Q-040, Q-041  
**Implementation plan:** [`../plans/Q-042-execution-worker-on-the-edge-plan.md`](../plans/Q-042-execution-worker-on-the-edge-plan.md)

## Purpose

After Q-040 the Wine edge can submit and look up orders, but the worker still
gets its quotes by importing `MetaTrader5` through the market-data service, and
its live broker still drives `MetaTrader5` directly. On Linux both can only
reach a stub, so the worker cannot trade at all without Windows. This task puts
the worker on the edge for everything that touches the terminal: quotes for
paper and live deployments alike, the account for the live gates, submission,
and lookup-only reconciliation. It enforces §6.2 in the worker: one submission
attempt per intent, ever, with every ambiguity resolved only by lookup. Live
deployments become possible end to end, behind the same default-deny gates as
today, and the Linux side of execution stops importing `MetaTrader5`.

## Requirements

### Edge client

- The worker reaches the edge through one client, typed by the vendored edge
  contract, with explicit timeouts per operation. The client never retries a
  submit.
- A transport error or timeout on submit is an indeterminate outcome, never an
  error that aborts the bar or leaves the order in an intermediate state.
- The client refuses to talk to an edge whose health reports a different schema
  major.

### Quotes

- Every deployment's executable quote comes from the edge. The freshness gate
  uses the age the edge reports, not a comparison between the worker's clock
  and the terminal's timestamp.
- An unreachable edge, or a disconnected terminal, is "no quote". It blocks new
  orders through the existing quote freshness risk check, and never raises out of
  the poll loop.

### Live broker on the edge

- `mt5_live` deployments submit through the edge. Paper deployments keep the
  paper broker, priced from edge quotes. The worker picks the broker per
  deployment, from its broker mode.
- Before any live submission the four existing gates are evaluated, unchanged
  and default-deny. The account login comes from the edge's account operation.
  A disabled account or terminal is a trading-disabled rejection, as today.
- An accepted submission is confirmed by a lookup of that intent. Filled records
  the fill. Anything short of filled leaves the order unknown and pending
  reconciliation, and blocks the deployment. The worker never infers a fill from
  acceptance alone.
- A rejected submission records the rejection. An indeterminate submission,
  timeout, transport error or duplicate-intent answer leaves the order unknown
  and pending. The worker never submits that intent again.
- Reconciliation asks the edge's lookup, with a window that starts before the
  intent was recorded and ends now. Only filled, rejected and not found close
  an order. Unavailable records the attempt and keeps blocking.
- Flatten on a live deployment follows the same path as any other order.

### Deployments

- The control API accepts `mt5_live` as a broker mode when a deployment is
  created. Creation does not bypass any gate: a live deployment whose gates are
  closed runs, and every order it attempts is rejected as live-locked.
- The OpenAPI capture in `q_contracts` is refreshed for the new broker mode.

### Invariant 3

- No module under the backend's execution package imports `MetaTrader5`,
  directly or through a helper, and a test enforces it.
- The Linux-side MetaTrader runtime wrapper, the direct-MT5 quote source and the
  MetaTrader broker's direct terminal calls are deleted. Their tested behaviours
  (gates, volume normalization, fill aggregation) are kept where they still
  apply, or proven to live in the edge.

## Constraints and non-goals

- **No real-money activation.** The gates stay as they are and default to
  deny. Setting them for a real account is an operator decision outside this
  batch. The human check uses a broker demo account.
- **No change to risk checks, the ledger, leases, recovery flow or the kill
  switch,** beyond what routing orders to the edge requires.
- **No change to market-data bar sourcing.** The bar coordinator keeps reading
  bars through the data gateway.
- **The research side's MetaTrader imports** (the native market-data client used
  only on Windows) are out of scope. They are recorded as a finding against
  invariant 3.
- **No systemd unit for the worker.** That is Q-045.

## Acceptance criteria

### Agent-verifiable

1. A test with a fake edge server covers each submit outcome: accepted then
   filled, accepted then not yet filled, rejected, indeterminate, timeout,
   transport error, and `duplicate_intent`. Each ends in the documented order
   status and reconciliation state, with exactly one submit request per intent.
2. Across a worker restart with an unknown live order, the fake edge receives
   lookups and no submit for that intent.
3. Reconciliation closes an order on filled, rejected and not found, and keeps
   it pending on unavailable, with the lookup window starting before the
   intent's creation.
4. With each live gate closed in turn, a live deployment's order is rejected
   with the existing code, and the fake edge receives no submit.
5. With the fake edge down, the poll loop continues, no order is created, and
   the risk event is a stale or unavailable quote.
6. The freshness gate rejects a quote whose reported age exceeds the limit, even
   when its timestamp looks fresh to the worker's clock.
7. Creating an `mt5_live` deployment through the API succeeds. The recaptured
   OpenAPI shows the broker mode, and `make contracts-check` passes.
8. A hygiene test fails if any module under `q_backend/execution` imports
   `MetaTrader5`, and it passes.
9. The paper execution tests pass with unchanged expected values, with quotes
   now served by a fake edge.
10. The full validation suite passes.

### Human-verifiable

1. Against a broker demo account under Wine, with the gates opened for that
   account only, an `mt5_live` deployment of MACrossover on `WIN$N` M1 takes one
   entry. The fill appears in the ledger with the terminal's deal ticket, and
   the terminal shows one position.
   Command: `uv run q-execution --log-level INFO run 2>&1 | tee worker.log`, then
   `curl -s http://127.0.0.1:8000/api/v1/execution/deployments/<id>/fills | jq`
2. A worker started with the validation-only crash checkpoint
   `after_broker_response` dies right after its first demo submission. Started
   again normally, it resolves that order by lookup, records the fill from the
   terminal's deal, and no second order appears in the terminal.
   Command: `uv run q-execution run --crash-at after_broker_response`, then `uv run q-execution run`
3. Flatten from the API closes the demo position through the edge, and the
   ledger returns to flat.
   Command: `curl -s -X POST http://127.0.0.1:8000/api/v1/execution/deployments/<id>/actions -d '{"action":"flatten","confirm":true}' -H 'content-type: application/json' -H "Idempotency-Key: $(uuidgen)" | jq`
