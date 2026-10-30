# Q-143 implementation plan: Flatten and archive stopped deployments

> **For implementation agents:** Read the linked specification and `q_backend`'s instructions. Launch only through
> `./work start Q-143 --agent <agent> --worktree` after written-plan approval.

**Goal:** a queued flatten runs on a stopped or paused deployment, so the deployment can be archived.
**Spec:** [Specification](../specs/Q-143-flatten-stopped-deployments-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- `q_backend` only. No schema change and no contract change.
- Backend environment: `UV_PROJECT_ENVIRONMENT=/mnt/build/venvs/<name>`, also when committing.

## Ordered implementation

### 1. Tests

**Files:** `tests/execution/test_worker_flatten_stopped.py` (new), beside `test_worker.py` and
`test_worker_position_sync.py`.

- [ ] Write failing tests for acceptance 1 to 5 against the fake edge and the paper broker, and commit.

### 2. Worker

**Files:** `src/q_backend/execution/worker.py`, and the lease helpers it uses.

- [ ] Add a pass in `_poll_body`, before the early return, that lists non-archived deployments with a pending
  flatten and no runtime. Lease each one, run `service.flatten_deployment`, clear the pending action and
  release the lease. Keep the existing runtime path for deployments that have one.
- [ ] Clear a flatten at once when the deployment has no open position and no working orders.
- [ ] Keep an unpriceable flatten queued and record one risk event per attempt window. Commit.

### 3. Message

**Files:** `src/q_backend/api/services/execution.py`, `tests/api/test_execution_api.py`.

- [ ] Reword the pending-action `409` and update its test. Commit.

## Verification

- `uv run pytest tests/execution tests/api/test_execution_api.py`, and `ruff`/`black` on the changed files.
- `./work board set Q-143 in-review -m "<summary; checks and results; follow-ups>"`.
