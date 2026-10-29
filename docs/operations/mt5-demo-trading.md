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
