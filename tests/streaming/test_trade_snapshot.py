"""Immutable trade snapshots: cache limits, tokens, and the cross-process request channel."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pyarrow as pa
import pytest

from q_backend.streaming.market.trade_history import (
    CacheResourceLimit,
    InvalidCursor,
    PublisherUnavailable,
    SnapshotExpired,
    SnapshotNotFound,
    TradeHistoryReader,
    TradeService,
    TradeSnapshotCache,
    TradeSnapshotClient,
    decode_cursor,
)
from q_backend.streaming.market.trades import TradeSessionCoordinator
from tests.streaming.trade_fakes import FakeClock, FakeSink, FakeTradeSource, Mono, row

SYMBOL = "WINZ26"


def _ready(source=None, sink=None, clock=None, symbol=SYMBOL, **kwargs):
    source = source or FakeTradeSource([row(-9000 + i // 2, 100.0 + i) for i in range(20)])
    sink = sink or FakeSink()
    clock = clock or FakeClock()
    kwargs.setdefault("settle_ms", 100)
    coordinator = TradeSessionCoordinator(
        source, symbol, sink, clock=clock, chunk_ms=3_600_000, monotonic=Mono(), retry_backoff_s=0.0, **kwargs
    )
    while coordinator.backfilling:
        coordinator.step(budget_s=60)
    return coordinator, sink, clock


def _cache(tmp_path, clock=None, **kwargs):
    clock = clock or FakeClock()
    return TradeSnapshotCache(tmp_path / "cache", clock=clock, **kwargs), clock


def _page_rows(data: bytes) -> list[dict]:
    return pa.ipc.open_stream(data).read_all().to_pylist()


def test_pages_are_immutable_while_live_trades_append_and_other_tokens_evict(tmp_path):
    coordinator, sink, clock = _ready()
    cache, cache_clock = _cache(tmp_path, ttl=timedelta(minutes=10))
    first = cache.write(coordinator.freeze(), instance_id="i1")
    before, rows_before, next_before, pages = cache.read_page(first["snapshot_id"], None, 7)
    assert rows_before == 7 and pages == 3 and next_before is not None

    coordinator.client.rows += [row(i) for i in range(5)]
    clock.advance(seconds=3)
    coordinator.step()
    second = cache.write(coordinator.freeze(), instance_id="i1")
    cache_clock.advance(minutes=11)  # both expire; a third write sweeps the oldest
    cache.sweep()

    assert first["trade_count"] == 20 and second["trade_count"] == 25
    assert first["frozen_watermark"] != second["frozen_watermark"]
    with pytest.raises(SnapshotExpired):
        cache.read_page(first["snapshot_id"], None, 7)
    assert sink.seq >= 1


def test_page_bytes_and_watermark_are_stable_across_reads_and_pages_cover_every_row_once(tmp_path):
    coordinator, _sink, _clock = _ready()
    cache, _ = _cache(tmp_path)
    descriptor = cache.write(coordinator.freeze(), instance_id="i1")
    snapshot_id = descriptor["snapshot_id"]

    collected, cursor = [], None
    while True:
        data, rows, cursor, pages = cache.read_page(snapshot_id, cursor, 6)
        assert cache.read_page(snapshot_id, None, 6)[0] == cache.read_page(snapshot_id, None, 6)[0]
        collected += _page_rows(data)
        if cursor is None:
            break

    assert len(collected) == descriptor["trade_count"] == 20
    assert [(r["time_msc"], r["occurrence"]) for r in collected] == sorted(
        {(r["time_msc"], r["occurrence"]) for r in collected}
    )
    assert collected[0]["time_msc"].tzinfo is not None
    assert pages == 4


def test_page_schema_binds_the_delivery_context(tmp_path):
    coordinator, _sink, _clock = _ready(FakeTradeSource([row(-9000, real=2.0)], field="volume_real", unit="contracts"))
    cache, _ = _cache(tmp_path)
    descriptor = cache.write(coordinator.freeze(), instance_id="i1")
    data = cache.read_page(descriptor["snapshot_id"], None, 10)[0]
    metadata = pa.ipc.open_stream(data).schema.metadata

    assert metadata[b"source_generation"].decode() == descriptor["source_generation"]
    assert metadata[b"volume_field"] == b"volume_real" and metadata[b"volume_unit"] == b"contracts"
    assert metadata[b"session_key"] == b"2026-10-01"
    assert _page_rows(data)[0]["volume_real"] == 2.0


def test_empty_session_snapshot_is_a_complete_empty_history(tmp_path):
    coordinator, _sink, _clock = _ready(FakeTradeSource([]))
    cache, _ = _cache(tmp_path)
    descriptor = cache.write(coordinator.freeze(), instance_id="i1")
    data, rows, cursor, pages = cache.read_page(descriptor["snapshot_id"], None, 100)

    assert (rows, cursor, pages) == (0, None, 0)
    assert _page_rows(data) == [] and descriptor["trade_count"] == 0
    assert descriptor["coverage"]["coverage_state"] == "complete"


def test_cache_never_truncates_and_reports_resource_limits(tmp_path):
    coordinator, _sink, _clock = _ready()
    frozen = coordinator.freeze()
    cache, clock = _cache(tmp_path)
    first = cache.write(frozen, instance_id="i1")
    cache.max_bytes = 11_500  # two ~5 kB tokens fit, a third does not
    cache.write(frozen, instance_id="i1")

    with pytest.raises(CacheResourceLimit):  # both tokens are still active
        cache.write(frozen, instance_id="i1")
    assert cache.read_descriptor(first["snapshot_id"])["trade_count"] == 20

    clock.advance(minutes=11)
    third = cache.write(frozen, instance_id="i1")  # expired tokens make room
    assert third["trade_count"] == 20


def test_oldest_expired_token_is_evicted_first(tmp_path):
    coordinator, _sink, _clock = _ready()
    frozen = coordinator.freeze()
    cache, clock = _cache(tmp_path, ttl=timedelta(minutes=1))
    old = cache.write(frozen, instance_id="i1")
    cache.max_bytes = 13_000  # evicting only the oldest expired token is enough
    clock.advance(seconds=30)
    newer = cache.write(frozen, instance_id="i1")
    clock.advance(seconds=45)  # old is expired, newer is still active
    cache.write(frozen, instance_id="i1")

    with pytest.raises(SnapshotExpired):  # evicted rows leave a tombstone: expired, not unknown
        cache.read_page(old["snapshot_id"], None, 5)
    assert cache.read_descriptor(newer["snapshot_id"])["snapshot_id"] == newer["snapshot_id"]


def test_a_snapshot_larger_than_the_cache_is_refused_not_cut(tmp_path):
    coordinator, _sink, _clock = _ready()
    cache, _ = _cache(tmp_path, max_bytes=100)
    with pytest.raises(CacheResourceLimit):
        cache.write(coordinator.freeze(), instance_id="i1")
    assert cache._tokens() == []


@pytest.mark.parametrize("token", ["../etc", "..", "a/b", "short", "x" * 100, "../../" + "a" * 20, ".tmp-" + "a" * 20])
def test_tokens_are_opaque_and_contained_in_the_cache_root(tmp_path, token):
    cache, _ = _cache(tmp_path)
    with pytest.raises(SnapshotNotFound):
        cache.read_descriptor(token)


def test_cursors_are_bound_to_their_snapshot(tmp_path):
    coordinator, _sink, _clock = _ready()
    cache, _ = _cache(tmp_path)
    one = cache.write(coordinator.freeze(), instance_id="i1")
    two = cache.write(coordinator.freeze(), instance_id="i1")
    _data, _rows, cursor, _pages = cache.read_page(one["snapshot_id"], None, 5)

    assert decode_cursor(one["snapshot_id"], cursor) == 5
    with pytest.raises(InvalidCursor):
        cache.read_page(two["snapshot_id"], cursor, 5)
    with pytest.raises(InvalidCursor):
        cache.read_page(one["snapshot_id"], "not-a-cursor!", 5)


def _channel(tmp_path, coordinators, server=None, **service_kwargs):
    """A publisher-side service and an API-side client sharing only Redis and the cache dir."""
    server = server or fakeredis.FakeServer()
    publisher_redis = fakeredis.FakeRedis(server=server)
    api_redis = fakeredis.FakeRedis(server=server)
    publisher_cache = TradeSnapshotCache(tmp_path / "shared")
    api_cache = TradeSnapshotCache(tmp_path / "shared")
    service = TradeService(publisher_redis, publisher_cache, coordinators, **service_kwargs)
    return service, TradeSnapshotClient(api_redis, timeout_s=5), TradeHistoryReader(api_cache, api_redis)


class _Pump:
    """Runs the publisher side on its own thread, as the real process would."""

    def __init__(self, service):
        self.service, self.stop = service, threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            self.service.step()
            self.stop.wait(0.005)

    def __enter__(self):
        self.service.start()
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join(timeout=5)


def test_independent_processes_share_snapshots_only_through_redis_and_the_cache(tmp_path):
    coordinator, _sink, _clock = _ready()
    service, client, reader = _channel(tmp_path, {SYMBOL: coordinator})

    with _Pump(service):
        outcome = client.request_snapshot(SYMBOL)
        assert outcome.kind == "ready"
        descriptor = outcome.descriptor
        _desc, data, next_cursor, pages = reader.read(descriptor["snapshot_id"], None, 8)

    assert len(_page_rows(data)) == 8 and next_cursor is not None and pages == 3
    assert descriptor["source_generation"] == coordinator.generation
    assert descriptor["coverage"]["source_generation"] == descriptor["source_generation"]


def test_concurrent_requests_share_one_snapshot_and_never_see_mixed_watermarks(tmp_path):
    coordinator, _sink, _clock = _ready()
    service, client, reader = _channel(tmp_path, {SYMBOL: coordinator})
    results, errors = [], []

    def ask():
        try:
            outcome = TradeSnapshotClient(client.client, timeout_s=10).request_snapshot(SYMBOL)
            descriptor = outcome.descriptor
            _d, data, _n, _p = reader.read(descriptor["snapshot_id"], None, 50)
            results.append((descriptor["snapshot_id"], descriptor["frozen_watermark"], len(_page_rows(data))))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    with _Pump(service):
        threads = [threading.Thread(target=ask) for _ in range(8)]
        [t.start() for t in threads]
        [t.join(timeout=15) for t in threads]

    assert not errors
    assert len(results) == 8
    assert len({r[1]["seq"] for r in results}) == 1 and {r[2] for r in results} == {20}
    assert len({r[0] for r in results}) <= 8


def test_snapshot_request_during_backfill_is_pending_with_progress_not_empty_complete(tmp_path):
    source = FakeTradeSource([row(-9000)])
    coordinator = TradeSessionCoordinator(
        source, SYMBOL, FakeSink(), clock=FakeClock(), chunk_ms=1_000, monotonic=Mono(), retry_backoff_s=0.0
    )
    service, client, _reader = _channel(tmp_path, {SYMBOL: coordinator}, step_budget_s=0.0)
    service.start()
    client.client.rpush("q:trades:requests", "ignored")  # malformed requests are skipped

    import json
    import time

    request = {"id": "r" * 16, "op": "snapshot", "symbol": SYMBOL, "ts": time.time()}
    service.client.rpush("q:trades:requests", json.dumps(request))
    service.serve_requests()
    reply = json.loads(service.client.lpop("q:trades:result:" + "r" * 16))

    assert reply["outcome"] == "pending"
    assert reply["pending"]["status"] == "backfill_pending"
    assert reply["pending"]["status_token"].startswith("backfill-gen-")


def test_unknown_symbols_and_unavailable_sources_are_explicit(tmp_path):
    source = FakeTradeSource([])
    source.unknown_symbol = True
    coordinator = TradeSessionCoordinator(
        source, "NOPE", FakeSink(), clock=FakeClock(), chunk_ms=3_600_000, monotonic=Mono(), retry_backoff_s=0.0
    )
    service, client, _reader = _channel(tmp_path, {"NOPE": coordinator, SYMBOL: _ready()[0]})

    with _Pump(service):
        assert client.request_snapshot("MISSING").kind == "unknown_symbol"
        assert client.request_snapshot("NOPE").kind == "unknown_symbol"


def test_resource_limit_outcome_reaches_the_api_side(tmp_path):
    coordinator, _sink, _clock = _ready()
    service, client, _reader = _channel(tmp_path, {SYMBOL: coordinator})
    service.cache.max_bytes = 100

    with _Pump(service):
        outcome = client.request_snapshot(SYMBOL)

    assert outcome.kind == "resource_limit"
    assert client.client.hget("q:trades:diagnostics", "snapshot_resource_limit") == b"1"


def test_only_the_configured_number_of_backfills_run_at_once(tmp_path):
    clock = FakeClock()
    coordinators = {
        name: TradeSessionCoordinator(
            FakeTradeSource([row(-9000)]),
            name,
            FakeSink(),
            clock=clock,
            chunk_ms=1_000,
            monotonic=Mono(),
            retry_backoff_s=0.0,
        )
        for name in ("A", "B", "C")
    }
    service, _client, _reader = _channel(tmp_path, coordinators, max_backfills=2, step_budget_s=0.004)
    service.step()

    progressed = [name for name, c in coordinators.items() if c.diagnostics.backfill_chunks > 0]
    assert progressed == ["A", "B"]


def test_publisher_restart_and_generation_change_expire_tokens(tmp_path):
    coordinator, _sink, _clock = _ready()
    service, client, reader = _channel(tmp_path, {SYMBOL: coordinator})
    with _Pump(service):
        snapshot_id = client.request_snapshot(SYMBOL).descriptor["snapshot_id"]
        reader.read(snapshot_id, None, 5)

        coordinator._begin_generation("overlap_mismatch")  # a source correction elsewhere in the loop
        service.step()
        with pytest.raises(SnapshotExpired) as replaced:
            reader.read(snapshot_id, None, 5)
        assert replaced.value.reason == "source_generation_replaced"

    # A new publisher instance expires everything the old one issued.
    coordinator2, _sink2, _clock2 = _ready()
    service2, client2, reader2 = _channel(tmp_path, {SYMBOL: coordinator2})
    old = service2.cache.write(coordinator2.freeze(), instance_id="previous-instance")
    service2.start()
    with pytest.raises(SnapshotExpired) as restarted:
        reader2.read(old["snapshot_id"], None, 5)
    assert restarted.value.reason == "publisher_restarted"


def test_expired_and_missing_publisher_are_distinct_failures(tmp_path):
    coordinator, _sink, _clock = _ready()
    service, client, reader = _channel(tmp_path, {SYMBOL: coordinator})
    with pytest.raises(PublisherUnavailable):
        client.request_snapshot(SYMBOL)
    with _Pump(service):
        snapshot_id = client.request_snapshot(SYMBOL).descriptor["snapshot_id"]
    reader.cache.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(SnapshotExpired) as expired:
        reader.read(snapshot_id, None, 5)
    assert expired.value.reason == "snapshot_expired"
    assert client.client.hget("q:trades:diagnostics", "snapshot_expired") == b"1"


def test_cache_directory_contains_only_whole_tokens(tmp_path):
    coordinator, _sink, _clock = _ready()
    cache, _ = _cache(tmp_path)
    cache.write(coordinator.freeze(), instance_id="i1")
    entries = sorted(p.name for p in Path(cache.root).iterdir())
    assert len(entries) == 1 and not entries[0].startswith(".")
    assert sorted(p.name for p in (Path(cache.root) / entries[0]).iterdir()) == ["descriptor.json", "rows.arrow"]
