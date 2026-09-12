# Q-015 implementation plan: Stream snapshot and history endpoints

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-015-stream-snapshot-and-history-endpoints-spec.md`](../specs/Q-015-stream-snapshot-and-history-endpoints-spec.md)  
**Depends on:** Q-012

## Current-system context

After Q-010, `stream_outbox` holds durable events keyed `(topic, epoch, seq)`,
and `stream_outbox_topic_state` holds each topic's current epoch.
`streaming/outbox.py` provides `read_watermark` (inside the caller's snapshot),
`oldest_retained_seq`, and `prune_relayed`. After Q-011, the latest entry per
routing key of each ephemeral topic is referenced from the hash `q:latest:<topic>`
(routing-key string → stream id), and `streaming/codec.decode_entry` rebuilds
envelopes. After Q-012, `stream_job_terminal_markers` records exactly one row per
terminal job, in the same transaction as its outbox event, and
`streaming/jobs.py` holds `NAMESPACE_TO_KIND` and `STATUS_TO_STREAM`. Active job
state is spread across the Postgres run tables (`backtest_runs`,
`optimization_studies`, `walkforward_runs`, `strategy_search_runs`, each with a
`status` column and index) and the Redis progress keys
`"<namespace>:progress:<id>"` for kinds with no table.

The existing execution history routes
(`/api/v1/execution/deployments/{id}/decisions`, and others in
`api/routers/execution.py`) page by `from_time`, `to_time`, `limit`, and `offset`
over their own tables. `q_contracts/schema/api/FINDINGS.md` Finding 4 names the
absence of sequence-based history and latest endpoints. `q_contracts/tools/capture_api.py`
recaptures `schema/api/openapi.yaml` from a running API, and
`tests/test_api_drift.py` compares routes live. Q-009 defined `history-page`,
`history-expired`, `latest`, and `watermark` schemas. The gap: a client told
`lagging` or `epoch_changed` has nothing to call.

## Interfaces produced

```python
# src/q_backend/api/routers/stream_replay.py
router = APIRouter(prefix="/api/v1/stream", tags=["stream"])

@router.get("/{topic}/history", response_model=HistoryPageResponse,
            responses={409: {"model": EpochMismatchResponse}, 410: {"model": HistoryExpiredResponse},
                       400: {"model": ErrorResponse}})
def get_history(topic: str, epoch: str, from_seq: int = Query(ge=1), limit: int = Query(500, ge=1, le=2000)): ...

