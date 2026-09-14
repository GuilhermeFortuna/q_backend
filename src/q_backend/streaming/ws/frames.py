"""Q-009 WebSocket frame encoding and subscription parsing."""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from typing import Any

from q_contracts.stream import SubscribeFrame


class FrameError(ValueError):
    """A client frame does not satisfy the stream control contract."""


def text_frame(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


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
    if cursors is not None and not isinstance(cursors, dict):
        raise FrameError("cursors must be an object")
    return SubscribeFrame(topics=topics, cursors=cursors)
