# Q-068 implementation plan: Paper execution audit and performance

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-068-paper-execution-audit-and-performance-spec.md`](../specs/Q-068-paper-execution-audit-and-performance-spec.md)  
**Depends on:** Q-067

## Current-system context

`execution/brokers/paper.py` already fills from `EdgeQuoteSource` at bid/ask.
`execution/service.py` commits intent before broker dispatch and applies the
fill through `ExecutionLedger`; `execution/reconciliation.py` handles unknowns.
`execution/worker.py` and `cli/q_execution.py` currently construct one
`PaperCostConfig` from global settings, including a global point value.
`ExecutionFill` persists quote fields, but the paged API response omits them.
`ExecutionOrder` has `submitted_at` but no dispatch-attempt record. The dev
systemd API/worker units load an operator-editable backend environment file.

## Interfaces produced

```text
GET /api/v1/execution/deployments/{id}/performance
GET /api/v1/execution/deployments/{id}/performance/marks?limit=50&offset=0
```

The summary returns `realized_pnl`, nullable `unrealized_pnl`, `fees`,
nullable `net_pnl`, `closed_trade_count`, `win_count`, nullable `win_rate`,
`mark_status`, nullable `marked_at`, `config_revision`. The paged marks return
`bar_close_time`, `config_revision`, `mark_status`, nullable `quote_bid`,
`quote_ask`, `quote_timestamp`, `mark_price`, `unrealized_pnl`, `equity_delta`,
and non-null `realized_pnl` and `fees`. Money/price values serialize as exact
decimal strings. Extend order responses with `dispatch_attempted_at` and
fill responses with the existing persisted quote fields.

Persistence: add `dispatch_attempted_at` to orders and
`execution_paper_marks` with unique `(deployment_id, bar_close_time)`. Store
the post-fill mark and cumulative per-deployment realized P&L/fees at the
completed-bar boundary. Closed-trade statistics can be derived from ordered
fill/ledger history; do not maintain a second mutable trade total.

## Implementation decisions

- Add `execution_paper_only` to settings. The dev API and worker service
  commands force it to true after loading the environment file. The API
  rejects live create/activation; the worker rejects live orders before
  `BrokerRouter`, even for pre-existing live rows.
- Keep the edge running for read-only quotes. No second paper receiver process
  is needed: `PaperBroker` is the receiver behind `BrokerRouter`.
- Commit the attempt timestamp between intent and adapter call. Show it as an
  attempt; only the committed fill/rejection is a receipt. Preserve unknown
  reconciliation if the process fails in that interval.
- Obtain point value and cost inputs from the deployment revision at each
  worker operation, including flatten, mark, recovery and manual resolution.
  Keep quote-age limits from system risk settings. Backfill old deployments
  with one snapshot of current settings on migration.
- Write marks once per completed bar after order processing. A failed quote
  yields an unavailable mark with nullable computed values. The curve is
  per-deployment net P&L from zero, not the shared paper account's equity.

## Ordered implementation

- [ ] 1. On the Q-068 task branch, pin Q-066 contracts and run
  `make contracts-check`. Add failing tests for forced paper-only API and
  worker behavior with every live gate enabled and an existing live row.
- [ ] 2. Add the forced dev profile in the backend-owned dev API/worker unit
  templates, API gate and
  worker pre-routing gate. Use a fake edge counter to prove zero submit calls
  while quote and health reads continue.
- [ ] 3. Add migration/repository support for `dispatch_attempted_at` and
  marks. Test the intent → attempt → result sequence, rollback and crash
  windows with the existing execution service/reconciliation fixtures.
- [ ] 4. Route per-deployment cost and point value through worker, service,
  ledger, risk, reconciliation, flatten and manual resolution. Test two
  deployments on one account with different costs and a paused revision edit.
- [ ] 5. Add mark recording and performance queries. Test long, short,
  reversal, fees, no completed trades, unavailable quote, duplicate bar and
  restart. Expose summary and paged marks through the execution API.
- [ ] 6. Extend order/fill response and stream serializers, recapture OpenAPI
  through `q_contracts`, run `make contracts-check` and `./scripts/ci.sh`,
  and commit focused changes on the task branch.
- [ ] 7. During an open market, run `./dev up execution` with a live-only MT5
  account, inspect one paper decision-to-ledger chain, and verify no Q order
  or deal appears in the MT5 account history. Record timestamps and results.

## Review focus

- Operator environment settings must not override the dev paper-only guard.
- Existing live rows must not reach the edge submit path.
- Dispatch attempt must never be misreported as a confirmed fill.
- Missing quotes must not create stale or zero-valued performance marks.
- Shared account balances must not be mislabeled per-deployment equity.
