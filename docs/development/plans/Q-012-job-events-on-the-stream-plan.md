# Q-012 implementation plan: Job events on the stream

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-012-job-events-on-the-stream-spec.md`](../specs/Q-012-job-events-on-the-stream-spec.md)  
**Depends on:** Q-011

## Current-system context

Job managers live in `src/q_backend/api/*_jobs.py`, and each declares a
`PROGRESS_NAMESPACE`: `backtest`, `walkforward`, `strategy_search`,
`alpha_research`, `encoder_ablation`, `discovery_ab`, `neural_training`, and
`storage_ingest`. `optimization_jobs.py` calls `set_job_progress` with the
default namespace `job`. Every progress write goes through
`storage/redis/progress.py::set_job_progress`, usually wrapped in a per-manager
`_persist_progress` that swallows exceptions (`backtest_jobs.py:83`). This is the
single choke point for progress. Terminal state is split:

- **Postgres-backed.** Backtest, optimization, walk-forward, and strategy search
  call the `update_*` functions in `storage/db/repositories.py`
  (`update_backtest_run` at line 217, `update_optimization_study`,
  `update_walkforward_run`, `update_strategy_search_run`) inside `session_scope`.
  Statuses come from `RunStatus` (`pending`, `running`, `completed`, `failed`,
  `cancelled`) or equal literals.
- **Redis-only.** `discovery_ab_jobs.py` and `storage_jobs.py` hold no database
  references. Neural training, encoder ablation, and alpha research are mixed,
  and they also write `"error"` for failure.

Each manager has `reconcile_orphaned_runs`, called from `api/lifespan.py` at
startup, which marks leftover runs cancelled (`repositories.mark_active_runs_cancelled`
at line 36). Tests drive whole jobs synchronously through the `run_jobs_sync`
fixture in `tests/conftest.py`, which reroutes actor sends in-process and
replaces Redis with one `fakeredis` instance. After Q-011,
`streaming/publisher.EphemeralPublisher` publishes with a Redis-assigned
sequence, and after Q-010 `streaming/outbox.record_event` records durable events
in the caller's session. Q-009's handoff supplies the status mapping table. The
gap: no job kind produces a stream event of any kind.

## Interfaces produced

```python
# src/q_backend/streaming/jobs.py
JobKind = Literal[
    "backtest", "optimization", "walkforward", "strategy_search", "alpha_research",
    "encoder_ablation", "discovery_ab", "neural_training", "storage_ingest",
]

NAMESPACE_TO_KIND: Mapping[str, JobKind]          # "job" → "optimization"; others identity
STATUS_TO_STREAM: Mapping[str, Literal["queued", "running", "completed", "failed", "cancelled"]]
    # from Q-009 handoff: "pending"→"queued", "error"→"failed", ...

def publish_job_progress(kind: JobKind, job_id: str, raw_payload: Mapping[str, Any]) -> None: ...
    """Map and publish to jobs.progress; bounded, never raises; skipped once terminal."""

def record_job_terminal(
    session: Session,
    kind: JobKind,
    job_id: str,
    raw_status: str,
    *,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> bool: ...
    """Record one jobs.terminal event in session's transaction; False if already recorded."""
```

```python
# src/q_backend/storage/db/outbox_models.py  (addition)
class JobTerminalMarker(Base):
    __tablename__ = "stream_job_terminal_markers"
    kind: Mapped[str]            # PK part 1
    job_id: Mapped[str]          # PK part 2
    status: Mapped[str]
    outbox_seq: Mapped[int]
    recorded_at: Mapped[datetime]
```

```python
# src/q_backend/storage/redis/progress.py  (signature unchanged; body gains the publish)
def set_job_progress(client, job_id, payload, ttl_seconds=..., *, namespace="job") -> None: ...
```

```
alembic/versions/<date>_0018_stream_job_terminal_markers.py
tests/streaming/test_job_events.py          parametrized over the nine kinds
tests/streaming/test_status_mapping.py      source scan of api/*_jobs.py status literals
tests/api/fixtures/job_payloads_pre_q012/   captured REST payloads for regression
```

## Implementation decisions

- **Progress is published from inside `set_job_progress`, not from each
  manager.** Every progress write in the codebase already passes through it, so
  one call site covers all nine kinds, and a job manager added later is covered
  without anyone remembering to add a publish. The namespace argument already
  identifies the kind, through `NAMESPACE_TO_KIND`.

- **The progress publish is wrapped with a 50 ms socket timeout and a catch-all
  that logs at debug level, mirroring `_persist_progress`'s existing
  best-effort handling.** The spec requires that progress never fails or
  meaningfully slows a job. A research worker blocked on an unreachable Redis
  for the client's default timeout on every progress tick would stretch a long
  optimization by minutes.

- **Terminal events are recorded inside the `update_*` repository functions
  when `status` moves into a terminal value, not in the managers.** Those
  functions receive the same `Session` the state change is flushed on, so
  recording there is atomic with the state change by construction. Recording
  in the managers would need every one of about twenty terminal call sites to
  pass the session correctly. The repository call already has it.

- **Redis-only kinds call `record_job_terminal` in their own `session_scope` at
  the point they write their terminal status to Redis, before that Redis
  write.** There is no Postgres state change to be atomic with. Committing the
  durable event first means a crash between the two leaves a durable
  "finished" alongside a stale "running" key. Q-015's snapshot resolves that in
  favor of the outbox. The opposite order would leave a finished job with no
  durable record.

- **Exactly-once is enforced by `stream_job_terminal_markers` with primary key
  `(kind, job_id)`, inserted with `ON CONFLICT DO NOTHING` in the same
  transaction as the outbox record, and the event is recorded only when the
  insert affected a row.** Dramatiq retries (`DEFAULT_MAX_RETRIES`), duplicate
  finalizers, and `reconcile_orphaned_runs` running over an already-finished
  job are all real ways to reach a terminal transition twice. The marker's
  unique key makes the second attempt a no-op at the database level, so no
  interleaving of two workers can produce two events. The marker is also how
  `publish_job_progress` knows a job is terminal. It checks a short-lived Redis
  flag set after commit, so it does not query Postgres on every progress tick.

- **A job id reused across runs (`find_backtest_run_by_config` reuses a row for
  an identical config) gets its marker cleared when the run is restarted.**
  `backtest_jobs._persist_run_start` sets an existing row back to `running`. Left
  alone, the marker would suppress the rerun's terminal event, and the frontend
  would never learn it finished. Clearing the marker happens in the same
  transaction as the move back to `running`.

- **`STATUS_TO_STREAM` has no default, and a test scans the sources.**
  `tests/streaming/test_status_mapping.py` collects every string literal
  assigned or passed as a status in `api/*_jobs.py` with `ast`, and asserts that
  each one is a key of the mapping. A runtime default such as "unknown → failed"
  would silently misreport the first new spelling someone introduces. The scan
  turns that into a failing build.

- **Neural training and encoder ablation publish `progress: null` with their
  text in `message`.** Q-009 forbids invented fractions. Their current
  `"Epoch 3/10"` strings could be parsed, but a parser for free text written by
  one manager is exactly the ad-hoc mirror the contracts exist to prevent.

- **The REST regression compares captured JSON payloads, not status codes.** The
  risk in this change is a manager's payload gaining or losing a field through a
  shared helper edit. Payloads for each kind are captured under
  `run_jobs_sync` before the first code change and compared after the last one,
  with volatile fields (timestamps, ids) normalized.

## Ordered implementation

1. Create the branch `Q-012-job-events-on-the-stream-spec` in `q_backend` from
   `development`, after Q-011 is merged.
2. Capture the regression baseline. For each of the nine kinds, run a job to
   completion under `run_jobs_sync`, and save its REST status and results
   payloads, with timestamps and ids normalized, under
   `tests/api/fixtures/job_payloads_pre_q012/`. Write
   `tests/api/test_job_payloads_unchanged.py` comparing live payloads against
   them, and confirm it passes on unmodified code. Commit.
3. Write the failing `tests/streaming/test_status_mapping.py`, which scans the
   job manager sources and asserts full coverage. Confirm it fails because
   `streaming/jobs.py` does not exist. Implement `NAMESPACE_TO_KIND` and
   `STATUS_TO_STREAM` from Q-009's handoff table. Confirm the test passes, and
   that it fails when a `"errored"` literal is added to a scratch copy of a
   manager. Commit.
4. Write failing unit tests for `publish_job_progress`: a backtest payload
   `{"status": "running"}` publishes `{kind: "backtest", status: "running",
   progress: null}` with routing key `{kind: "backtest", job_id}`; a payload whose raw
   status maps to a terminal value (`"completed"`, `"error"`) publishes nothing, because
   Q-009's progress enum admits only `queued` and `running`, and terminal outcomes travel only on
   `jobs.terminal`; an optimization payload under namespace
   `job` publishes kind `optimization`; a neural payload with progress
   `"Epoch 3/10"` publishes `progress: null, message: "Epoch 3/10"`; a publisher
   raising `ConnectionError` does not raise; a job flagged terminal publishes
   nothing. Confirm they fail. Implement, and call it from `set_job_progress`.
   Confirm they pass. Commit.
5. Add `JobTerminalMarker` and its migration. Write failing unit tests for
   `record_job_terminal`: the first call returns `True` and records one
   `jobs.terminal` event with status `failed` for raw `"error"`; a second call
   for the same `(kind, job_id)` returns `False` and records nothing; raw status
   `"running"` raises `ValueError`. Confirm they fail, implement, confirm they
   pass. Commit.
6. Write a failing `integration` test on Postgres: two threads call
   `record_job_terminal` for the same job at once, and exactly one event exists.
   Write a failing test that `update_backtest_run(status="completed")` inside a
   session that then raises leaves no marker and no event. Confirm they fail.
   Wire `record_job_terminal` into the four `update_*` functions on terminal
   transitions, and clear the marker on a transition back to `running`. Confirm
   they pass. Commit.
7. Write the failing parametrized `tests/streaming/test_job_events.py`: for each
   kind, run to completion under `run_jobs_sync` (extended to give the
   publisher the harness's `fakeredis` and the outbox an SQLite session), and
   assert at least one `jobs.progress` entry, exactly one `jobs.terminal` event
   with status `completed`, and no progress entry with a later `seq` than the
   terminal flag. Add forced-failure and cancellation variants where the kind
   supports them. Confirm they fail for the five Redis-only and mixed kinds.
   Wire `record_job_terminal` into those managers' terminal paths. Confirm they
   pass. Commit.
8. Write failing tests for reconciliation: an orphaned running backtest row, after
   `reconcile_orphaned_runs`, has one `cancelled` terminal event, and a second
   call adds none. Do the same for each manager's reconcile function. Confirm they
   fail, wire `mark_active_runs_cancelled` through `record_job_terminal`, confirm
   they pass. Commit.
9. Write a failing test that, with the stream publisher's client pointed at a
   closed port, every kind still completes under the harness and its terminal
   event is in the outbox. Confirm it fails if the 50 ms bound is removed (the
   test enforces a per-job time budget), implement, confirm it passes. Commit.
10. Run `tests/api/test_job_payloads_unchanged.py`, confirm it still passes, and
    commit.
11. Human step, matching human-verifiable criterion 1: with the API, one research
    worker, and `q-outbox-relay` running, start a backtest and an optimization
    from the research UI, read both streams with `redis-cli`, and confirm the UI
    status views behave as before.
12. Write `scripts/bench_job_overhead.py`. It submits a fixed optimization
    request to `POST /api/v1/optimize` (study parameters from
    `configs/optimization/examples`, trial count pinned), polls the status
    endpoint until the run is terminal, and prints wall-clock time per run. It
    goes through the API because `q-optimize` runs `OptimizationRunner`
    in-process and never calls `set_job_progress`, so it cannot measure this
    change. Commit. Human step, matching human-verifiable criterion 2: run it
    with `--runs 3` on the pre-task commit and on this branch, and record every
    run and the medians.
13. Run the full validation suite. Commit.

## Validation

- **Unit:** kind and status mapping; progress mapping per kind; publish failure
  tolerated; terminal recording, idempotency, and non-terminal rejection.
- **Integration:** concurrent terminal recording gives one event; rollback leaves
  none; nine kinds under the harness give progress plus exactly one terminal
  event; reconciliation is idempotent; Redis down still records terminal events.
- **Regression:** REST status and results payloads for all nine kinds are
  byte-equal to the pre-task capture after normalization, and the full existing
  suite passes.
- **Manual:** step 11.
- **Measurement:** optimization wall-clock, 3 runs before and 3 after,
  individual values and medians.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/streaming/test_job_events.py tests/streaming/test_status_mapping.py \
  tests/api/test_job_payloads_unchanged.py -v
uv run q-outbox-relay &
uv run python scripts/bench_job_overhead.py --kind optimization --runs 3
redis-cli -p 6380 XRANGE q:stream:jobs.terminal - + COUNT 20
redis-cli -p 6380 XRANGE q:stream:jobs.progress - + COUNT 50
```

## Handoff

Report a table of the nine kinds with, for each, the number of progress entries
and terminal events produced under the harness for completion, failure, and
cancellation, marking variants a kind does not support. Report the final
`STATUS_TO_STREAM` table and every source location the scan found. Report the
concurrency test's event count. Report the six optimization wall-clock times and
the median difference as a percentage. State which terminal call sites were
wired through repository functions and which through direct
`record_job_terminal` calls, with file and line, so a later job kind knows which
pattern applies to it.
