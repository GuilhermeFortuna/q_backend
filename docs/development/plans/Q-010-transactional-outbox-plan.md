# Q-010 implementation plan: Transactional outbox

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-010-transactional-outbox-spec.md`](../specs/Q-010-transactional-outbox-spec.md)  
**Depends on:** Q-009

## Current-system context

Persistence is SQLAlchemy 2.0 over Postgres. `storage/db/engine.py` provides
`get_engine`, `create_session_factory`, and `session_scope`, which commits on
exit and rolls back on exception. `storage/db/base.py` defines `Base`,
`UUIDPrimaryKeyMixin`, `TimestampMixin`, `utc_now`, and `PortableJSON` (JSONB on
Postgres, JSON on SQLite for unit tests). Alembic revisions live in
`alembic/versions/`, named `YYYYMMDD_NNNN_<slug>.py`. The head is
`20260702_0016` (order reconciliation fields), and `scripts/ci.sh` runs `alembic
upgrade head` before tests. Unit tests under `tests/storage/` use an in-memory
SQLite engine (`tests/storage/conftest.py::db_engine`). Tests marked
`integration` in `tests/storage/test_integration.py` use the real engine and
skip when Postgres is unreachable. CI provides Postgres on 5434.

The vendored `contracts/` directory (Python dataclasses from
`generated/python/q_contracts`, pinned by `CONTRACTS_REV` at `998a505`) is
checked by `make contracts-check` and imported by nothing: `pyproject.toml` does
not package it and no module under `src/` or `tests/` references it. There is no
outbox, no event table, and no sequence anywhere in the schema. Execution tables
(`20260630_0014`, `_0015`) record audit rows but do not number them per topic.
The gap this task closes is a durable, gapless, per-topic event log that can be
written inside any existing transaction.

## Interfaces produced

```python
# src/q_backend/storage/db/outbox_models.py
class OutboxEvent(Base):
    __tablename__ = "stream_outbox"
    topic: Mapped[str]                 # durable topic name; PK part 1
    epoch: Mapped[str]                 # outbox epoch at record time; PK part 2
    seq: Mapped[int]                   # gapless per (topic, epoch); PK part 3
    producer_id: Mapped[str]
    origin_ts: Mapped[datetime]        # UTC, producer clock; never used for ordering
    routing_key: Mapped[dict | None]   # envelope "key" (Q-009), PortableJSON
    payload_kind: Mapped[str]          # "control" only for durable topics in this batch
    payload_schema: Mapped[str]
    payload: Mapped[dict]              # PortableJSON
    recorded_at: Mapped[datetime]      # DB clock, for 30-day retention

class OutboxTopicState(Base):
    __tablename__ = "stream_outbox_topic_state"
    topic: Mapped[str]                 # PK
    epoch: Mapped[str]
    last_seq: Mapped[int]              # last issued; row-locked on issue
    last_relayed_seq: Mapped[int]      # advanced by Q-011's relay; 0 until then
```

```python
# src/q_backend/streaming/outbox.py
class OutboxTopicError(ValueError): ...      # ephemeral or undeclared topic
class OutboxEnvelopeError(ValueError): ...   # envelope or routing invalid

def record_event(
    session: Session,
    topic: str,
    payload: Mapping[str, Any],
    *,
    payload_schema: str,
    producer_id: str,
    routing_key: Mapping[str, str] | None = None,
) -> OutboxEvent: ...
    """Issue the next seq for topic and add the event to session's transaction."""

def read_watermark(session: Session, topics: Iterable[str]) -> dict[str, tuple[str, int]]: ...
    """(epoch, max visible seq) per topic, as seen by session's snapshot."""

def prune_relayed(session: Session, *, older_than: timedelta = timedelta(days=30)) -> dict[str, int]: ...
    """Delete relayed events older than the cutoff; return oldest retained seq per topic."""

def oldest_retained_seq(session: Session, topic: str) -> int | None: ...

def rotate_epoch(session: Session, topic: str, *, reason: str) -> tuple[str, str]: ...
    """Operator action: new epoch, seq restarts at 0. Returns (old, new)."""
