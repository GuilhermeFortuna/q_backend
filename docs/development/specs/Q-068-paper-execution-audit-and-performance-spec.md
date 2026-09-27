# Q-068: Paper execution audit and performance

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.3, §6.1, §9](https://github.com/GuilhermeFortuna/q_contracts/blob/main/docs/system-architecture.md)  
**Depends on:** Q-067  
**Implementation plan:** [`../plans/Q-068-paper-execution-audit-and-performance-plan.md`](../plans/Q-068-paper-execution-audit-and-performance-plan.md)

## Purpose

Q already has a broker-neutral worker, a `PaperBroker`, an intent ledger and
real quotes from the MT5 execution edge. This task makes that path safe to run
against a live-only MT5 account: the dev stack never submits broker orders,
while the terminal can verify every simulated dispatch and inspect ongoing
paper performance. It applies each deployment's frozen cost revision rather
than shared worker-wide defaults.

## Requirements

### Paper-only dev stack

- `./dev up execution|full` starts its API and execution worker in a forced
  paper-only profile. The profile is imposed by the launcher/service command,
  not by an operator-editable backend environment file. The API rejects new
  `mt5_live` deployments and live activation in this profile. The worker has
  a separate guard before broker routing and refuses existing live deployments
  as `live_locked`. Neither a permissive live environment flag nor an existing
  database row can cause an MT5 `order_send` from the dev stack.
- The MT5 execution edge remains available for read-only quote and account
  health operations. Paper fills use its fresh bid and ask, not a fabricated
  candle-close price. A disconnected edge, stale/crossed quote or closed
  session prevents a fill and produces a visible reason.
- Outside the dev paper-only profile, the existing gated live adapter remains
  unchanged. This task does not certify live trading.

### Cost application and dispatch audit

- The worker reads `paper_cost_config` from the deployment's current revision
  for sizing/point value, risk/marking, paper fill price, commission,
  reconciliation and manual resolution. It never silently falls back to a
  global point value for a revisioned deployment. Existing deployments are
  backfilled once from current settings during migration.
- Retain intent-before-submission. After the intent commit and before calling
  `PaperBroker`, durably record `dispatch_attempted_at` and emit the order
  state. A paper fill/rejection then records broker result and its time. A
  crash in the gap remains unknown and follows existing reconciliation; it is
  never automatically resent.
- The audit API and terminal-readable order/fill responses expose decision,
  intent and revision IDs, dispatch attempt, status, rejection or unknown
  reason, bid/ask and quote time, fill price, slippage and fee. A paper receipt
  is the committed fill or rejection, not the attempt timestamp alone.

### Paper performance

- Record one mark per running deployment and completed bar, after any fill for
  that bar, with a unique `(deployment_id, bar_close_time)` key. Mark an open
  long at bid and an open short at ask. Store quote time and source. On a
  missing/stale quote store `mark_status=unavailable` and null mark/unrealized
  values; never reuse an older price or write a zero-valued substitute.
- `GET /api/v1/execution/deployments/{id}/performance` returns exact-decimal
  per-deployment realized P&L, unrealized P&L when marked, fees, net P&L,
  closed-trade count, win count and win rate (null until a trade closes),
  latest mark status/time, and current config revision. Performance spans
  revisions but the response identifies the current one. A closed trade's
  net result includes both entry and exit fees; a positive net result is a
  win. Reversals count as a close followed by a new entry.
- `GET /api/v1/execution/deployments/{id}/performance/marks` returns paged
  completed-bar history with realized P&L, fees, unrealized P&L when available,
  and `equity_delta = realized - fees + unrealized`. This starts at zero for
  each deployment and is not presented as an allocation of the shared paper
  account's cash or equity. The existing account view remains authoritative
  for account balance and equity.
- All performance numbers derive from durable fills, ledger, positions and
  marks. No terminal money math or per-tick database writes.

## Constraints and non-goals

- Market orders only; paper fills are full or rejected at top of book plus
  configured deterministic slippage/commission. Depth and broker execution
  probability cannot be inferred from a quote.
- Keep leases, kill switch, one-position rule, at-most-once intent and unknown
  order reconciliation. Do not introduce an additional receiver process.
- No UI work; Q-069 presents these APIs.

## Acceptance criteria

1. With all existing live flags, allowlists and deployment activation set,
   the dev profile still rejects live creation and sends zero calls to edge
   `submit`/MT5 `order_send`. An existing live row is also refused. Read-only
   quote calls still work.
2. For a paper signal, a durable decision and intent precede the dispatch
   attempt; a received paper result produces one filled/rejected order and,
   when filled, one fill and corresponding ledger/position change. A crash at
   each boundary neither duplicates nor silently resends an order.
3. Buy/cover fills use ask; sell/short fills use bid. Per-deployment slippage,
   fee and point value affect fills, P&L and marks and remain attributable to
   the decision's revision after a paused edit.
4. Stale, missing or crossed quotes and closed sessions cannot produce a fill;
   the audit shows the exact reason and performance does not invent a mark.
5. Long, short, close and reversal fixtures reconcile ledger totals with
   per-deployment realized P&L, fees, win rate and equity delta. Marks are
   unique per completed bar across retries and restarts.
6. The paged history and current summary use decimal strings, and the OpenAPI
   capture plus `make contracts-check` and `./scripts/ci.sh` pass.
7. In a live-quote paper walkthrough, a completed-bar signal appears in the
   decision → order → paper fill → ledger chain, while the MT5 account order
   and deal history shows no new submission from Q.
