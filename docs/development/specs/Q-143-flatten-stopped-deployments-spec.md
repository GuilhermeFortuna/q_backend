# Q-143: Flatten and archive stopped deployments

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 41 — Stopped deployment cleanup
**Depends on:** none
**Repositories:** q_backend (owner)
**Implementation plan:** [Plan](../plans/Q-143-flatten-stopped-deployments-plan.md)

## Purpose

A deployment that is stopped and still holds a queued `flatten` can never be archived. Archiving needs the
flatten to finish, and the execution worker only runs a flatten for a deployment it has running, so nothing
ever finishes it. The terminal's Delete button then fails for good, and the only way out is editing the
ledger by hand. This task makes the worker run a queued flatten whatever the deployment's lifecycle, so the
deployment can be archived.

## Current system

- `apply_deployment_action` (`api/services/execution.py`) queues `flatten` as `pending_action` on any
  deployment. It refuses `archive` with `409` while `pending_action` is set, while the net position is open, or
  while orders are unsettled.
- `ExecutionWorker._refresh_deployments` (`execution/worker.py`) builds a runtime, and leases the deployment,
  only for `running` and `paused` deployments. `_poll_body` runs a queued flatten only for the deployments that
  have a runtime, and returns early when no deployment is `running`. A paused deployment's flatten therefore
  waits until another deployment runs.
- A `stopped` deployment has no lease and no runtime, so its queued flatten is never read.
- Real case, 2026-10-30: a stopped paper deployment held a 1-lot position from a rejected order and a queued
  flatten. Delete answered `deployment has a pending action`, and nothing could clear the action.

## Behaviour

- The worker acts on a queued flatten for every non-archived deployment, whatever its lifecycle, and whether or
  not any other deployment is running. For a deployment without a runtime it takes the lease, flattens, clears
  the pending action and releases the lease. A deployment it cannot lease is skipped until the next poll.
- A queued flatten for a deployment with no open position and no working orders clears at once, without a
  broker call.
- A flatten the worker cannot price or send, for example when a paper deployment has no quote, stays queued.
  The worker records the reason as a risk event once per attempt window, not on every poll.
- Flattening does not change the lifecycle. A stopped deployment stays stopped and can then be archived.
- `archive` keeps its checks. The `409` for a pending action reads
  `deployment has a pending flatten; the worker clears it once the position is closed`, so the operator knows
  to wait rather than to retry.

## Acceptance criteria

1. A stopped paper deployment with an open position and a queued flatten is flattened by the worker with no
   other deployment running. Its pending action clears, its lifecycle stays `stopped`, and `archive` then
   succeeds.
2. The same holds for a paused deployment while no deployment is running.
3. A queued flatten on a stopped deployment with no open position clears without a broker call.
4. With no quote, the flatten stays queued, one risk event records the reason, and `archive` is refused with
   the new message.
5. A deployment leased by another worker is skipped and flattened once the lease ends.
6. Existing worker, lease and flatten tests pass unchanged.

## Out of scope

- Archiving a deployment that holds an open position without a flatten.
- Changing what `stop` does to positions.
- `q_terminal`: whether its Delete button shows the `409` detail is a separate check.

## Human inspection

With the live stack up, create a paper deployment, give it a position, stop it and press Flatten, then Delete.
The deployment leaves the active list.