```

```python
# src/q_backend/cli/q_outbox.py   (project script "q-outbox")
def main(argv: list[str] | None = None) -> int: ...   # subcommands: rotate-epoch TOPIC --reason, prune
```

```
alembic/versions/<date>_0017_stream_outbox.py
scripts/bench_outbox.py        # human criterion 1
pyproject.toml                 contracts/ packaged as import name q_contracts
CONTRACTS_REV                  → Q-009 commit
```

## Implementation decisions

- **Sequence numbers come from a row-locked counter in
  `stream_outbox_topic_state` (`UPDATE ... SET last_seq = last_seq + 1 RETURNING`),
  not from a Postgres `SEQUENCE`.** Architecture §4.1 says "Postgres sequence per
  topic", but `nextval` is non-transactional: a rolled-back transaction burns a
  number. Clients treat that gap as a lost event, and Q-015's history endpoint
  can never supply it. The row lock is the transactional equivalent. This is a
  deliberate deviation from the architecture's wording in service of the
  guarantee that wording was meant to give, and it is recorded in the
  architecture's revision note by the handoff.

- **The row lock also makes commit order equal `seq` order.** Transaction B
  cannot issue `seq` n+1 until A, holding the lock for n, commits or rolls back.
  So there is no moment when n+1 is visible and n is not. With a `SEQUENCE`,
  that moment exists, and a watermark read in it would silently skip n.

- **The counter row is locked last in the caller's transaction, by convention
  enforced in `record_event`'s docstring and tested by timing.** A job manager
  that calls `record_event` and then spends two seconds writing result rows
  would hold every other writer on that topic for two seconds. The contention
  benchmark (human criterion 1) measures what serialization costs when the
  convention is followed.

- **The primary key is `(topic, epoch, seq)`.** Envelope ordering is defined per
  `(topic, epoch)` (Q-002). A key without `epoch` would make a rotated epoch's
  `seq` 1 collide with the retained rows of the old epoch.

- **Topic class is checked against the generated `q_contracts.topics.TOPICS`
  from Q-009, not against a list in this repository.** A hand-kept list of
  durable topics is exactly the mirror architecture invariant 2 forbids, and it
  would accept a topic the contracts later reclassify.

- **The envelope is validated at record time by constructing the vendored
  `StreamEnvelope` and checking routing against the topic's `coalesce_key`.** A
  malformed event found at relay time has already committed alongside real
  state and cannot be rolled back. Record time is the last moment the error can
  still undo the change it describes.

- **`contracts/` is packaged under the import name `q_contracts`** by adding it
  to the wheel in `pyproject.toml`, rather than being put on `sys.path` in
  `conftest.py`. The relay (Q-011) and publisher (Q-013) run as installed
  console scripts, where a test-only path fix does not exist.

- **The epoch is a string `"<yyyymmdd>-<8 hex>"` created by the migration, not a
  timestamp generated at process start.** Architecture §4.3 ties a durable
  epoch change to "full re-snapshot of that topic". If a restart minted a new
  epoch, every API redeploy would force every client to re-snapshot, and that
  would teach everyone to ignore epoch changes.

- **`prune_relayed` deletes only rows with `seq <= last_relayed_seq`.** If the
  relay is down for longer than the retention window, pruning by age alone would
  delete events Redis never received. Those events would then exist nowhere.

- **Unit tests use SQLite for validation and shape. Every ordering, locking,
  and isolation claim is an `integration` test against Postgres.** SQLite has no
  row locks and no `REPEATABLE READ`, so a concurrency test passing on it proves
  nothing. The existing `integration` marker and skip-when-unreachable pattern
  are reused, and `scripts/ci.sh` already provides Postgres, so these tests run
  in CI rather than skipping.

- **`src/q_backend/streaming/` is a new package, not a module under
  `storage/redis/`.** The outbox is Postgres, the relay and publisher are Redis,
  and the endpoint is FastAPI. Filing them under a storage backend would split
  one protocol across three unrelated directories.

## Ordered implementation

1. Create the branch `Q-010-transactional-outbox-spec` in `q_backend` from
   `development`.
2. Set `CONTRACTS_REV` to Q-009's commit and run `make contracts`. Package
   `contracts/` as `q_contracts` in `pyproject.toml`. Write a failing test,
   `tests/streaming/test_contracts_import.py`, asserting that
   `from q_contracts.topics import TOPICS` succeeds and that
   `TOPICS["jobs.terminal"].topic_class == "durable"`. Confirm it fails before
   the packaging change, then passes. Confirm `make contracts-check` passes.
   Commit.
3. Add `OutboxEvent` and `OutboxTopicState` and the migration, which seeds one
   state row per durable topic with a fresh epoch and `last_seq = 0`. Write a
   failing `integration` test that upgrades from `20260702_0016` to head and back
   down, asserting the two tables appear and disappear and that seven state rows
   exist after upgrade. Confirm it fails, implement, confirm it passes. Commit.
4. Write failing unit tests for `record_event` on SQLite: recording `quotes`
   raises `OutboxTopicError`; recording `"nope"` raises `OutboxTopicError`;
   recording `jobs.terminal` with a payload missing `job_id` raises
   `OutboxEnvelopeError`; two events recorded on `jobs.terminal` in one session
   get `seq` 1 and 2 with the seeded epoch. Confirm they fail. Implement.
   Confirm they pass. Commit.
5. Write a failing `integration` test for rollback: record on `jobs.terminal` in
   a session that raises and rolls back, then record in a new session and
   assert `seq == 1`. Write a failing `integration` test for concurrency: 100
   threads each open a session, record one event on `jobs.terminal`, and commit;
   assert the set of `seq` equals `{1..100}`. Confirm both fail before the lock
   exists (use a version of `record_event` that reads `max(seq)+1`, which must
   fail the concurrency test). Implement the row lock. Confirm both pass. Commit.
6. Write a failing `integration` test for `read_watermark`: session A starts
   `REPEATABLE READ` and reads the watermark (0); session B records and commits
   `seq` 1; A reads again and still sees 0; a new session sees 1. Confirm it
   fails, implement, confirm it passes. Commit.
7. Write failing `integration` tests for `prune_relayed`: with events 1–10 all
   older than 31 days and `last_relayed_seq = 6`, pruning deletes 1–6, keeps
   7–10, and reports `{"jobs.terminal": 7}`; `oldest_retained_seq` returns 7;
   a second prune deletes nothing. Confirm they fail, implement, confirm they
   pass. Commit.
8. Write failing tests for `rotate_epoch` and the `q-outbox rotate-epoch` CLI:
   the epoch changes, the next `seq` is 1, the log line contains both epochs and
   the reason, and running `alembic upgrade head` again leaves the epoch
   unchanged. Confirm they fail, implement, confirm they pass. Commit.
9. Write `scripts/bench_outbox.py`: N writer threads, one topic, each recording
   in its own transaction for the given duration, printing events per second and
   p50/p95/p99 recording latency. Commit.
10. Human step, matching human-verifiable criterion 1: run the benchmark against
    the development Postgres with 8 writers for 60 seconds, three times, and
    record the individual and median figures.
11. Run the full validation suite. Commit.

## Validation

- **Unit:** topic discipline, envelope and routing validation, and in-session
  sequence issue (SQLite).
- **Integration:** migration up and down; rollback leaves no gap; 100 concurrent
  writers give exactly `{1..100}`; repeatable-read watermark stability; prune
  bounded by `last_relayed_seq`; epoch stable across re-migration.
- **Regression:** the full existing suite passes unchanged. No existing table is
  altered.
- **Measurement:** throughput and p95 latency, 8 writers × 60 s × 3 runs,
  individual values and median.

```bash
cd /home/gui/projects/q/q_backend
make contracts-check
scripts/ci.sh
uv run pytest tests/streaming -v -m "integration or not integration"
uv run python scripts/bench_outbox.py --writers 8 --seconds 60
```

## Handoff

Report the pinned contracts commit and the clean `contracts-check`. Report the
concurrency test's observed `seq` set size and range, and the failure the naive
`max(seq)+1` version produced before the lock (the duplicate or missing numbers),
so the lock is shown to be necessary. Report the three benchmark runs with the
median events per second and p95 latency. Report the migration revision id and
the seeded epoch value. State plainly that this implementation deviates from
architecture §4.1's "Postgres sequence" wording, and why, so the architecture
document can be amended in `q_contracts` rather than silently contradicted.
