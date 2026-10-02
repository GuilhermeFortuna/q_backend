"""Subscribe -> buffered live -> paged history -> watermark discard, scripted end to end."""

from __future__ import annotations

import pyarrow as pa

from q_backend.streaming.market.trade_history import SnapshotExpired
from q_backend.streaming.ws.queue import OfferResult, QueuedEntry, TopicQueue
from q_contracts.topics import TOPICS
from tests.streaming.trade_fakes import FakeTradeSource, World, row


def _key(record):
    return (int(record["time_msc"].timestamp() * 1000), record["occurrence"], record["price"])


def _load_history(world, descriptor, limit=7):
    rows, cursor = [], None
    while True:
        _d, data, cursor, _pages = world.reader.read(descriptor["snapshot_id"], cursor, limit)
        rows += pa.ipc.open_stream(data).read_all().to_pylist()
        if cursor is None:
            return rows


def test_history_plus_buffered_stream_equals_one_ordered_source_sequence(tmp_path):
    source = FakeTradeSource([row(-9000, 100.0), row(-8000, 101.0), row(-8000, 101.0), row(-800, 102.0)])
    world = World(tmp_path, source)
    world.backfill()

    # 1. Subscribe: from now on every delivery is buffered.
    # 2. Live trades keep arriving, including equal prints and a late print in a closed group.
    world.live(row(-800, 102.0), row(300, 103.0), row(300, 103.0), advance_s=2)
    world.live(row(1500, 104.0), advance_s=2)

    # 3. The snapshot freezes the confirmed prefix and the watermark atomically.
    frozen = world.coordinator.freeze()
    descriptor = world.service.cache.write(frozen, instance_id=world.service.instance_id)

    # 4. More live trades arrive while the pages are being fetched.
    world.live(row(2500, 105.0), row(2500, 105.0), advance_s=3)
    world.live(row(9000, 106.0), advance_s=3)

    buffered = world.stream_entries()
    watermark = descriptor["frozen_watermark"]
    history = _load_history(world, descriptor)
    applied = [
        r for seq, epoch, rows, _m in buffered if (epoch, seq) > (watermark["epoch"], watermark["seq"]) for r in rows
    ]

    # 5. Exactly the source's ordered sequence: nothing missing, nothing doubled.
    expected, counts = [], {}
    for r in sorted(source.rows, key=lambda r: r[0]):
        expected.append((r[0], counts.get(r[0], 0), r[1]))
        counts[r[0]] = counts.get(r[0], 0) + 1
    joined = [_key(r) for r in history + applied]
    held = world.coordinator._held  # the still-open newest group is in neither source of truth yet
    open_rows = len(held["time_msc"])
    assert joined == expected[: len(expected) - open_rows]
    assert any(seq <= watermark["seq"] for seq, _e, _r, _m in buffered), "some deliveries precede the watermark"
    assert watermark["seq"] >= 1


def test_every_delivery_carries_its_context_and_one_generation(tmp_path):
    world = World(tmp_path, FakeTradeSource([row(-9000)], field="volume_real", unit="contracts"))
    world.backfill()
    world.live(row(0), row(0), advance_s=2)

    (entry,) = world.stream_entries()
    metadata = entry[3]
    assert metadata[b"source_generation"].decode() == world.coordinator.generation
    assert metadata[b"volume_field"] == b"volume_real" and metadata[b"provider_id"] == b"fake-mt5"
    assert metadata[b"exchange_timezone"] == b"America/Sao_Paulo"
    assert [r["occurrence"] for r in entry[2]] == [0, 1]


def test_generation_change_invalidates_tokens_and_status_reaches_consumers(tmp_path):
    world = World(tmp_path, FakeTradeSource([row(-9000, 100.0), row(-8000, 101.0)]))
    world.backfill()
    snapshot = world.service.cache.write(world.coordinator.freeze(), instance_id=world.service.instance_id)
    world.live()
    world.source.rows[1] = row(-8000, 777.0)  # the provider corrects a confirmed print
    world.live(advance_s=2)

    statuses = world.stream_entries("trades.status")
    reasons = [(s[2]["coverage_state"], s[2]["coverage_reason"]) for s in statuses]
    assert ("partial", "overlap_mismatch") in reasons
    assert statuses[-1][2]["source_generation"] == world.coordinator.generation
    assert [s[0] for s in statuses] == list(range(1, len(statuses) + 1))  # its own sequence

    world.service.step()
    try:
        world.reader.read(snapshot["snapshot_id"], None, 5)
    except SnapshotExpired as exc:
        assert exc.reason == "source_generation_replaced"
    else:  # pragma: no cover - the assertion above must raise
        raise AssertionError("token survived a generation change")


def test_trade_overflow_lags_instead_of_coalescing_identical_prints():
    queue = TopicQueue("trades", TOPICS["trades"], capacity=2)
    entries = [QueuedEntry(b"1-0", seq, "epoch", None, f"frame-{seq}") for seq in (1, 2, 3)]
    assert [queue.offer(e) for e in entries] == [OfferResult.ACCEPTED, OfferResult.ACCEPTED, OfferResult.OVERFLOW]
    assert queue.lagging_from_seq == 3
    assert [queue.pop().seq, queue.pop().seq, queue.pop()] == [1, 2, None]

    status = TopicQueue("trades.status", TOPICS["trades.status"], capacity=4)
    assert status.offer(QueuedEntry(b"1-0", 1, "e", "WINZ26", "a")) is OfferResult.ACCEPTED
    assert status.offer(QueuedEntry(b"2-0", 2, "e", "WINZ26", "b")) is OfferResult.COALESCED


def test_watermark_before_any_trade_names_a_real_epoch_and_sequence_zero(tmp_path):
    world = World(tmp_path, FakeTradeSource([]))
    world.backfill()
    descriptor = world.service.cache.write(world.coordinator.freeze(), instance_id=world.service.instance_id)
    assert descriptor["frozen_watermark"]["seq"] == 0 and descriptor["frozen_watermark"]["epoch"]
    world.live(row(0), advance_s=2)
    assert world.stream_entries()[0][1] == descriptor["frozen_watermark"]["epoch"]
