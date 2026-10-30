# Q-137: Stop, limit and protective orders on MT5

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 38 — Research strategies on MT5
**Depends on:** Q-136, Q-139 (revised in Batch 39 to include its order types)
**Repositories:** q_backend (owner), q_contracts
**Implementation plan:** [Plan](../plans/Q-137-stop-limit-and-protective-orders-on-mt5-plan.md)

## Purpose

Q-136 deploys strategy versions that use market orders only. Most research strategies also use protective stops
and targets, move them with `amend`, or enter with resting stop and limit orders. Live, these orders must rest at
the broker, which fills them against real ticks as the backtest's tick replay does. This task sends those orders
to MT5 through the execution edge, records their fills, and adds them to the deployable set. Batch 39 adds the
live-placeable orders that replace same-candle priced orders: OCO groups, timed exits and expiry. Trailing and
breakeven exit rules are out of scope: a strategy that trails uses `amend`.

## Current system

- The edge (`gateway/mt5_execution_edge.py`) builds `TRADE_ACTION_DEAL` requests only, and refuses order types
  other than buy and sell. `/v1/lookup` resolves an intent from deals matched by magic and comment.
  `/v1/positions` and `/v1/deals` exist. Deals carry no position id and no deal reason.
- `ExecutionBroker` (`execution/brokers/base.py`) has `health`, `submit_market_order` and `lookup_order`.
  `ExecutionOrder` rows (`storage/db/execution_models.py`) have a side, an `order_type`, a quantity, a status and
  a reconciliation state. Intents are committed before dispatch, and ambiguous outcomes go to reconciliation.
- Research semantics (`research/orders.py`, `docs/research/user_guide/execution.rst`):
  - `stop_loss` and `take_profit` attach to an entry;
  - `amend` moves the levels of open positions;
  - one working stop or limit entry rests for `expire_bars` bars after the deciding bar (or until filled,
    replaced or cancelled);
  - `cancel` removes it.

  Stops fill at the first trade at or beyond the level, and targets and limits at their level.
- Q-139 (`research/orders.py`) adds:
  - `TradeOrder.oco(...)`, groups of resting entries where the first fill cancels the rest;
  - `close(at=...)`, timed exits filled at the first trade at or after an instant;
  - `expire_at=` on resting entries.

  Their kinds are `oco`, `timed_exit` and `expiring_entry`. Trailing and breakeven stops come from `exit_params`
  and are updated on every tick in backtests; they are not carried live by this task.
- Q-136's deployable set is `market_entry` and `market_close`. Any other kind pauses the deployment.

## Behaviour

### Edge contract (`q_contracts`)

- A new minor version of the execution-edge contract (major 1). `COMPAT.md` records it, and the backend refuses
  pending and protective intents when the edge advertises an older version.
- Submit requests gain an `action`:
  - `deal`: today's market order. It may now carry `sl` and `tp`.
  - `pending`: a buy-stop, sell-stop, buy-limit or sell-limit order with `price`, optional `sl` and `tp`, and an
    optional `expiration` (UTC). Without an expiration it is good-till-cancelled.
  - `sltp`: new `sl` and `tp` for the position `position`.
  - `remove`: cancel the pending order `order`.

  Each action carries an intent id under the existing at-most-once, never-retry and intent-field rules.
- `GET /v1/orders?symbol=` lists working pending orders, each with ticket, type, price, `sl`, `tp`, expiration,
  magic and comment.
- Deals gain `position_id` and `reason` (`client`, `expert`, `sl`, `tp`, `so` or `other`), so broker-side exits are
  identified by position, not by intent.
- Lookup gains pending outcomes: `placed`, `filled`, `cancelled`, `expired` and `rejected`, as well as the
  existing indeterminate outcome.

### Ledger and broker (`q_backend`)

- `ExecutionOrder` gains:
  - an intent kind: `entry`, `close`, `protect` or `cancel`;
  - order types `stop` and `limit`;
  - a limit or stop price, `stop_loss`, `take_profit` and an expiration;
  - the broker's order and position tickets.

  New statuses are `working`, `cancelled` and `expired`.
- An open position stores its current `stop_loss` and `take_profit`.
- `ExecutionBroker` gains `submit_pending_order`, `set_protection` and `cancel_order`. `submit_market_order`
  accepts levels. The paper broker refuses the new calls with a clear rejection, so a paper deployment of a
  strategy that needs them cannot start.
- Each new intent is committed before dispatch, and resolved through lookup and reconciliation as market intents
  are.

### Order mapping

| Research order | MT5 |
| --- | --- |
| `buy()` / `sell()` with `stop_loss`/`take_profit` | `deal` with `sl`/`tp` |
| `amend(stop_loss=, take_profit=)` | `sltp` on the deployment's position |
| `buy_stop`, `buy_limit`, `sell_stop`, `sell_limit` | `pending`, with expiration at the close of the deciding bar plus `expire_bars` bars |
| a new resting entry while one is working | `remove` the working one, then place the new one |
| `cancel()` | `remove` |
| `close()` | `deal` closing the position, as today |
| `TradeOrder.oco(...)` | one `pending` per member; on the first fill, `remove` the others |
| resting entry with `expire_at=` | `pending` with that expiration |
| `close(at=...)` | a `deal` closing the position, sent by the worker at the first poll at or after the instant |

