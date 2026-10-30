# MT5 Demo Trading

Forward tests run on an MT5 demo account through the execution edge instead of Q's paper broker.
This page covers the settings that enable demo orders, how the account's trade mode decides the
controlled-account gate, how to confirm the capability, and why a real account stays locked.

The gate is defined in `q_backend/execution/brokers/live_gates.py`. The edge reports the trade mode
in `GET /v1/account` (`q_contracts`, `schema/edge/execution/account-response.schema.json`).

## Settings

The execution worker submits orders, and the API creates the deployments and reports health. Both
read the same environment (`.env` or the process environment).

| Setting | Value for demo trading | Purpose |
|---------|------------------------|---------|
| `Q_LIVE_EXECUTION_ENABLED` | `true` | Global enable for MT5 live execution. |
| `Q_LIVE_EXECUTION_ACCOUNT_ALLOWLIST` | the demo login, for example `10987654` | Logins that may receive orders. Comma-separated. |
| `Q_EXECUTION_LIVE_CAPABILITY_LOCKED` | `false` | The API refuses to create an `mt5_live` deployment with live activation while this is `true`. |
| `Q_EXECUTION_PAPER_ONLY` | `false` | The paper-only profile refuses `mt5_live` deployments. |
| `Q_LIVE_EXECUTION_VALIDATED` | `false` | Leave unset for demo trading. See [Real accounts stay locked](#real-accounts-stay-locked). |
| `Q_MT5_EDGE_URL` | `http://127.0.0.1:18813` | Execution edge base URL. |

Then create a deployment with `broker_mode` set to `mt5_live` and `live_activation_enabled` set to
`true`. Its market orders go to the demo account through the edge.

## How the trade mode decides the gate

Gates one to four are unchanged: the global enable flag, a connected account, the allowlist, and the
deployment's live activation flag. The fifth gate depends on the trade mode that the terminal reports
for the account:

| Trade mode reported by the account | Passes the fifth gate |
|------------------------------------|-----------------------|
| `demo` | Yes, without `Q_LIVE_EXECUTION_VALIDATED`. |
| `contest` | Only when `Q_LIVE_EXECUTION_VALIDATED=true`. |
| `real` | Only when `Q_LIVE_EXECUTION_VALIDATED=true`. |
| Not reported (an older edge) or an unrecognised value | Only when `Q_LIVE_EXECUTION_VALIDATED=true`. |

The trade mode is read from the same `/v1/account` call as the login, immediately before each order.
If the terminal switches from the demo login to a real login, the next order is rejected with
`live_locked`, and the rejection names the mode the account reports.

## Confirm the capability

`GET /api/v1/execution/health` reports whether MT5 orders are possible on the connected account in
its `mt5_orders` field:

```bash
curl -s http://127.0.0.1:8000/api/v1/execution/health | jq .mt5_orders
```

For a connected demo account the result is:

```json
{
  "capability": "available",
  "reason": null,
  "account": {"login": 10987654, "server": "MetaQuotes-Demo", "trade_mode": "demo"}
}
```

| `capability` | Meaning |
|--------------|---------|
| `available` | The account passes the gates that health evaluates. Orders still need an activated deployment. |
| `validation_required` | The account is `contest`, `real` or unreported, and `Q_LIVE_EXECUTION_VALIDATED` is not set. |
| `locked` | A gate closed before the fifth, or the edge is unreachable or did not answer in time. `reason` names the gate. |

Health does not evaluate the deployment's activation flag, because that belongs to each deployment.
`account` is `null` when the edge cannot be reached.

## Real accounts stay locked

A real or contest account stays `validation_required` until an operator sets
`Q_LIVE_EXECUTION_VALIDATED=true` deliberately, after the controlled-account sign-off in the README.
That setting also lets every allowlisted login with a non-demo trade mode place orders, so keep the
allowlist to the validated login only.

To stop all submissions at once, set `Q_LIVE_EXECUTION_ENABLED=false`. Every account then reports
`locked`.

## Deploying research strategy versions

In `q_terminal`, the deploy dialog lists saved strategy-file runs from the research stack:

1. **Selection & Badging**: When `mt5_orders.capability` is `available`, the dialog offers `MT5 LIVE`
   and preselects it by default, displaying the demo account login and a `DEMO` badge.
2. **Eligibility Requirements**: The saved run must record only deployable order kinds (`market_entry`,
   `market_close`), use a timeframe of M5 or higher, and have a day-trading session configured.
   Ineligible runs are greyed out with an explanation.
3. **Session Timing**: Ensure the backtest run's day-trade `close_time` is set inside active broker
   trading hours (for instance 18:15 for `IND$N` or 17:00 for `WIN$N`), ensuring day-trade close
   market orders are filled before trading closes.
4. **Parameter Edits**: The deploy and edit dialogs support editing typed `bool` (checkboxes) and
   `time` (`HH:MM`) strategy parameters within their declared bounds.

## Resting and protective orders

A demo deployment can hold stop and limit entries, protective levels and OCO groups at
the broker. What to look for in the MT5 terminal:

- A resting entry appears as a pending order with its expiration (good-till-cancelled when
  the strategy gave none). It disappears when it fills, expires or is removed by Q.
- A stop or target set on a market entry appears as the position's stop-loss and
  take-profit. An `amend` moves them; the change is recorded in the ledger.
- A broker-side stop or target exit appears in `q_terminal` with its reason (`stop_loss`
  or `take_profit`). Q records it as the close of the position.

Timing: working orders are reconciled every `Q_EXECUTION_ORDER_POLL_INTERVAL_S` seconds
(default 2). Timed exits and their closes act at that poll granularity, not on every tick.
This is the one known timing difference between a live demo run and its backtest.

Limits in this release: trailing and breakeven exit rules are not deployable, and a
partly filled resting order is recorded only once the terminal reports it filled.

