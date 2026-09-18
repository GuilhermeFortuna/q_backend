# Q-041: Evaluator follows positions while running

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.3, §6.1, §9 invariants 1 and 4, §10 Phase 4](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#61-fail-closed-restated)  
**Depends on:** Q-031  
**Implementation plan:** [`../plans/Q-041-evaluator-follows-positions-while-running-plan.md`](../plans/Q-041-evaluator-follows-positions-while-running-plan.md)

## Purpose

FINDINGS item 18: the forward evaluator learns about an open position only when
the worker builds a deployment's runtime at startup. A position opened while the
worker runs is invisible to exit rules and strategy exits until the next
restart, and a position closed while it runs still looks open. For a paper
deployment that is a wrong result. For a live one it is an unmanaged position.
Q-031 triaged it as a task that must land before live activation, and phase 4
wires live execution. This task makes the evaluator follow the deployment's
net position after every change the worker makes or observes. It changes no
decision the evaluator would have made had it known.

## Requirements

- After a fill is applied for a deployment, whether from a bar decision, a
  flatten, or a reconciliation that resolves an unknown order as filled, the
  evaluator's open trade is set from the deployment's net position before the
  next bar is evaluated. A position that went flat clears the open trade.
- When several completed bars reach a deployment in one poll, each bar's
  decision is processed before the next bar is evaluated. The evaluator
  therefore never decides bar *n+1* without the outcome of bar *n*.
- An open trade's identity is unique per position, not per deployment. A new
  position after a flat one never inherits exit-rule state from the previous
  position. The identity is derived from the position's durable fields, so a
  restart reconstructs the same identity and the same exit-rule state.
- A restarted worker and a worker that never restarted emit the same decisions
  for the same bars and fills, including after a position opens and closes
  while running.
- Nothing else changes: the evaluator's decisions for a given open trade,
  sizing, the risk gate, and the order and ledger paths.

## Constraints and non-goals

- **No change to how holding periods match entry bars.** FINDINGS item 3 (live
  fill times rarely equal a bar timestamp) is a separate semantic change with
  its own review.
- **No change to evaluator sizing** (FINDINGS item 6).
- **No broker change.** The edge and the live path are Q-042. This task is
  proven with the paper broker, which is the path both brokers share after a
  fill.
- **No change to the evaluator's window or its q_core step.**

## Acceptance criteria

### Agent-verifiable

1. A worker test with the paper broker opens a position on bar *n* and shows
   that a trailing stop closes it on a later bar without a restart. The same
   test on today's code never closes it.
2. After a flatten processed by the worker, the evaluator's open trade is
   cleared, and the next entry signal opens a new position.
3. After a reconciliation resolves an unknown order as filled, the evaluator's
   open trade reflects the reconciled position before the next bar.
4. With two completed bars in one poll, where the first fills an entry and the
   second would trigger an exit, the second bar's decision is the exit.
5. Two positions opened one after the other have different trade identities,
   and the second starts with empty exit-rule state.
6. A restart in the middle of the second position reproduces the next
   decision of an uninterrupted run, and fails if replay is skipped.
7. `tests/execution`, the parity test and the goldens pass with unchanged
   expected values, except where a test pinned item 18's behaviour. Each such
   test is listed and justified.
8. FINDINGS item 18 is marked resolved by this task.
9. The full validation suite passes.

### Human-verifiable

1. A paper deployment of MACrossover with a trailing stop on `WIN$N` M1 opens
   a position and later exits on `trailing` without a worker restart. The
   decision log shows both, with no evaluation error.
   Command: `uv run q-execution --log-level INFO run 2>&1 | tee worker.log`, then
   `curl -s http://127.0.0.1:8000/api/v1/execution/deployments/<id>/decisions | jq`
