# Batch 42 — Market replay

**Status:** proposed specifications/plans awaiting human review; the Q project board is the status of record.

## Outcome

Operate the normal live stack and q_terminal against a separately controlled historical market and simulated
demo broker. Quotes keep moving while the stack computes and broker transport/acceptance/execution/response
latency elapses. Replay controls stay outside the live terminal and control API: in the CLI and, from Q-146,
the Research UI.

| Task | Owner | Dependencies | Deliverable |
| --- | --- | --- | --- |
| Q-144 | q_core | Q-138 (Done) | Released incremental execution primitives; preserve existing research results |
| Q-145 | q_backend | Q-144, Q-136 and Q-137 (latter two Done) | Standalone replay service, normal edge/gateway paths, clocks, isolated launcher and demo deployment support |
| Q-146 | q_backend | Q-145 | Research UI page that prepares, starts, drives and shuts down replay runs |

Q-144 needs its own task because q_core releases only through the human finish workflow. Q-145 pins that
released date tag; a task-local native dependency is not a production delivery mechanism.

## Documents and implementation launch

Q-144 spec/plan belong in q_core at `docs/development/{specs,plans}/Q-144-incremental-execution-primitives-*`.
Q-145 [spec](../specs/Q-145-market-replay-and-simulated-broker-spec.md) and
[plan](../plans/Q-145-market-replay-and-simulated-broker-plan.md) belong in q_backend.
Planning branches in both owners: `docs/batch-42-market-replay`. They are local and not pushed.
Q-146 [spec](../specs/Q-146-research-replay-control-spec.md) and
[plan](../plans/Q-146-research-replay-control-plan.md) belong in q_backend, on branch
`docs/q-146-research-replay-control`; its issue is https://github.com/GuilhermeFortuna/q_backend/issues/67.
Issues embed the full written documents so remote review does not rely on unavailable branch links.

After approval, the human makes Q-144 Todo and launches `./work start Q-144 --agent <agent> --worktree`.
Before launching either task, integrate its approved planning commit into its owner's development branch
through the human workflow: `./work start` checks for documents in the owner checkout before creating its
implementation branch. Finish/release Q-144 before making Q-145 Todo and launching it the same way.
Q-145 spans q_backend, q_contracts, q and q_terminal: create its exact printed task branch from development
in each additional repository's worktree and report all changes in one board message. No replay code is
implemented by this planning batch, and no task is agent-approved as Todo.

## Decisions requiring review

- Quote-based broker fills differ explicitly from trade-based research limits/targets.
- Fixed latency components run in historical market time; actual stack processing contributes measured delay.
- Interactive replay has no consumer barrier; recorded ingress is reproducible, independent sessions may vary.
- Historical market clocks are separate from operational health/leases/deadlines.
- Initial control surface is CLI, with no replay controls in the live terminal/API. Q-146 adds a Research UI page;
  strategy deployments stay in q_terminal.
- Full top-of-book fills and simple declared margin are initial models; no liquidity/depth or partial fills.
- First version includes pending/protective/cancel/expiry behavior, durable reconciliation and ordinary demo
  deployment support, not merely a paper-data playback demonstration.
