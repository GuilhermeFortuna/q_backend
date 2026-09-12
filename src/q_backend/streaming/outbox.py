from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
import logging
import secrets
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from q_backend.storage.db.base import utc_now
from q_backend.storage.db.outbox_models import OutboxEvent, OutboxTopicState
from q_contracts.stream import JobProgressPayload, JobTerminalPayload, StreamEnvelope
from q_contracts.topics import TOPICS

logger = logging.getLogger(__name__)

PAYLOAD_MODELS: dict[str, type] = {
    "schema/stream/payloads/job-terminal.schema.json": JobTerminalPayload,
    "schema/stream/payloads/job-progress.schema.json": JobProgressPayload,
}


class OutboxTopicError(ValueError):
    """Raised when an ephemeral or undeclared topic is supplied to the outbox."""


class OutboxEnvelopeError(ValueError):
    """Raised when an event envelope, payload, or routing key is invalid under contracts."""


def record_event(
    session: Session,
    topic: str,
    payload: Mapping[str, Any],
    *,
    payload_schema: str,
    producer_id: str,
    routing_key: Mapping[str, str] | None = None,
    origin_ts: datetime | None = None,
) -> OutboxEvent:
    """Issue the next seq for topic and add the event to session's transaction.

    Convention:
        The counter row in stream_outbox_topic_state is locked last in the caller's
        transaction. Any validation or preceding work should happen before calling
        record_event so lock contention on the topic is minimized.
    """
    # 1. Topic discipline
    if topic not in TOPICS or TOPICS[topic].topic_class != "durable":
        session.rollback()
        raise OutboxTopicError(f"Topic {topic!r} is not a declared durable topic")

    policy = TOPICS[topic]

    # 2. Routing key check against topic coalesce_key
    if policy.coalesce_key:
        if not routing_key:
            session.rollback()
            raise OutboxEnvelopeError(f"Topic {topic!r} requires routing key fields: {policy.coalesce_key}")
        for k in policy.coalesce_key:
            if k not in routing_key or routing_key[k] is None or routing_key[k] == "":
                session.rollback()
                raise OutboxEnvelopeError(f"Missing required routing key field: {k!r}")

    # 3. Payload validation
    if not isinstance(payload, Mapping):
        session.rollback()
        raise OutboxEnvelopeError("Payload must be a mapping")

    if payload_schema in PAYLOAD_MODELS:
        try:
            PAYLOAD_MODELS[payload_schema](**payload)
        except Exception as exc:
            session.rollback()
            raise OutboxEnvelopeError(f"Payload does not match schema {payload_schema}: {exc}") from exc
    elif policy.payload_schema != "schema/stream/envelope.schema.json" and payload_schema != policy.payload_schema:
        session.rollback()
        raise OutboxEnvelopeError(
            f"Payload schema {payload_schema!r} does not match declared topic schema {policy.payload_schema!r}"
        )

    # 4. Lock counter row and increment seq
    stmt = (
        sa.update(OutboxTopicState)
        .where(OutboxTopicState.topic == topic)
        .values(last_seq=OutboxTopicState.last_seq + 1)
        .returning(OutboxTopicState.epoch, OutboxTopicState.last_seq)
    )
    result = session.execute(stmt).first()
    if result is None:
        session.rollback()
        raise RuntimeError(f"Topic {topic!r} has no initialized state in stream_outbox_topic_state")
    epoch, seq = result[0], result[1]

    # 5. Envelope construction check
    event_origin_ts = origin_ts or utc_now()
    try:
        StreamEnvelope(
            epoch=epoch,
            origin_ts=event_origin_ts.isoformat(),
            payload=dict(payload),
            payload_kind="control",
            payload_schema=payload_schema,
            producer_id=producer_id,
            schema_major=1,
            seq=seq,
            topic=topic,  # type: ignore[arg-type]
            key=dict(routing_key) if routing_key is not None else None,
        )
    except Exception as exc:
        session.rollback()
        raise OutboxEnvelopeError(f"Envelope validation failed: {exc}") from exc

    # 6. Add event to session
    event = OutboxEvent(
        topic=topic,
        epoch=epoch,
        seq=seq,
        producer_id=producer_id,
        origin_ts=event_origin_ts,
        routing_key=dict(routing_key) if routing_key is not None else None,
        payload_kind="control",
        payload_schema=payload_schema,
        payload=dict(payload),
        recorded_at=utc_now(),
    )
    session.add(event)
    session.flush()
    return event


def read_watermark(session: Session, topics: Iterable[str]) -> dict[str, tuple[str, int]]:
    """(epoch, max visible seq) per topic, as seen by session's snapshot."""
    topic_list = list(topics)
    for t in topic_list:
        if t not in TOPICS or TOPICS[t].topic_class != "durable":
            raise OutboxTopicError(f"Topic {t!r} is not a declared durable topic")

    if not topic_list:
        return {}

    stmt = sa.select(
        OutboxTopicState.topic,
        OutboxTopicState.epoch,
        OutboxTopicState.last_seq,
    ).where(OutboxTopicState.topic.in_(topic_list))
    rows = session.execute(stmt).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def prune_relayed(
    session: Session,
    *,
    older_than: timedelta = timedelta(days=30),
) -> dict[str, int]:
    """Delete relayed events older than the cutoff; return oldest retained seq per topic."""
    cutoff = utc_now() - older_than
    states = session.execute(sa.select(OutboxTopicState.topic, OutboxTopicState.last_relayed_seq)).all()
    for state in states:
        if state[1] > 0:
            del_stmt = sa.delete(OutboxEvent).where(
                OutboxEvent.topic == state[0],
                OutboxEvent.recorded_at < cutoff,
                OutboxEvent.seq <= state[1],
            )
            session.execute(del_stmt)
    session.flush()

    min_stmt = sa.select(OutboxEvent.topic, sa.func.min(OutboxEvent.seq)).group_by(OutboxEvent.topic)
    min_rows = session.execute(min_stmt).all()
    return {row[0]: row[1] for row in min_rows if row[1] is not None}


def oldest_retained_seq(session: Session, topic: str) -> int | None:
    """Return the oldest retained sequence number for topic, or None if no events exist."""
    stmt = sa.select(sa.func.min(OutboxEvent.seq)).where(OutboxEvent.topic == topic)
    return session.execute(stmt).scalar_one_or_none()


def rotate_epoch(session: Session, topic: str, *, reason: str) -> tuple[str, str]:
    """Operator action: new epoch, seq restarts at 0. Returns (old, new)."""
    if topic not in TOPICS or TOPICS[topic].topic_class != "durable":
        raise OutboxTopicError(f"Topic {topic!r} is not a declared durable topic")
    if not reason or not reason.strip():
        raise ValueError("Reason is required for epoch rotation")

    now = utc_now()
    new_epoch = f"{now.strftime('%Y%m%d')}-{secrets.token_hex(4)}"

    stmt = sa.select(OutboxTopicState).where(OutboxTopicState.topic == topic).with_for_update()
    state = session.execute(stmt).scalar_one_or_none()
    if state is None:
        raise RuntimeError(f"Topic {topic!r} has no initialized state in stream_outbox_topic_state")

    old_epoch = state.epoch
    state.epoch = new_epoch
    state.last_seq = 0
    state.last_relayed_seq = 0
    session.flush()

    logger.info(
        "Rotated epoch for topic %r from %s to %s. Reason: %s",
        topic,
        old_epoch,
        new_epoch,
        reason,
    )
    return old_epoch, new_epoch
