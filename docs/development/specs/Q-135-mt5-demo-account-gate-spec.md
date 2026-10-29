# Q-135: MT5 demo-account gate

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 38 — Research strategies on MT5
**Depends on:** none
**Repositories:** q_backend (owner), q_contracts
**Implementation plan:** [Plan](../plans/Q-135-mt5-demo-account-gate-plan.md)

## Purpose

Forward tests move from Q's paper broker to an MT5 demo account. Today the four live gates cannot tell a demo
account from a real one. To trade on the demo, the operator would have to set `Q_LIVE_EXECUTION_VALIDATED`,
which then also unlocks real money for any allowlisted login. This task makes the account's own trade mode part
of the gate: demo orders flow without the controlled-account validation, and real orders still need it.

## Current system

- `evaluate_live_gates` (`src/q_backend/execution/brokers/live_gates.py`) rejects in order:
  1. `Q_LIVE_EXECUTION_ENABLED` false;
  2. no connected account;
  3. the login missing from `Q_LIVE_EXECUTION_ACCOUNT_ALLOWLIST`;
  4. the deployment's `live_activation_enabled` false;
  5. `Q_LIVE_EXECUTION_VALIDATED` false.

  `EdgeBroker.submit_market_order` (`execution/brokers/edge.py`) evaluates them before every submission.
- `/v1/account` (`schema/edge/execution/account-response.schema.json`, implemented in
  `gateway/mt5_execution_edge.py`) returns login, server, currency, the trade-allowed flags, balance, equity and
  free margin. It omits `trade_mode`, which `mt5.account_info()` provides (0 demo, 1 contest, 2 real).
- `GET /api/v1/execution/health` reports MT5 connectivity but not whether MT5 orders are possible, so
  `q_terminal`'s deploy dialog shows MT5 as locked unconditionally.
- `Settings.live_execution_dry_run` is defined and read nowhere.

## Behaviour

### Edge contract (`q_contracts`)

- The account response gains a required `trade_mode`: `demo`, `contest` or `real`. This is an additive minor
  version of the execution-edge contract; the schema major stays 1. `COMPAT.md` records the version.
- The edge maps `account_info().trade_mode` 0, 1 and 2 to these values, and reports any other value as `real`.

### Gate (`q_backend`)

- The fifth gate reads: an account whose trade mode is `demo` passes. A `contest` or `real` account, or a
  response without `trade_mode` (an older edge), passes only when `Q_LIVE_EXECUTION_VALIDATED` is true.
- The rejection for a validated-only account says which mode the account reports. Its capability stays
  `live_locked`.
- Gates one to four are unchanged. A demo login must still be on the allowlist.
- The trade mode is read from the same `/v1/account` call as the login, immediately before each submission, so a
  terminal that switches accounts cannot carry a demo decision over to a real login.
- The dead `live_execution_dry_run` setting is removed.

### Health and capability (`q_contracts`, `q_backend`)

- `ExecutionHealthResponse` gains `mt5_orders`, with:
  - `capability`: `available`, `validation_required` or `locked`;
  - `reason`: the text of the first failing gate, or null;
  - `account`: login, server and trade mode, or null when the edge or terminal is unreachable.

  The deployment gate is not evaluated here. The health endpoint answers whether an MT5 deployment *could* trade
  on the connected account.
- The edge account call has the existing edge read timeout. A timeout or an unreachable edge reports `locked`,
  with the reason.

### Documentation

- `docs/operations/` gains `mt5-demo-trading.md`, covering:
  - the settings to enable demo orders (enable flag, allowlist, deployment activation);
  - how the trade mode decides the fifth gate;
  - how to confirm the capability through `GET /api/v1/execution/health`;
  - that a real account stays locked until `Q_LIVE_EXECUTION_VALIDATED` is set deliberately.

## Acceptance criteria

1. Against the fake `MetaTrader5`, `/v1/account` reports `demo`, `contest` and `real` for trade modes 0, 1 and 2,
   and `real` for an unknown value.
2. With gates one to four open and `Q_LIVE_EXECUTION_VALIDATED` false:
   - a demo account's market order reaches the fake edge;
   - a real account's, a contest account's and a mode-less account's orders are rejected with `live_locked`,
     naming the reported mode.
3. With `Q_LIVE_EXECUTION_VALIDATED` true, a real account's order reaches the fake edge.
4. When the fake terminal switches from a demo login to a real login between two submissions, the second is
   rejected.
5. `GET /api/v1/execution/health` reports `available`, `validation_required` and `locked` (gate closed, edge
   unreachable) for the corresponding fakes, with account and reason.
6. Existing edge, gate and worker tests pass unchanged. `make check` passes in `q_contracts`, and
   `make contracts-check` passes in `q_backend`.

## Out of scope

- Setting `Q_LIVE_EXECUTION_VALIDATED`, and any real-money activation.
- Deployment and terminal changes (Q-136) and order types beyond market orders (Q-137).
- Retiring the paper broker.

## Human inspection

With `./dev live` connected to the MT5 demo account and the demo login allowlisted, the reviewer checks that
`GET /api/v1/execution/health` reports `available` with trade mode `demo`. They then submit one market order from
an `mt5_live` test deployment of a registry strategy, close it, and confirm both deals in the MT5 terminal.
