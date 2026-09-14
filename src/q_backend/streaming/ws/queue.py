"""Bounded per-topic outbound queues with policy-aware overflow."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum

from q_contracts.topics import TopicPolicy


class OfferResult(str, Enum):
    ACCEPTED = "accepted"
    COALESCED = "coalesced"
    OVERFLOW = "overflow"


@dataclass
class QueuedEntry:
    stream_id: bytes
    seq: int
    epoch: str
    routing_key: str | None
    frame: str | bytes


class TopicQueue:
    """Bounded per-client queue that applies the declared topic overflow policy."""

    def __init__(self, topic: str, policy: TopicPolicy, capacity: int) -> None:
        self.topic = topic
        self.policy = policy
        self.capacity = capacity
        self.lagging_from_seq: int | None = None
        self._entries: deque[QueuedEntry] = deque()
        self._coalesced: OrderedDict[str, QueuedEntry] = OrderedDict()

    def offer(self, entry: QueuedEntry) -> OfferResult:
        if self.policy.on_overflow == "coalesce":
            if entry.routing_key is None:
                raise ValueError(f"coalescing topic {self.topic!r} requires a routing key")
            if entry.routing_key in self._coalesced:
                self._coalesced[entry.routing_key] = entry
                return OfferResult.COALESCED
            if len(self._coalesced) >= self.capacity:
                self.lagging_from_seq = entry.seq
                return OfferResult.OVERFLOW
            self._coalesced[entry.routing_key] = entry
            return OfferResult.ACCEPTED

        if len(self._entries) >= self.capacity:
            self.lagging_from_seq = entry.seq
            return OfferResult.OVERFLOW
        self._entries.append(entry)
        return OfferResult.ACCEPTED

    def pop(self) -> QueuedEntry | None:
        if self.policy.on_overflow == "coalesce":
            if not self._coalesced:
                return None
            _, entry = self._coalesced.popitem(last=False)
            return entry
        return self._entries.popleft() if self._entries else None

    def oldest_seq(self) -> int | None:
        """Lowest sequence still queued: the first entry a clear would discard."""
        entries = self._coalesced.values() if self.policy.on_overflow == "coalesce" else self._entries
        return min((entry.seq for entry in entries), default=None)

    def clear(self) -> None:
        self._entries.clear()
        self._coalesced.clear()

    def __len__(self) -> int:
        return len(self._coalesced) if self.policy.on_overflow == "coalesce" else len(self._entries)
