from q_contracts.topics import TOPICS

from q_backend.streaming.ws.queue import OfferResult, QueuedEntry, TopicQueue


def _entry(seq: int, routing_key: str | None = None) -> QueuedEntry:
    return QueuedEntry(b"1-0", seq, "epoch", routing_key, f"frame-{seq}")


def test_quotes_coalesce_in_place_and_bound_distinct_routing_keys():
    """Replacing a quote at the tail would reorder a quiet symbol behind a hot one."""
    queue = TopicQueue("quotes", TOPICS["quotes"], capacity=2)

    queue.offer(_entry(1, "A"))
    queue.offer(_entry(2, "B"))
    assert queue.offer(_entry(3, "A")) is OfferResult.COALESCED

    assert [queue.pop().seq, queue.pop().seq] == [3, 2]
    queue.offer(_entry(4, "A"))
    queue.offer(_entry(5, "B"))
    assert queue.offer(_entry(6, "C")) is OfferResult.OVERFLOW


def test_durable_overflow_marks_the_first_undelivered_sequence():
    """Reporting the wrong sequence would make a durable client resnapshot from a gap."""
    queue = TopicQueue("jobs.terminal", TOPICS["jobs.terminal"], capacity=3)

    for seq in range(1, 4):
        assert queue.offer(_entry(seq)) is OfferResult.ACCEPTED

    assert queue.offer(_entry(4)) is OfferResult.OVERFLOW
    assert queue.lagging_from_seq == 4


def test_non_durable_lag_queue_marks_the_first_dropped_sequence():
    """An incorrect lag watermark would hide a missing completed-bar range."""
    queue = TopicQueue("bars.completed", TOPICS["bars.completed"], capacity=2)

    queue.offer(_entry(1))
    queue.offer(_entry(2))

    assert queue.offer(_entry(3)) is OfferResult.OVERFLOW
    assert queue.lagging_from_seq == 3
