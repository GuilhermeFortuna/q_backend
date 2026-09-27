# Q-067 implementation plan: Catalog-based paper deployments

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-067-catalog-based-paper-deployments-spec.md`](../specs/Q-067-catalog-based-paper-deployments-spec.md)  
**Depends on:** Q-066

## Current-system context

`api/services/execution.py::create_deployment` chooses saved-run or
client-built identity. `execution/validation.py` checks the hash, name and
timeframe, but not registry parameter bounds or evaluator buildability.
`execution/strategy_build.py` builds from `BacktestRequest`, and
`backtesting/strategy_registry.py` exposes `StrategyParamSpec`. Custom
wrappers are mutable records in `data/custom_strategies.json`.
`ExecutionDeployment` and `ExecutionDecision` already store compiled-config
snapshots; they lack configuration revision and paper costs. The worker keeps
a runtime while paused, so it must notice a revision change on resume.

## Interfaces produced

```text
GET   /api/v1/execution/strategy-catalog
POST  /api/v1/execution/deployments         + catalog: CatalogDeploymentInput
PATCH /api/v1/execution/deployments/{id}/configuration
```

`CatalogDeploymentInput` contains `strategy_name`, `strategy_params`,
`exit_params`, `symbol`, `timeframe`, `sizing_config`, `risk_config` and
`paper_cost_config`. The PATCH body contains `expected_revision: int`,
`actor: str`, and complete replacement values for those four mutable config
groups; it excludes strategy, symbol and timeframe. Its success response is
`DeploymentDetailResponse` with `config_revision` and `paper_cost_config`.
Both mutating routes use the existing idempotent command mechanism.

Persistence: add `execution_deployment_revisions` keyed by
`(deployment_id, revision)` with the full resolved identity, source name/kind,
paper costs, actor and creation time. Add current `config_revision`,
`paper_cost_config`, and `activation_cutoff_at` to the deployment projection;
add `config_revision` and `paper_cost_config` to decisions. Keep the current
`config_hash` algorithm for compiled config; revision distinguishes cost-only
and sizing-only changes.

## Implementation decisions

- The API resolves and validates catalog selections. QML never builds a
  `BacktestRequest` or computes a hash.
- Store the expanded built-in base name and all resolved defaults in
  `compiled_config`. Store the custom wrapper name only as provenance, so a
  later wrapper edit cannot alter evaluation.
- The deployment row is the current projection; immutable revision rows are
  the audit history. Insert a revision and update the projection under a row
  lock in the idempotent command transaction.
- Validate a known symbol using provider metadata, not a currently fresh
  quote. Runtime quote freshness remains a Q-068/risk-gate concern.
- Persist `activation_cutoff_at` at every start/resume. Recovery reconstructs
  indicator state from bars at or before that point, but processes only bars
  closing later. On detecting a new revision, discard the old evaluator and
  rebuild from the revision/current projection before processing another bar.

## Ordered implementation

- [x] 1. On the Q-067 task branch, pin and vendor Q-066; run
  `make contracts-check`. Add failing catalog eligibility/default tests in
  `tests/api/test_execution_api.py`, including custom-wrapper mutation after
  creation.
- [x] 2. Add focused catalog compilation and parameter validation in
  `execution/validation.py` and `execution/strategy_build.py`; expose the
  catalog route. Prove rejected inputs write no deployment or outbox event.
- [x] 3. Add migration and model/repository support for current revision,
  immutable revision rows, costs and activation cutoff. Test row locking,
  revision uniqueness, immutable history, rollback and event emission in
  `tests/storage/test_execution_repositories.py`.
- [x] 4. Extend create and add the idempotent PATCH route in the execution
  router/service. Test draft, paused-flat, running, open position, pending
  unknown, stale revision and same-key retry in `tests/api/`.
- [x] 5. Update decision snapshots and worker activation/recovery logic.
  Test no catch-up orders across pause, edit, resume and restart; test the
  first new decision carries the new revision and costs.
- [x] 6. Recapture backend OpenAPI in `q_contracts` through its normal capture
  workflow, verify the catalog/edit routes and `Idempotency-Key`, run
  `make contracts-check` and `./scripts/ci.sh`, then commit focused changes
  on the task branch. Coordinate the contract capture with the Q-066 pin.
  (OpenAPI recapture itself lands as a separate `q_contracts` commit outside
  this repo/worktree's authorization — see review notes below.)

## Review focus

- The explicit-identity and saved-run paths must not bypass buildability.
- A custom wrapper edit must not alter a deployed strategy after restart.
- Concurrent PATCH requests must create at most one next revision.
- Resume must not submit signals from bars closed during the pause.
- A cost-only edit must retain old decisions' cost snapshots.
