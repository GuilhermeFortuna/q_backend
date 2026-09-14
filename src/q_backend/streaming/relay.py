from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import threading
import time
from typing import Any

import redis
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from q_backend.storage.db.base import utc_now
from q_backend.storage.db.outbox_models import OutboxEvent, OutboxTopicState
from q_backend.streaming.codec import encode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.outbox import oldest_retained_seq, prune_relayed
from q_backend.streaming.redis_binary import ensure_stream_epoch
from q_contracts.stream import StreamEnvelope
from q_contracts.topics import TOPICS

logger = logging.getLogger(__name__)


class _LiveClock:
    def now(self) -> datetime:
        return utc_now()


@dataclass(frozen=True)
class RelayConfig:
    poll_interval_s: float = 0.2
    batch_size: int = 500
    prune_interval_s: float = 3600.0
    max_backoff_s: float = 30.0


class OutboxRelay:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        client: redis.Redis,
        config: RelayConfig | None = None,
        clock: Any = None,
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.config = config or RelayConfig()
        self.clock = clock or _LiveClock()
        self._current_stream_epoch: str | None = None

    def _get_now(self) -> datetime:
        if hasattr(self.clock, "now"):
            return self.clock.now()
        if callable(self.clock):
            return self.clock()
        return utc_now()

    def republish_retained(self) -> dict[str, int]:
        """Republish each durable topic's retained window on stream epoch creation."""
        republished: dict[str, int] = {}
        with self.session_factory() as session:
            for topic, policy in TOPICS.items():
                if policy.topic_class != "durable":
                    continue

                state = session.execute(
                    sa.select(OutboxTopicState).where(OutboxTopicState.topic == topic)
                ).scalar_one_or_none()

                if state is None or state.last_relayed_seq == 0:
                    continue

                oldest = oldest_retained_seq(session, topic)
                if oldest is None:
                    continue

                retention_entries = policy.retention_entries
                start_seq = max(oldest, state.last_relayed_seq - retention_entries + 1)
                end_seq = state.last_relayed_seq
                if start_seq > end_seq:
                    continue

                events = (
                    session.execute(
                        sa.select(OutboxEvent)
                        .where(
                            OutboxEvent.topic == topic,
                            OutboxEvent.epoch == state.epoch,
                            OutboxEvent.seq >= start_seq,
                            OutboxEvent.seq <= end_seq,
                        )
                        .order_by(OutboxEvent.seq.asc())
                    )
                    .scalars()
                    .all()
                )

                if not events:
                    continue

                pipe = self.client.pipeline()
                s_key = stream_key(topic)
                maxlen = policy.retention_entries

                for event in events:
                    env = StreamEnvelope(
                        topic=event.topic,  # type: ignore[arg-type]
                        schema_major=1,
                        seq=event.seq,
                        epoch=event.epoch,
                        producer_id=event.producer_id,
                        origin_ts=(
                            event.origin_ts.isoformat()
                            if isinstance(event.origin_ts, datetime)
                            else str(event.origin_ts)
                        ),
                        payload_kind=event.payload_kind,  # type: ignore[arg-type]
                        payload_schema=event.payload_schema,
                        payload=event.payload,
                        key=event.routing_key,
                    )
                    fields = encode_entry(env)
                    pipe.xadd(s_key, fields, maxlen=maxlen, approximate=True)

                pipe.execute()
                republished[topic] = len(events)
                logger.info(
                    "Republished %d retained events for topic %s (seq %d..%d)",
                    len(events),
                    topic,
                    start_seq,
                    end_seq,
                )

        return republished

    def _record_progress(self, session: Session, topic: str, epoch: str, last_seq: int) -> None:
        session.execute(
            sa.update(OutboxTopicState)
            .where(OutboxTopicState.topic == topic, OutboxTopicState.epoch == epoch)
            .values(last_relayed_seq=last_seq)
        )
        session.commit()

    def run_once(self) -> int:
        """Process one batch of unrelayed events across durable topics. Returns total events appended."""
        epoch, created = ensure_stream_epoch(self.client)
        if created or (self._current_stream_epoch is not None and epoch != self._current_stream_epoch):
            self.republish_retained()
        self._current_stream_epoch = epoch

        total_appended = 0
        with self.session_factory() as session:
            for topic, policy in TOPICS.items():
                if policy.topic_class != "durable":
                    continue

                state = session.execute(
                    sa.select(OutboxTopicState).where(OutboxTopicState.topic == topic)
                ).scalar_one_or_none()

                if state is None or state.last_seq <= state.last_relayed_seq:
                    continue

                events = (
                    session.execute(
                        sa.select(OutboxEvent)
                        .where(
                            OutboxEvent.topic == topic,
                            OutboxEvent.epoch == state.epoch,
                            OutboxEvent.seq > state.last_relayed_seq,
                        )
                        .order_by(OutboxEvent.seq.asc())
                        .limit(self.config.batch_size)
                    )
                    .scalars()
                    .all()
                )

                if not events:
                    continue

                pipe = self.client.pipeline()
                s_key = stream_key(topic)
                maxlen = policy.retention_entries

                for event in events:
                    env = StreamEnvelope(
                        topic=event.topic,  # type: ignore[arg-type]
                        schema_major=1,
                        seq=event.seq,
                        epoch=event.epoch,
                        producer_id=event.producer_id,
                        origin_ts=(
                            event.origin_ts.isoformat()
                            if isinstance(event.origin_ts, datetime)
                            else str(event.origin_ts)
                        ),
                        payload_kind=event.payload_kind,  # type: ignore[arg-type]
                        payload_schema=event.payload_schema,
                        payload=event.payload,
                        key=event.routing_key,
                    )
                    fields = encode_entry(env)
                    pipe.xadd(s_key, fields, maxlen=maxlen, approximate=True)

                pipe.execute()

                # Record progress ONLY after append is confirmed in Redis
                last_seq = events[-1].seq
                self._record_progress(session, topic, state.epoch, last_seq)
                total_appended += len(events)

        return total_appended

    def run_forever(
        self,
        stop: threading.Event,
        on_first_success: Callable[[], None] | None = None,
    ) -> None:
        """Run the relay loop until stop event is set, backing off exponentially on outages."""
        backoff = 0.5
        last_prune = time.monotonic()
        first_success_notified = False

        while not stop.is_set():
            try:
                appended = self.run_once()
                backoff = 0.5

                if on_first_success is not None and not first_success_notified:
                    try:
                        on_first_success()
                    except Exception as cb_err:  # noqa: BLE001
                        logger.warning("on_first_success callback failed: %s", cb_err)
                    first_success_notified = True

                now = time.monotonic()
                if now - last_prune >= self.config.prune_interval_s:
                    try:
                        with self.session_factory() as session:
                            prune_relayed(session)
                            session.commit()
                        last_prune = now
                    except sa.exc.SQLAlchemyError as prune_err:
                        logger.warning("Periodic outbox prune failed: %s", prune_err)

                if appended < self.config.batch_size:
                    stop.wait(self.config.poll_interval_s)
            except (redis.RedisError, sa.exc.SQLAlchemyError) as exc:
                logger.warning("Relay outage: %s. Backing off for %.2fs", exc, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, self.config.max_backoff_s)
            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected error in relay loop: %s. Backing off for %.2fs", exc, backoff, exc_info=True)
                stop.wait(backoff)
                backoff = min(backoff * 2, self.config.max_backoff_s)
