"""Durable per-run state, event queue and journal for the replay simulator.

One SQLite file per simulator instance holds the single active run. The store is the only state
owner: scheduled events, cursor progress, command results and journal entries are written in
transactions at event boundaries, so a crash before commit has no acknowledged effect. The store
lives outside the Q ledger; it never writes to live or research databases.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    rate TEXT NOT NULL,
    cursor_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    dataset_checksums TEXT NOT NULL,
    max_queued_events INTEGER NOT NULL,
    max_journal_bytes INTEGER NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS queue (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at_us INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS queue_pending ON queue (status, at_us, priority, seq);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    market_time_us INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    reason TEXT,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL
);
"""


class ReplayStoreError(RuntimeError):
    """Base class for store failures that must stop the run."""


class QueueLimitError(ReplayStoreError):
    """The run already holds ``max_queued_events`` pending events."""


class JournalLimitError(ReplayStoreError):
    """Appending would exceed ``max_journal_bytes``."""


class RunAlreadyExistsError(ReplayStoreError):
    """A run is already recorded; a new run needs a new store generation."""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    generation: str
    state: str
    revision: int
    rate: str
    cursor_us: int
    end_us: int
    config_hash: str
    config_json: str
    dataset_checksums: tuple[str, ...]
    max_queued_events: int
    max_journal_bytes: int
    last_error: str | None


@dataclass(frozen=True)
class QueuedEvent:
    seq: int
    at_us: int
    priority: int
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class StoredCommand:
    command_id: str
    action: str
    expected_revision: int
    accepted: bool
    reason: str | None
    revision: int
    state: str


def canonical_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


class ReplayRunStore:
    """Transactional SQLite store for one replay simulator instance."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def install_schema(self, ddl: str) -> None:
        """Create the tables of a component (idempotent ``CREATE ... IF NOT EXISTS`` statements)."""
        self._conn.executescript(ddl)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically; any exception rolls back every write in it."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # run row -----------------------------------------------------------------

    def create_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        generation: str,
        state: str,
        rate: str,
        cursor_us: int,
        end_us: int,
        config_hash: str,
        config_json: str,
        dataset_checksums: tuple[str, ...],
        max_queued_events: int,
        max_journal_bytes: int,
    ) -> None:
        if conn.execute("SELECT 1 FROM run").fetchone() is not None:
            raise RunAlreadyExistsError("this simulator already holds a run")
        conn.execute(
            "INSERT INTO run (id, run_id, generation, state, revision, rate, cursor_us, end_us,"
            " config_hash, config_json, dataset_checksums, max_queued_events, max_journal_bytes,"
            " last_error) VALUES (1, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                run_id,
                generation,
                state,
                rate,
                cursor_us,
                end_us,
                config_hash,
                config_json,
                json.dumps(list(dataset_checksums)),
                max_queued_events,
                max_journal_bytes,
            ),
        )

    def load_run(self) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT run_id, generation, state, revision, rate, cursor_us, end_us, config_hash,"
            " config_json, dataset_checksums, max_queued_events, max_journal_bytes, last_error"
            " FROM run WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row[0],
            generation=row[1],
            state=row[2],
            revision=row[3],
            rate=row[4],
            cursor_us=row[5],
            end_us=row[6],
            config_hash=row[7],
            config_json=row[8],
            dataset_checksums=tuple(json.loads(row[9])),
            max_queued_events=row[10],
            max_journal_bytes=row[11],
            last_error=row[12],
        )

    def update_run(self, conn: sqlite3.Connection, **fields: Any) -> None:
        allowed = {"state", "revision", "rate", "cursor_us", "last_error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update run fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(f"UPDATE run SET {assignments} WHERE id = 1", tuple(fields.values()))

    # queue --------------------------------------------------------------------

    def pending_count(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) FROM queue WHERE status = 'pending'").fetchone()
        return int(row[0])

    def enqueue(
        self,
        conn: sqlite3.Connection,
        *,
        at_us: int,
        priority: int,
        kind: str,
        payload: Mapping[str, Any],
        max_queued_events: int,
    ) -> int:
        if self.pending_count(conn) >= max_queued_events:
            raise QueueLimitError(f"run already has {max_queued_events} queued events")
        cursor = conn.execute(
            "INSERT INTO queue (at_us, priority, kind, payload, status)" " VALUES (?, ?, ?, ?, 'pending')",
            (at_us, priority, kind, canonical_payload(payload)),
        )
        return int(cursor.lastrowid or 0)

    def next_due(self, conn: sqlite3.Connection, until_us: int) -> QueuedEvent | None:
        """Return the next pending event at or before ``until_us`` in deterministic order.

        Order is ``(market time, priority, sequence)``. Bounds are inclusive here; callers pass
        the exclusive run end minus one microsecond when they must stop before it.
        """
        row = conn.execute(
            "SELECT seq, at_us, priority, kind, payload FROM queue"
            " WHERE status = 'pending' AND at_us <= ?"
            " ORDER BY at_us, priority, seq LIMIT 1",
            (until_us,),
        ).fetchone()
        if row is None:
            return None
        return QueuedEvent(row[0], row[1], row[2], row[3], json.loads(row[4]))

    def set_queue_status(self, conn: sqlite3.Connection, seq: int, status: str) -> None:
        conn.execute("UPDATE queue SET status = ? WHERE seq = ?", (status, seq))

    def cancel_pending(self, conn: sqlite3.Connection) -> int:
        cursor = conn.execute("UPDATE queue SET status = 'cancelled' WHERE status = 'pending'")
        return cursor.rowcount

    # journal ------------------------------------------------------------------

    def journal_bytes(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM journal").fetchone()
        return int(row[0])

    def append_journal(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        market_time_us: int,
        priority: int,
        payload: Mapping[str, Any],
        max_journal_bytes: int,
    ) -> int:
        text = canonical_payload(payload)
        size = len(text.encode("utf-8"))
        if self.journal_bytes(conn) + size > max_journal_bytes:
            raise JournalLimitError(f"journal would exceed {max_journal_bytes} bytes")
        cursor = conn.execute(
            "INSERT INTO journal (kind, market_time_us, priority, payload, payload_hash, size_bytes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, market_time_us, priority, text, payload_hash(payload), size),
        )
        return int(cursor.lastrowid or 0)

    def journal_entries(self) -> list[tuple[int, str, int, str, str]]:
        rows = self._conn.execute(
            "SELECT seq, kind, market_time_us, payload_hash, payload FROM journal ORDER BY seq"
        ).fetchall()
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    # commands -----------------------------------------------------------------

    def get_command(self, command_id: str) -> StoredCommand | None:
        row = self._conn.execute(
            "SELECT command_id, action, expected_revision, accepted, reason, revision, state"
            " FROM commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredCommand(
            command_id=row[0],
            action=row[1],
            expected_revision=row[2],
            accepted=bool(row[3]),
            reason=row[4],
            revision=row[5],
            state=row[6],
        )

    def record_command(self, conn: sqlite3.Connection, command: StoredCommand) -> None:
        conn.execute(
            "INSERT INTO commands (command_id, action, expected_revision, accepted, reason,"
            " revision, state) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                command.command_id,
                command.action,
                command.expected_revision,
                int(command.accepted),
                command.reason,
                command.revision,
                command.state,
            ),
        )
