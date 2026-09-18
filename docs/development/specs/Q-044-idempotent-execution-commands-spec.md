# Q-044: Idempotent execution commands

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.5, §6.1, §8.1, §10 Phase 4](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#45-control-path)  
**Depends on:** Q-039  
**Implementation plan:** [`../plans/Q-044-idempotent-execution-commands-plan.md`](../plans/Q-044-idempotent-execution-commands-plan.md)

## Purpose

Operators will command live deployments from `q_terminal`: start, pause, stop,
flatten, the kill switch, resolving an unknown order, deploying a strategy.
Those requests cross a network that can time out after the server has already
acted. A retried flatten is harmless only by accident. A retried "resolve as
filled" would apply a fill twice, and a retried "create deployment" creates two.
§4.5 requires every command to carry a client-generated idempotency key, with
the server returning the stored result on retry for 24 hours. API FINDINGS 3
records that none do. This task implements the Q-039 idempotency policy on
every execution command.

## Requirements

- Every command listed as requiring a key in the Q-039 policy refuses a
  request without one, with the declared error code and status, before any
  state changes.
- The first request with a key executes normally. Its status code and body are
  stored under the key in the same transaction as the command's state change,
  so that the stored result exists if and only if the command committed.
- A later request with the same key, method, path and body returns the stored
  status and body without executing again, for 24 hours, and says that it is a
  replay.
- A later request with the same key and a different method, path or body is
  refused with the key-reuse error, and executes nothing.
- Two concurrent requests with the same key execute the command at most once.
  The other request receives the stored result, or a retryable conflict if the
  first has not yet committed.
- A command that fails validation or is refused by a business rule stores its
  error result like any other result, so a retry gets the same answer.
- A command that fails because a dependency is unavailable (`503`) stores
  nothing, so the client can retry with the same key once the dependency returns.
- Stored results older than 24 hours are removed by a periodic prune. A key
  whose result has been pruned is treated as new.
- The OpenAPI capture in `q_contracts` shows the header on every covered command,
  and API FINDINGS 3 is closed.

## Constraints and non-goals

- **Execution commands only.** Research job submission (backtest, optimization
  and the rest) is not covered. Its duplicate-job risk is real, but its
  consumers are the research UI and its own job model. That goes in the
  FINDINGS entry as the remaining scope.
- **No change to what any command does.** Only its retry behaviour changes.
- **`q_frontend` is not updated.** Its execution workspace is removed in Q-050.
  Until then it keeps working, because the header will be required only once
  the frontend's commands send it, or once the frontend workspace is gone: see
  the plan's enforcement flag.

## Acceptance criteria

### Agent-verifiable

1. Each covered command without a key is refused with
   `idempotency_key_required`, and no state or outbox event changes.
2. Each covered command repeated with the same key and body returns the
   identical status and body, marked as a replay, and produces no second state
   change and no second outbox event.
3. The same key with a different body or path is refused with
   `idempotency_key_reused`.
4. Two concurrent identical requests for "resolve as filled" apply exactly one
   fill.
5. A command whose transaction rolls back stores no result. A `503` stores no
   result, and a retry with the same key executes once the database is back.
6. A business-rule refusal (an illegal lifecycle transition) is stored and
   replayed.
7. The prune removes results older than 24 hours and keeps younger ones.
8. With the enforcement flag off, a missing key is accepted and logged. With it
   on, it is refused. The shipped default is documented.
9. The recaptured OpenAPI declares the header on every covered operation, a
   `q_contracts` validation check confirms that against the policy file, and
   `make contracts-check` passes.
10. The full validation suite passes.

### Human-verifiable

1. Against `./research`, the same flatten request sent twice with one key
   returns the same body both times with the replay marker, and the audit log
   shows one flatten.
   Command: `k=$(uuidgen); for i in 1 2; do curl -si -X POST http://127.0.0.1:8000/api/v1/execution/deployments/<id>/actions -H "Idempotency-Key: $k" -H 'content-type: application/json' -d '{"action":"flatten","confirm":true}'; done`
