import pytest

from q_backend.streaming.outbox import (
    OutboxEnvelopeError,
    OutboxTopicError,
    record_event,
)
from tests.streaming.conftest import SEEDED_EPOCH


def test_record_event_ephemeral_topic_raises(seeded_session):
    with pytest.raises(OutboxTopicError):
        record_event(
            seeded_session,
            "quotes",
            {"symbol": "EURUSD", "bid": 1.05, "ask": 1.06},
            payload_schema="schema/api/arrow/ticks.schema.json",
            producer_id="test-producer",
        )


def test_record_event_undeclared_topic_raises(seeded_session):
    with pytest.raises(OutboxTopicError):
        record_event(
            seeded_session,
            "nope",
            {"some": "data"},
            payload_schema="schema/some/schema.json",
            producer_id="test-producer",
        )


def test_record_event_missing_job_id_raises(seeded_session):
    with pytest.raises(OutboxEnvelopeError):
        record_event(
            seeded_session,
            "jobs.terminal",
            {
                "kind": "backtest",
                "status": "completed",
                "finished_at": "2026-09-12T00:00:00Z",
                # missing job_id
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id="test-producer",
        )


def test_record_event_sequence_numbers(seeded_session):
    ev1 = record_event(
        seeded_session,
        "jobs.terminal",
        {
            "job_id": "job-1",
            "kind": "backtest",
            "status": "completed",
            "finished_at": "2026-09-12T00:00:00Z",
        },
        payload_schema="schema/stream/payloads/job-terminal.schema.json",
        producer_id="test-producer",
    )
    assert ev1.seq == 1
    assert ev1.epoch == SEEDED_EPOCH
    assert ev1.topic == "jobs.terminal"

    ev2 = record_event(
        seeded_session,
        "jobs.terminal",
        {
            "job_id": "job-2",
            "kind": "optimization",
            "status": "failed",
            "error": "out of bounds",
            "finished_at": "2026-09-12T00:01:00Z",
        },
        payload_schema="schema/stream/payloads/job-terminal.schema.json",
        producer_id="test-producer",
    )
    assert ev2.seq == 2
    assert ev2.epoch == SEEDED_EPOCH
    assert ev2.topic == "jobs.terminal"
