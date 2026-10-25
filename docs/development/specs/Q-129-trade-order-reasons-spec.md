# Q-129: Trade order reasons

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 33 — Trade order reasons
**Depends on:** —
**Implementation plan:** [Plan](../plans/Q-129-trade-order-reasons-plan.md)

## Purpose

A research strategy cannot say why it entered or left a trade. The Research UI Trade List has an Exit Reason column, but it shows only engine codes (`SIGNAL`, `STOP_LOSS`, `TAKE_PROFIT`, …). There is no entry reason anywhere.

After this task, a strategy can label its orders:

```python
return TradeOrder.buy(stop_loss=low, reason="VWAP reclaim")
...
return TradeOrder.close(reason="Momentum faded")
```

The labels then appear in the following places:
- in the trade records;
- in the CSV export;
- in a new Entry Reason column and the existing Exit Reason column of the Trade List, both sortable;
- in a Performance tab chart, renamed "By reason", that switches between entry and exit reasons.

## Behaviour

### `TradeOrder.reason`

- `TradeOrder` gains an optional `reason: str | None = None`. It is optional on every entry and on `close`. Omitting it leaves the order and its trade exactly as they are today, so existing strategies need no change.
- It must be `None` or a non-blank `str`; anything else raises `ValueError` naming `reason`. The value is stored as given.
- The entry actions and factories accept it: `buy`, `sell`, `buy_stop`, `buy_limit`, `sell_stop` and `sell_limit`. It becomes the `entry_reason` of the trade the order opens.
- `close` accepts it, and it becomes the `exit_reason` of every trade that close takes out.
- `amend` and `cancel` reject a reason with `ValueError`.

### Which reason a trade carries

- `entry_reason` comes from the order that opened the trade. That is the deciding bar's order for market and priced entries, and the order that placed the resting order for stop and limit entries. It is the same attribution that `stop_loss` and `take_profit` use today. A trade opened by an order without a reason has `entry_reason = null`.
- `exit_reason` comes from a reasoned `close` only when the kernel reports the exit as `SIGNAL`:
  - a plain close is decided on bar *b* and fills at the open of *b + 1*;
  - a priced close fills on its own bar.

  A `SIGNAL` exit without a reasoned close stays `SIGNAL`.
- Stop-loss, take-profit, end-of-day, force-close and exit-rule exits keep their engine labels, even when the entry or a later close carried a reason.
- Chunked and parallel research runs give the same reasons as a single run.

### Records and exports

- `Trade` gains `entry_reason: Optional[str]`. It is included in `model_dump`, so it reaches published runs, lake artifacts and the API trade dicts, which stay free-form in the contract.
- The research trades frame (`research/results.py`) gains an `entry_reason` column. The trades CSV export gains an `entry_reason` column before `exit_reason`.
- Runs stored before this task have no `entry_reason`; they read as null.

### Performance

- `BacktestPerformance` gains `entry_reasons`, a list of `{reason, trades, pnl, win_rate}` per `entry_reason`. It uses the same grouping, the `"unspecified"` fallback and the ordering as `exit_reasons`.
- The contract schema `ExitReasonStats` is renamed `ReasonStats`, and both lists use it.
- A stored `performance` without `entry_reasons` is treated as missing. The result endpoint recomputes it on read from the stored trades and does not write it back, as Q-123 does for results without `performance`.

### Research UI

- **Trade List** (Backtests → Trade List):
  - An Entry Reason column follows Entry Price, with `-` when null.
  - Size, Entry Reason, Exit Reason and PnL are sortable. Clicking a header cycles ascending → descending → unsorted, and the header shows the sort indicator.
  - Reasons sort by their displayed label, case-insensitively. Null values sort last in both directions. Equal values keep the original trade order.
  - No other column is sortable.
- **Reason labels:** the known engine codes keep their labels (`STOP_LOSS` → "Fixed Stop Loss", `SIGNAL` → "Signal Exit", …), and every q_core exit-rule id has one. Any other string, which includes every strategy-supplied reason, is shown verbatim.
- **Trade hover card:** an Entry Reason row appears when the trade has one.
- **Performance tab:**
  - The "By exit reason" panel becomes "By reason", with an Entry / Exit segmented toggle that defaults to Exit.
  - The chart, labels and tooltip show the selected list.
  - The panel shows when either list is non-empty.
  - When the selected list is empty, the chart is replaced by a short empty message.

## Interfaces

- `q_backend.research.TradeOrder`: the optional `reason` field (default `None`) and an optional `reason=None` keyword-only parameter on the seven factories listed above.
- `q_backend.backtesting.models.Trade.entry_reason`.
- `q_backend.backtesting.analytics.compute_backtest_performance` fills `entry_reasons`. The Pydantic model `ExitReasonStats` becomes `ReasonStats` in `api/schemas/backtest.py`.
- `q_contracts/schema/api/openapi.yaml`:
  - `ExitReasonStats` is renamed `ReasonStats`;
  - `BacktestPerformance` gains a required `entry_reasons` array of `ReasonStats`.

  The generators are re-run, and q_backend and q_frontend pin the new `CONTRACTS_REV`.
- q_frontend:
  - `Trade.entry_reason?: string | null` in `src/types/backtesting.ts`;
  - `DataColumn` gains an optional `sortValue(row)`, used for sorting instead of `row[id]` when present;
  - `ReasonStats` replaces the `ExitReasonStats` re-export.
- Docs: the `TradeOrder` docstrings, and `docs/research/user_guide/strategy.rst` with an example of entry and exit reasons.

## Focused acceptance

1. `TradeOrder` accepts an optional `reason` on the entry actions and on `close`, and the factories pass it through. Orders built without it behave exactly as before, and existing research tests pass unchanged. It rejects blank or non-string reasons, and rejects any reason on `amend` and `cancel`.
2. A synthetic research backtest produces the expected reasons:
   - reasoned market, priced, stop and limit entries yield those `entry_reason` values;
   - reasoned plain and priced closes yield those `exit_reason` values;
   - stop-loss and take-profit exits keep `STOP_LOSS` and `TAKE_PROFIT`;
   - an unreasoned close stays `SIGNAL`;
   - unreasoned entries give null.
3. The same strategy run in chunked or parallel sessions returns the same reasons.
4. `compute_backtest_performance` returns `entry_reasons`, with `"unspecified"` for null reasons. A stored result whose `performance` lacks `entry_reasons` gets it on read, and nothing is written.
5. The trades CSV export has the `entry_reason` column.
6. Frontend tests:
   - the Trade List renders Entry Reason;
   - Size, Entry Reason, Exit Reason and PnL sort ascending, then descending, then unsorted, with nulls last, and reasons sort by label;
   - other headers do not sort;
   - user reasons render verbatim;
   - the By reason toggle switches between entry and exit data.
7. `make check` passes in q_contracts. `make contracts-check` passes in q_backend and q_frontend. The backend lint and focused pytest pass, and `pnpm typecheck` and focused vitest pass with `TZ=America/Sao_Paulo`.

Tests use synthetic bars and ticks, the API test client and a temporary lake root. No worker, Redis, gateway, Docker, GPU, Wine or desktop run is needed.

## Delivery boundary

- No q_core change; reasons are attributed in q_backend.
- No reasons on `amend` or `cancel`, no per-level stop or target labels, and no reasons in live execution or q_terminal.
- No sorting on Trade List columns other than the four above. No change to the other tables that use `DataTable`.
- No backfill or write-back for stored runs.