- A protective level the broker rejects (for example, too close to the market) records the rejection on the
  intent. An entry with rejected levels is not sent without them.
- When day trading, a working entry is removed at the decision bar after `day_trade_end_time` and at the close
  time, and protective levels are left to the flatten.
- Pause, stop, kill switch and flatten remove working orders before closing positions.
- If two OCO members fill before the worker can remove the other orders, the worker closes the excess at market,
  records a risk event, and pauses the deployment.
- Timed exits and worker-managed stops act at poll granularity (`Q_EXECUTION_ORDER_POLL_INTERVAL_S`), not per
  tick. The operations page documents this as the one known live-versus-backtest timing difference.

### Fill tracking

- Before evaluating each completed bar, the worker reconciles the deployment with the edge:
  - working orders and their fills, by intent;
  - the position, and any broker-side exit, by position id and deal reason.

  The evaluator therefore sees the same position that the backtest would after the bar's ticks. A stop-entry fill
  and a stop-loss exit inside one bar are both recorded, in deal order.
- Between bars, the worker polls the same state every `Q_EXECUTION_ORDER_POLL_INTERVAL_S` (default 2 s), so
  fills reach the ledger, the stream and `q_terminal` promptly.
- Fills from `sl` and `tp` deals are recorded as exits with reasons `stop_loss` and `take_profit`.
- Recovery after a restart rebuilds working orders and protective levels from `/v1/orders` and `/v1/positions`.
  It reconciles them against the ledger before the first decision, and sends ambiguities to reconciliation as
  today.

### Deployable set

- `protective_levels`, `amend`, `resting_stop`, `resting_limit`, `cancel`, `oco`, `timed_exit` and
  `expiring_entry` join the deployable set. `priced_entry`, `priced_close` and `tick_hooks` stay outside it.
  A run that enables any exit rule in `exit_params` (trailing, breakeven, fixed or ATR stops, targets, time
  exits) is refused at deploy, because the forward path does not apply those rules.

### Parity

- On fixture bars with fixture ticks, a research backtest is compared with the forward evaluator driven by a fake
  edge that fills pending and protective orders from the same ticks by the research rules. The comparison
  covers:
  - an entry with a stop and a target;
  - an `amend`;
  - a resting stop entry that fills;
  - a resting limit entry that expires;
  - a `cancel`;
  - an OCO bracket where the second member fills first;
  - a resting entry expiring by `expire_at`;
  - a timed exit.

  The fake edge fills timed exits at the first fixture tick at or after each poll instant. With the poll interval set to the fixture's tick spacing, the two runs produce the same trades.

## Acceptance criteria

1. Against the fake `MetaTrader5`, the edge sends correct `TRADE_ACTION_DEAL` (with levels),
   `TRADE_ACTION_PENDING` (each type, with and without expiration), `TRADE_ACTION_SLTP` and
   `TRADE_ACTION_REMOVE` requests. It refuses a mismatched intent field and a duplicate intent. `/v1/orders`
   lists working orders, and deals carry `position_id` and `reason`.
2. The broker and ledger:
   - commit each new intent before dispatch;
   - resolve indeterminate pending and protective intents through lookup;
   - send unresolved ones to reconciliation.
3. Against the fake edge, a deployment entering with a stop and a target, then amending them, records the
   amended levels. A broker-side stop exit is recorded as a `stop_loss` fill.
4. A resting stop entry fills and is seen by the next decision. A resting limit entry expires. A replacement
   removes the old order before placing the new one, and `cancel` removes the working order.
5. Pause, kill switch, flatten and the day-trade close remove working orders before closing.
6. A restart with a working order and a protected position recovers both and makes the same next decision.
7. The parity test of the spec passes.
8. A run using `protective_levels` and `resting_stop` deploys, as do runs using `oco`, `timed_exit` and
   `expiring_entry`. A run using `priced_close` is still refused.
9. An OCO bracket removes its sibling on the first fill. A double fill is flattened to the intended position,
   records a risk event and pauses the deployment. A timed exit closes at the first poll after its instant. A
   Trailing and breakeven exit rules are out of scope (see Out of scope).
10. Existing edge, broker, worker, recovery and parity tests pass unchanged. `make check` passes in
   `q_contracts`, and `make contracts-check` passes in `q_backend`.

## Out of scope

- Priced (`price=`) entries and closes, order lists of priced entries, and tick hooks.
- Trailing and breakeven exit rules from `exit_params`, including worker-managed stop moves for them. Runs
  that enable one are refused at deploy. A strategy that needs a trailing stop uses `amend`.
- Broker-native trailing stops, and stop-limit orders.
- `q_terminal` changes beyond what its tables already show for new types and statuses. If a table filters
  unknown values, the implementing agent records a follow-up instead of changing the terminal.
- Real-money activation.

## Human inspection

On the MT5 demo account, the reviewer deploys the run of a fixture strategy that enters with a resting stop, a
stop loss and a target, and amends the stop once. For example, this could be a port of
`OpeningRangeBreakoutStrategy` from `experiments/strategies/wdo_strategies.py` with protective levels. They
confirm in the MT5 terminal that:

- the pending order and its expiration appear;
- the levels move on the amend;
- the broker-side exit appears in `q_terminal` with its reason.

They also deploy one OCO bracket and one timed exit, and confirm that the sibling order disappears on the first
fill and that the exit happens at the set time.
