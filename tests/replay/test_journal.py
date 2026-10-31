"""Durable store behavior: atomic transactions, journal limits and persistence across reopen."""

from __future__ import annotations

import pytest

from q_backend.replay.config import to_market_us
from q_backend.replay.journal import JournalLimitError, ReplayRunStore

from .conftest import START, make_config

BASE = to_market_us(START)


def _seed(store: ReplayRunStore) -> None:
    config = make_config()
    with store.transaction() as conn:
        store.create_run(
            conn,
            run_id="run",
            generation="gen",
            state="paused",
            rate="1.0",
            cursor_us=BASE,
            end_us=config.end_us,
            config_hash=config.config_hash(),
            config_json=config.canonical_json(),
            dataset_checksums=(),
            max_queued_events=10,
            max_journal_bytes=200,
        )


def test_failed_transaction_rolls_back_every_write(store: ReplayRunStore) -> None:
    _seed(store)
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.enqueue(conn, at_us=BASE, priority=0, kind="a", payload={}, max_queued_events=10)
            store.update_run(conn, cursor_us=BASE + 99)
            raise RuntimeError("crash before commit")
    with store.transaction() as conn:
        assert store.pending_count(conn) == 0
    assert store.load_run().cursor_us == BASE


def test_journal_refuses_entries_beyond_byte_limit(store: ReplayRunStore) -> None:
    _seed(store)
    with store.transaction() as conn:
        big = {"blob": "x" * 150}
        store.append_journal(conn, kind="cursor", market_time_us=BASE, priority=0, payload=big, max_journal_bytes=200)
        with pytest.raises(JournalLimitError):
            store.append_journal(
                conn, kind="cursor", market_time_us=BASE, priority=0, payload=big, max_journal_bytes=200
            )


def test_queue_and_run_state_survive_reopen(tmp_path) -> None:
    path = tmp_path / "replay.sqlite3"
    first = ReplayRunStore(path)
    _seed(first)
    with first.transaction() as conn:
        first.enqueue(conn, at_us=BASE + 5, priority=2, kind="pending", payload={"n": 1}, max_queued_events=10)
    first.close()

    second = ReplayRunStore(path)
    try:
        run = second.load_run()
        assert run is not None and run.state == "paused" and run.max_journal_bytes == 200
        with second.transaction() as conn:
            event = second.next_due(conn, BASE + 10)
        assert event is not None and event.kind == "pending" and event.payload == {"n": 1}
    finally:
        second.close()


def test_store_holds_one_run_only(store: ReplayRunStore) -> None:
    _seed(store)
    from q_backend.replay.journal import RunAlreadyExistsError

    with pytest.raises(RunAlreadyExistsError):
        _seed(store)


def test_config_hash_is_stable_for_equal_configs() -> None:
    assert make_config().config_hash() == make_config().config_hash()
    assert make_config().config_hash() != make_config(rate=2.0).config_hash()
