# Q-135 implementation plan: MT5 demo-account gate

> **For implementation agents:** Read the linked specification and the instructions of each repository you
> change. Launch only through `./work start Q-135 --agent <agent> --worktree` after written-plan approval.

**Goal:** A demo account passes the controlled-account gate on its own trade mode; real orders still need
`Q_LIVE_EXECUTION_VALIDATED`, and the health endpoint reports whether MT5 orders are possible.
**Spec:** [Specification](../specs/Q-135-mt5-demo-account-gate-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `./work start` creates the task branch in `q_backend`. Create the same-named branch from `development` in
  `q_contracts`, in a worktree under `.worktrees/`. Vendor its unpushed commit with
  `make contracts CONTRACTS_REPO=<q_contracts worktree>`.
- Test through `tests/execution/fake_edge.py` and `tests/gateway/fake_metatrader5.py`. No MT5, Wine, Docker or
  desktop run.
- Do not edit expected values in existing tests to make them pass.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing.

## Ordered implementation

### 1. Contract (`q_contracts`)

**Files:** `schema/edge/execution/account-response.schema.json`, `schema/edge/execution.yaml`,
`schema/edge/examples/`, `schema/api/openapi.yaml`, `COMPAT.md`, generated outputs.

- [ ] Add the required `trade_mode` enum to the account response, and bump the execution-edge minor version.
- [ ] Add `mt5_orders` (capability, reason, account) to `ExecutionHealthResponse`, with examples.
- [ ] Record both in `COMPAT.md`. Regenerate, run `make check`, and commit.

### 2. Edge (`q_backend`)

**Files:** `gateway/mt5_execution_edge.py`, `tests/gateway/fake_metatrader5.py`,
`tests/gateway/test_mt5_execution_edge.py`.

- [ ] Write failing tests for acceptance 1.
- [ ] Map `trade_mode` in `EdgeApp.account` and advertise the new schema version. Run the gateway tests. Commit.

### 3. Gate and health (`q_backend`)

**Files:** vendored contracts, `execution/brokers/live_gates.py`, `execution/brokers/edge.py`,
`execution/edge_client.py`, `storage/settings.py`, `api/services/execution.py`, `api/schemas/execution.py`,
`tests/execution/test_edge_broker.py`, `tests/execution/fake_edge.py`, `tests/api/` health tests.

- [ ] Vendor the contract and run `make contracts-check`.
- [ ] Write failing tests for acceptance 2–5.
- [ ] Parse `trade_mode` in the edge client's account model; a missing value is `None`.
- [ ] Pass the trade mode to `evaluate_live_gates` and implement the fifth gate as the spec states.
- [ ] Add a gate evaluation that is independent of any deployment for the health endpoint, and build
  `mt5_orders` from it. Use the edge read timeout.
- [ ] Remove `live_execution_dry_run` and any reference in env examples.
- [ ] Run the new tests, `tests/execution/`, `tests/gateway/test_mt5_execution_edge.py`, and `ruff`/`black` on
  the changed files. Commit.

### 4. Documentation (`q_backend`)

**Files:** `docs/operations/mt5-demo-trading.md` (new), `README.md` (link).

- [ ] Write the page the spec lists. Commit.

## Verification

- `q_contracts`: `make check`.
- `q_backend`: `uv run pytest tests/execution tests/gateway/test_mt5_execution_edge.py` and the health tests,
  `make contracts-check`, `uv run ruff check` and `uv run black --check` on the changed files.
- `./work board set Q-135 in-review -m "<summary per repository; checks and results; follow-ups>"`.