@router.get("/{topic}/latest", response_model=LatestResponse,
            responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
def get_latest(topic: str, key: str | None = None): ...

@router.get("/jobs/snapshot", response_model=JobSnapshotResponse)
def get_job_snapshot(): ...
```

```python
# src/q_backend/api/schemas/stream.py   (Pydantic; mirror Q-009 replay schemas, validated by test)
class ErrorResponse(BaseModel):          # matches q_contracts schema/api/error.schema.json
    message: str
    code: str | None = None
    details: Any | None = None

class HistoryPageResponse(BaseModel):
    topic: str
    epoch: str
    entries: list[dict[str, Any]]       # logical envelopes
    next_seq: int | None

class HistoryExpiredResponse(BaseModel):
    topic: str
    requested_from_seq: int
    oldest_available_seq: int | None

class EpochMismatchResponse(BaseModel):
    topic: str
    requested_epoch: str
    current_epoch: str

class LatestResponse(BaseModel):
    topic: str
    entries: dict[str, dict[str, Any]]  # routing-key string → logical envelope

class JobSnapshotItem(BaseModel):
    kind: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: float | None
    message: str | None
    progress_seq: int | None            # latest jobs.progress entry reflected, if any
    progress_epoch: str | None

class JobSnapshotResponse(BaseModel):
    jobs: list[JobSnapshotItem]
    watermark: dict[str, dict[str, Any]]   # {"jobs.terminal": {"epoch", "seq"}}
```

```python
# src/q_backend/streaming/snapshot.py
def read_history(session: Session, topic: str, epoch: str, from_seq: int, limit: int) -> HistoryResult: ...
def read_latest(client: redis.Redis, topic: str, key: str | None) -> dict[str, StreamEnvelope]: ...
def read_job_snapshot(session_factory, client: redis.Redis, *, terminal_window: timedelta = timedelta(hours=24)) -> JobSnapshotResponse: ...
```

```
q_contracts: schema/api/openapi.yaml    recaptured (same task id, branch in q_contracts)
q_contracts: schema/api/FINDINGS.md     Finding 4 status
q_contracts: COMPAT.md                  q_backend row updated
```

## Implementation decisions

- **The job snapshot opens one `REPEATABLE READ` transaction and reads the
  terminal watermark first. It then reads the Postgres run tables and the terminal
  markers in the same transaction, and only after that reads the Redis progress
  keys.** In `REPEATABLE READ`, the first statement fixes the snapshot, so the
  watermark and every Postgres read see exactly the same committed terminal
  events. That is the §4.2 guarantee. Redis cannot join the transaction, so it is
  read last, and any Redis status that disagrees with the transaction is
  overridden by the rule below.

- **Terminal status in the snapshot comes only from `stream_job_terminal_markers`
  visible in the transaction. A Redis or table status that says terminal
  without a visible marker is reported as `running`.** The markers are written in
  the same transaction as the outbox event, so "marker visible" is exactly "event
  at or below the watermark". If a job finishes after the snapshot was fixed but
  before Redis is read, Redis already says `completed`. Reporting that would make
  the snapshot reflect an event above the watermark, and the client would then
  discard nothing and apply the terminal event again. That happens to be
  harmless for terminal events, but it breaks the stated invariant that criterion
  8 tests. Reporting `running` keeps the snapshot and watermark exact, and the
  terminal event arrives on the stream within a second.

- **A marker visible in the transaction overrides a Redis key that says
  `running`.** Q-012 commits the durable event before writing Redis, so a crash
  between the two leaves exactly that disagreement, and the outbox is the
  durable truth (criterion 9).

- **Active jobs are found by `status IN ('pending', 'running')` on the four run
  tables, and by `SCAN` over the Redis-only kinds' progress-key namespaces.**
  The tables are indexed on `status`. Redis-only kinds have no other record of
  being active. `SCAN` with `MATCH <namespace>:progress:*` and `COUNT 500` is
  bounded, and the job population is small, measured in dozens.

- **Per-job `progress_seq` and `progress_epoch` come from the `q:latest:jobs.progress`
  hash entry for that job's routing key.** A client buffering `jobs.progress`
  during the snapshot needs to know which of those entries the snapshot already
  reflects. §4.2 says ephemeral topics use their `latest` value as the snapshot,
  so the snapshot carries it per key.

- **History reads use the primary key `(topic, epoch, seq)` with `seq >= :from_seq
  ORDER BY seq LIMIT :limit + 1`, and `next_seq` is the extra row's `seq` if one
  exists.** Fetching one extra row answers "more follow" without a `COUNT`. The
  primary key makes the range scan index-only in order. A `limit` capped at 2000
  bounds a response to a few hundred kilobytes of JSON even for large
  job-terminal payloads.

- **Expired is decided by `from_seq < oldest_retained_seq(topic)`, not by an empty
  result.** After `prune_relayed`, a range below the oldest row returns rows from
  the oldest onward. Without the explicit check, a client asking for 3 would
  silently get 7 onward and conclude it had caught up.

- **Epoch mismatch returns 409 and expired returns 410, each with its own body
  schema.** They call for different client actions: re-snapshot versus
  re-snapshot-and-accept-the-gap. Collapsing them into one 4xx would make the
  frontend parse an error message to choose. The status codes are declared in
  `responses=`, and that also avoids repeating Finding 2's undeclared-error
  pattern for the new routes.

- **Pydantic response models are hand-written in `api/schemas/stream.py`, and a
  test validates their JSON output against the vendored Q-009 JSON Schemas.**
  `COMPAT.md` records that `q_backend` hand-writes API models, because the
  captured OpenAPI is generated from them, and generating them back would be a
  loop. The test is what keeps the hand-written models from drifting from the
  contract they must satisfy.

- **The OpenAPI recapture is a commit on a `Q-015-...` branch in `q_contracts`,
  made after the backend branch passes, and `COMPAT.md` records the backend
  commit it was captured from.** The route drift check compares against a running
  API, so the capture has to follow the backend change, and architecture §7.1
  makes updating `COMPAT.md` part of any cross-repository change. The branch
  carries the same task ID in both repositories. It is one task spanning two
  repositories, not two tasks.

## Ordered implementation

1. Create the branch `Q-015-stream-snapshot-and-history-endpoints-spec` in
   `q_backend` from `development`, after Q-012 is merged.
2. Write failing tests in `tests/streaming/test_history.py` against Postgres
   (`integration`): with events 1–10 on `jobs.terminal`, `read_history(from_seq=5,
   limit=3)` returns 5, 6, 7 with `next_seq == 8`; `from_seq=9, limit=3` returns 9
   and 10 with `next_seq is None`; after pruning 1–6, `from_seq=3` raises
   `HistoryExpired(oldest=7)`; a wrong epoch raises `EpochMismatch(current=...)`.
   Confirm they fail, implement `read_history`, confirm they pass. Commit.
3. Write failing router tests in `tests/api/test_stream_replay.py`: the four cases
   map to 200, 410, and 409 with bodies that validate against the vendored Q-009
   `history-page`, `history-expired`, and epoch-mismatch shapes; `quotes` history
   returns 400; with the Redis client pointed at a closed port, history still
   returns 200. Confirm they fail, implement the route and models, confirm they
   pass. Commit.
4. Write failing tests for `read_latest` and the route against `fakeredis`, using
   Q-011's `EphemeralPublisher`: publish quotes A1, B1, A2 (3-row IPC each); latest
   returns keys A and B with A's entry having A2's `seq`; its base64 payload decodes
   to the A2 batch; `?key=B` returns only B; the body validates against Q-009
   `latest`; with Redis at a closed port, the route returns 503
   `stream_unavailable`. Confirm they fail, implement, confirm they pass. Commit.
5. Write failing tests for `read_job_snapshot` under `run_jobs_sync` plus a real
   Postgres (`integration`): with one running backtest row, one finished
   backtest with a marker, and one Redis-only discovery A/B job whose progress key
   says `running`, the snapshot lists all three with `running`, `completed`, and
   `running`, and the watermark equals the outbox max `seq`. Then commit a marker
   and event for the discovery job without touching its Redis key, and the next
   snapshot reports it `completed` (criterion 9). Confirm they fail, implement,
   confirm they pass. Commit.
6. Write the failing race test for criterion 8 (`integration`), repeated 200
   times: thread A calls `read_job_snapshot`; thread B, started with a random
   0–5 ms delay, records a terminal event for a running backtest. For each trial,
   assert that either (job terminal in snapshot and watermark ≥ event `seq`) or
   (job running in snapshot and watermark < event `seq`). Confirm it fails
   against a variant that reads the watermark in a separate transaction (kept only
   in the test as a negative control), and passes against the implementation.
   Commit.
7. Add the `/jobs/snapshot` route with a failing test that its body validates
   against `JobSnapshotResponse` and Q-009's watermark schema. Implement. Confirm it
   passes. Confirm that `tests/api/test_job_payloads_unchanged.py` (from Q-012)
   still passes. Commit.
8. In `q_contracts`, create the branch `Q-015-stream-snapshot-and-history-endpoints-spec`.
   Run the backend from this task's branch, run `uv run python
   tools/capture_api.py`, and commit `schema/api/openapi.yaml`. Update Finding 4
   in `FINDINGS.md` to resolved for job and market-data topics and open for
   execution topics. Run `uv run pytest tests/test_api_drift.py` against the
   running API and confirm it passes. Run `make check`. Update `COMPAT.md`'s
   `q_backend` row with this branch's commit and verification. Commit.
9. Human step, matching human-verifiable criterion 1: with the API, one worker,
   and the relay running, start two backtests from the research UI, call the
   snapshot while both run, and read `jobs.terminal` once they finish. Confirm
   that the snapshot's watermark and statuses, together with the subsequent
   events, describe both jobs correctly.
10. Run the full validation suite in `q_backend` and `make check` in
    `q_contracts`. Commit.

## Validation

- **Unit:** history paging math, expired and epoch decisions, latest decoding,
  and snapshot status rules.
- **Integration:** history on Postgres with Redis down; 200-trial snapshot and
  terminal race; marker overrides a stale Redis key; response bodies validate
  against Q-009 schemas.
- **Regression:** Q-012's REST job payload capture still matches; existing
  execution history routes are unchanged; OpenAPI route drift is clean.
- **Manual:** step 9.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/streaming/test_history.py tests/api/test_stream_replay.py -v -m "integration or not integration"
uv run dev &
curl -s localhost:8000/api/v1/stream/jobs/snapshot | jq

cd /home/gui/projects/q/q_contracts
uv run python tools/capture_api.py
uv run pytest tests/test_api_drift.py -v
UV_CACHE_DIR=/tmp/q-uv-cache make check
```

## Handoff

Report the race test's outcome over 200 trials: how many trials landed on each
side of the watermark, and the failures the separate-transaction negative control
produced. This shows the single-transaction read is what makes the snapshot
exact. Report the three history error responses verbatim. Report the snapshot
body from the manual step. Report the `q_contracts` commit containing the
recaptured OpenAPI, the routes it added, and the `COMPAT.md` row as committed.
Report the snapshot endpoint's p95 latency over 100 calls with 20 active jobs, so
Q-016 knows what a re-snapshot costs.
