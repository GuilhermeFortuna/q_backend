"""Market and operational clock domains: remote market time fails closed; heartbeats stay operational."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from q_backend.time import (
    ClockGenerationError,
    ClockStaleError,
    ClockUnavailableError,
    RemoteMarketClock,
    SystemMarketClock,
)

SNAPSHOT = {
    "sequence": 3,
    "generation": "gen-1",
    "market_time_us": int(datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp() * 1_000_000),
    "market_time_utc": "2026-10-01T12:00:00Z",
    "rate": 2.0,
    "state": "running",
}


class Source:
    """A fake /v1/clock source whose answers the test controls."""

    def __init__(self, snapshot=None, status=200):
        self.snapshot = dict(SNAPSHOT) if snapshot is None else snapshot
        self.status = status
        self.fail = False
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(self.status, json=self.snapshot)


def _clock(source: Source, mono, **kwargs) -> RemoteMarketClock:
    return RemoteMarketClock(
        "http://127.0.0.1:18912",
        expected_generation="gen-1",
        monotonic=mono,
        transport=httpx.MockTransport(source.handler),
        **kwargs,
    )


class Mono:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def test_remote_market_time_advances_at_the_snapshot_rate() -> None:
    mono, source = Mono(), Source()
    clock = _clock(source, mono, refresh_seconds=100.0)  # no refresh inside this window
    start = clock.now()
    mono.value += 1.5  # 1.5 real seconds at rate 2 is 3 market seconds
    assert (clock.now() - start).total_seconds() == pytest.approx(3.0)


def test_remote_market_time_freezes_while_paused() -> None:
    mono = Mono()
    source = Source({**SNAPSHOT, "state": "paused"})
    clock = _clock(source, mono)
    first = clock.now()
    mono.value += 30.0
    assert clock.now() == first


def test_generation_mismatch_always_fails_closed() -> None:
    mono = Mono()
    source = Source({**SNAPSHOT, "generation": "gen-2"})
    with pytest.raises(ClockGenerationError):
        _clock(source, mono).now()


def test_unreachable_source_without_a_snapshot_fails_closed() -> None:
    mono, source = Mono(), Source()
    source.fail = True
    with pytest.raises(ClockUnavailableError):
        _clock(source, mono).now()


def test_a_failed_refresh_is_tolerated_only_within_the_staleness_bound() -> None:
    mono, source = Mono(), Source()
    clock = _clock(source, mono, refresh_seconds=1.0, max_staleness_seconds=5.0)
    clock.now()
    source.fail = True
    mono.value += 2.0
    assert clock.now() is not None  # still within the bound: keep advancing from the snapshot
    mono.value += 10.0
    with pytest.raises(ClockUnavailableError):
        clock.now()


def test_snapshot_beyond_the_bound_is_refused_even_if_the_source_answers_again() -> None:
    mono, source = Mono(), Source()
    clock = _clock(source, mono, refresh_seconds=1.0, max_staleness_seconds=5.0)
    clock.now()
    source.status = 503
    mono.value += 6.0
    with pytest.raises(ClockUnavailableError):
        clock.now()


def test_bad_snapshot_is_rejected() -> None:
    mono = Mono()
    source = Source({"generation": "gen-1"})
    with pytest.raises(ClockUnavailableError):
        _clock(source, mono).now()


def test_expected_generation_is_required() -> None:
    with pytest.raises(ValueError):
        RemoteMarketClock("http://x", expected_generation="")


def test_system_market_clock_is_utc_wall_time() -> None:
    now = SystemMarketClock().now()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 0


def test_worker_heartbeat_uses_operational_time_while_decisions_use_market_time(db_engine) -> None:
    """A replay market far from wall time must not change lease or heartbeat timestamps."""
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from sqlalchemy.orm import sessionmaker

    from q_backend.execution.brokers.fakes import FixedClock
    from q_backend.execution.worker import ExecutionWorker
    from q_backend.storage.db.execution_repositories import get_worker_heartbeat
    from q_backend.storage.settings import Settings

    operational_now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    market_now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)  # replayed history, far from wall time
    recovery = MagicMock()
    recovery.recover.return_value = SimpleNamespace(
        failed_closed=False, message="", leases_released=0, unknown_orders_marked=0
    )
    worker = ExecutionWorker(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False),
        coordinator=MagicMock(),
        quote_source=MagicMock(),
        broker=MagicMock(),
        ledger=MagicMock(),
        service=MagicMock(),
        recovery=recovery,
        settings=Settings(execution_worker_id="w-clock", execution_edge_health_interval_s=5.0),
        clock=FixedClock(market_now).now,
        operational_clock=FixedClock(operational_now).now,
        poll_interval_seconds=0.0,
        edge_health=None,
        version="9.9",
    )
    worker._started_at = operational_now - timedelta(minutes=1)
    worker.poll_once()
    with sessionmaker(bind=db_engine)() as session:
        row = get_worker_heartbeat(session, "w-clock")
        session.expunge_all()
    assert row.heartbeat_at.replace(tzinfo=UTC) == operational_now
