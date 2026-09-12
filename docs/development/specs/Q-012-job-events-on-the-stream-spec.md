# Q-012: Job events on the stream

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.1, §4.4, §10 phase 1](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#10-roadmap)  
**Depends on:** Q-011  
**Implementation plan:** [`../plans/Q-012-job-events-on-the-stream-plan.md`](../plans/Q-012-job-events-on-the-stream-plan.md)

## Purpose

Nine kinds of background job report progress by overwriting a Redis key that
the research UI polls every one to two seconds. That key is also the only place
several of them record that they finished. A job that completes between two
polls, or whose Redis write fails, looks to the UI as if it is still running,
and nothing tells the UI otherwise. This task makes every job kind the first
real producer on the stream. Progress goes onto the ephemeral progress topic,
where it may be coalesced. Each job run's single terminal outcome goes onto the
durable terminal topic, committed with the state change wherever that state
change is in Postgres. The frontend's move off polling (Q-016) needs both, and
the relay and outbox built in Q-010 and Q-011 get their first test against real
workloads.

## Requirements

### Progress

- Every progress update any job kind writes today is also published on the
  progress topic, keyed by job, in the stream vocabulary Q-009 defined.
- A job kind whose progress is not a fraction publishes no fraction. It publishes
  its textual progress as a message.
- A failure to publish progress never fails, slows beyond a bounded amount, or
  retries the job. Progress is observational.
- Progress is not published for a job after its terminal event has been
  recorded, so a late progress update cannot appear to reopen a finished job.

### Terminal outcome

- Every job run of every kind produces exactly one terminal event — completed,
  failed, or cancelled — including runs cancelled by the startup reconciliation
  of orphaned jobs.
- Where a job's state lives in Postgres, its terminal event commits in the same
  transaction as the state change that makes it terminal.
- Where a job's state lives only in Redis, its terminal event is committed to
  Postgres at the terminal transition, and becomes the job's only durable record
  of having finished.
- A job that is retried by the worker pool, finalized twice, or reconciled after
  it already finished does not produce a second terminal event.
- A terminal event carries the failure message for failed runs and never carries
  results.

### Vocabulary

- Every status spelling in use across job kinds today is mapped to exactly one
  stream status. The mapping is total: an unmapped spelling is a test failure,
  not a runtime default.
- The optimization job kind, which today reports under a generic namespace, is
  published under its own kind.

### Preserved behavior

- Every REST job status and results endpoint returns the same payloads as
  before. The existing progress keys are still written and read.
- No job's runtime, retry policy, or outcome changes.

## Constraints and non-goals

- **No change to the REST status payloads.** They stay inconsistent (Finding 5)
  until the frontend no longer depends on their shapes for live updates. Fixing
  them now would change eight response models in the same change that adds a
  new transport.
- **No removal of the progress keys.** The REST status endpoints read them, and
  the frontend's polling fallback (Q-016) reads those endpoints when the stream
  is down.
- **No per-trial or per-window events.** Optimization trials and walk-forward
  windows are job-internal. Publishing them would multiply the progress rate by
  the trial count, for a view no surface has asked for.
- **No execution topics.** Deployments, orders, and fills are phase 4.
- **No progress rate limiting beyond what exists.** Coalescing at the endpoint
  (Q-014) is what bounds what a client receives. If publish volume proves to be a
  problem, that is a measured follow-up.

## Acceptance criteria

### Agent-verifiable

1. For each of the nine job kinds, a job run to completion under the synchronous
   job harness produces at least one progress entry and exactly one terminal
   event with status completed.
2. For each job kind that can fail, a forced failure produces exactly one terminal
   event with status failed and the failure message.
3. For each job kind that can be cancelled, cancellation produces exactly one
   terminal event with status cancelled.
4. Startup reconciliation of an orphaned running job produces one cancelled
   terminal event, and running reconciliation again produces none.
5. Finalizing the same job twice produces one terminal event.
6. For a job kind whose state is in Postgres, a terminal transition whose
   transaction rolls back leaves no terminal event.
7. With Redis unavailable, every job kind still runs to its terminal state, and
   its terminal event is committed to the outbox.
8. Every status literal written by any job manager is covered by the status
   mapping, checked by a test that scans the job managers' sources.
9. No progress entry for a job is published after its terminal event.
10. The REST status and results responses for each job kind are unchanged,
    compared against their pre-task payloads.
11. The full validation suite passes.

### Human-verifiable

1. With the API, a research worker, and the relay running, a backtest and an
   optimization started from the research UI produce progress and terminal
   entries visible on the streams, and the UI behaves as before.
   Command: `redis-cli -p 6380 XRANGE q:stream:jobs.terminal - + COUNT 20` and `XRANGE q:stream:jobs.progress - + COUNT 50`
2. Publishing overhead is measured on a real optimization dispatched through the
   API and the worker pool, the path that publishes progress: the same study run
   three times before and three times after the change, reporting wall-clock
   time for each.
   Command: `uv run python scripts/bench_job_overhead.py --kind optimization --runs 3`
