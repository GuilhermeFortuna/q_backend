# Q-137 implementation plan: Stop, limit and protective orders on MT5

> **For implementation agents:** Read the linked specification and the instructions of each repository you
> change. Launch only through `./work start Q-137 --agent <agent> --worktree` after written-plan approval and
> after Q-136 and Q-139 are Done.

**Goal:** Protective levels, amendments, resting stop and limit entries and cancellations reach MT5 through the
execution edge. Their fills are tracked, and they join the deployable set.
**Spec:** [Specification](../specs/Q-137-stop-limit-and-protective-orders-on-mt5-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `./work start` creates the task branch in `q_backend`. Create the same-named branch from `development` in
  `q_contracts`, in a worktree under `.worktrees/`. Vendor its unpushed commit with
  `make contracts CONTRACTS_REPO=<q_contracts worktree>`.
- Keep the edge's safety invariants (`schema/edge/execution.yaml`, `schema/edge/OBLIGATIONS.md`) for every new
  action: at most one submission per intent, no retries, and indeterminate outcomes resolved by lookup.
- Do not edit expected values in existing tests to make them pass.
- Tests use `tests/gateway/fake_metatrader5.py`, `tests/execution/fake_edge.py`, hand-built bars and fixture
  ticks. No MT5, Wine, Docker or desktop run.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing.

## Ordered implementation

### 1. Contract (`q_contracts`)

**Files:** `schema/edge/execution.yaml`, `schema/edge/execution/*.schema.json` (submit request, order, deal,
lookup outcome, new orders request and response), `schema/edge/examples/`, `schema/api/openapi.yaml` (order and
position fields), `COMPAT.md`, generated outputs.

- [ ] Add the submit actions, `/v1/orders`, the deal fields and the pending lookup outcomes. Bump the minor
  version, and document each action's intent rules in `execution.yaml`.
- [ ] Add order kind, prices, levels, expiration and tickets to the control API's order and position schemas.
- [ ] Regenerate, run `make check`, and commit.

### 2. Edge (`q_backend`)

**Files:** `gateway/mt5_execution_edge.py`, `tests/gateway/fake_metatrader5.py`,
`tests/gateway/test_mt5_execution_edge.py`.

- [ ] Write failing tests for acceptance 1.
- [ ] Build each action's MT5 request (`TRADE_ACTION_PENDING` with `ORDER_TIME_SPECIFIED` or `ORDER_TIME_GTC`,
  `TRADE_ACTION_SLTP`, `TRADE_ACTION_REMOVE`). Implement `/v1/orders`, the deal fields and the pending lookup.
  Advertise the new version. Commit.

### 3. Ledger and broker (`q_backend`)

**Files:** vendored contracts, `storage/db/execution_models.py`, `alembic/versions/` (new revision),
`execution/domain.py`, `execution/ledger.py`, `execution/brokers/base.py`, `execution/brokers/edge.py`,
`execution/brokers/paper.py`, `execution/brokers/routing.py`, `execution/edge_client.py`,
`execution/reconciliation.py`, `tests/execution/fake_edge.py`, `tests/execution/test_edge_broker.py`,
`tests/execution/test_ledger.py`, `tests/execution/test_reconciliation.py`.

- [ ] Vendor the contract and run `make contracts-check`.
- [ ] Write failing tests for acceptance 2.
- [ ] Add the columns and statuses with a migration, tested for upgrade and downgrade. Add the broker methods,
  with paper refusals, the edge-version check and the lookup of pending intents. Teach the fake edge to fill
  pending and protective orders from fixture ticks by the research rules. Commit.

### 4. Order mapping and fill tracking (`q_backend`)

**Files:** `execution/research_evaluator.py`, `execution/worker.py`, `execution/service.py`,
`execution/recovery.py`, `execution/commands.py`, `execution/validation.py`, `storage/settings.py`,
`tests/execution/test_worker_on_edge.py`, `tests/execution/test_research_evaluator.py`,
`tests/execution/test_research_parity.py`.

- [ ] Write failing tests for acceptance 3–8.
- [ ] Map research orders as the spec's table states, including OCO sibling removal and the double-fill guard,
  timed exits and worker-managed trailing and breakeven stops. Widen the deployable set.
- [ ] Reconcile before each decision, and poll between bars with `Q_EXECUTION_ORDER_POLL_INTERVAL_S`.
- [ ] Remove working orders on pause, stop, kill switch, flatten and the day-trade close.
- [ ] Recover working orders and levels at start.
- [ ] Extend the parity test with ticks, covering every case in the spec. Run `tests/execution/` and `tests/gateway/test_mt5_execution_edge.py`,
  then `ruff`/`black` on the changed files. Commit.

### 5. Documentation (`q_backend`)

**Files:** `docs/research/user_guide/strategy_files.rst`, `docs/research/user_guide/execution.rst`,
`docs/operations/mt5-demo-trading.md`.

- [ ] Document the order mapping and the deployable set. Explain how to express a priced stop as `stop_loss` and
  `amend`, and how broker-side exits appear. Commit.

## Verification

- `q_contracts`: `make check`.
- `q_backend`: `uv run pytest tests/execution tests/gateway/test_mt5_execution_edge.py`, the execution API tests,
  `make contracts-check`, and `ruff`/`black` on the changed files.
- `./work board set Q-137 in-review -m "<summary per repository; checks and results; follow-ups>"`.
