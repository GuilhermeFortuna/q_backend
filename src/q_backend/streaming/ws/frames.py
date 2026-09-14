"""Q-009 WebSocket frame encoding and subscription parsing."""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Mapping
from typing import Any

from q_contracts.stream import (
    CursorExpiredFrame,
    EpochChangedFrame,
    LaggingFrame,
    RejectedFrame,
    SubscribedFrame,
    SubscribeFrame,
)

# Redis stream IDs are `<ms>` or `<ms>-<seq>`. Anything else fails XREAD, which
# would otherwise surface as a stream outage rather than a client error.
_CURSOR = re.compile(r"\d+(?:-\d+)?")

_CONTROL_FRAMES: Mapping[str, type] = {
    "subscribed": SubscribedFrame,
    "rejected": RejectedFrame,
    "cursor_expired": CursorExpiredFrame,
    "lagging": LaggingFrame,
    "epoch_changed": EpochChangedFrame,
}


class FrameError(ValueError):
    """A client frame does not satisfy the stream control contract."""


def text_frame(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


def control_frame(frame_type: str, **fields: Any) -> dict[str, Any]:
    """Build a server control frame; optional fields left as None are omitted.

    Constructing the contract dataclass rejects fields the schema does not declare.
    """
    frame = {"type": frame_type, **{name: value for name, value in fields.items() if value is not None}}
    _CONTROL_FRAMES[frame_type](**frame)
    return frame


def binary_frame(header: Mapping[str, Any], payload: bytes) -> bytes:
    header_bytes = text_frame(header).encode("utf-8")
    return struct.pack("<I", len(header_bytes)) + header_bytes + payload


def parse_client_frame(raw: str) -> SubscribeFrame:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FrameError("invalid JSON client frame") from exc
    if not isinstance(obj, dict):
        raise FrameError("client frame must be an object")
    topics = obj.get("topics")
    if not isinstance(topics, list) or not topics or not all(isinstance(topic, str) for topic in topics):
        raise FrameError("topics must be a non-empty array of strings")
    cursors = obj.get("cursors")
    if cursors is not None:
        if not isinstance(cursors, dict):
            raise FrameError("cursors must be an object")
        for topic, cursor in cursors.items():
            if not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor):
                raise FrameError(f"cursor for {topic!r} must be a Redis stream id")
    return SubscribeFrame(topics=topics, cursors=cursors)
