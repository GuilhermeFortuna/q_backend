from collections.abc import Mapping
import base64
import json
from typing import Any

from q_backend.storage.db.outbox_models import OutboxEvent
from q_contracts.stream import StreamEnvelope
from q_contracts.topics import TOPICS


def encode_entry(envelope: StreamEnvelope, payload_bytes: bytes | None = None) -> dict[bytes, bytes]:
    """Redis entry fields: b"h" = JSON header (envelope minus payload), b"p" = raw payload bytes."""
    header: dict[str, Any] = {
        "topic": envelope.topic,
        "schema_major": envelope.schema_major,
        "seq": envelope.seq,
        "epoch": envelope.epoch,
        "producer_id": envelope.producer_id,
        "origin_ts": envelope.origin_ts,
        "payload_kind": envelope.payload_kind,
        "payload_schema": envelope.payload_schema,
    }
    if envelope.key is not None:
        header["key"] = envelope.key

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")

    if payload_bytes is not None:
        p_bytes = payload_bytes
    else:
        if envelope.payload_kind == "arrow_ipc":
            if isinstance(envelope.payload, str):
                p_bytes = base64.b64decode(envelope.payload.encode("ascii"))
            elif isinstance(envelope.payload, bytes):
                p_bytes = envelope.payload
            else:
                raise TypeError(f"Expected str or bytes for arrow_ipc payload, got {type(envelope.payload)}")
        elif envelope.payload_kind == "control":
            if isinstance(envelope.payload, (dict, list)):
                p_bytes = json.dumps(envelope.payload, separators=(",", ":")).encode("utf-8")
            elif isinstance(envelope.payload, str):
                p_bytes = envelope.payload.encode("utf-8")
            elif isinstance(envelope.payload, bytes):
                p_bytes = envelope.payload
            else:
                raise TypeError(f"Unsupported type for control payload: {type(envelope.payload)}")
        else:
            raise ValueError(f"Unknown payload_kind: {envelope.payload_kind}")

    return {
        b"h": header_bytes,
        b"p": p_bytes,
    }


def decode_entry(fields: Mapping[bytes | str, bytes | str]) -> tuple[StreamEnvelope, bytes]:
    h_val = fields.get(b"h") if b"h" in fields else fields.get("h")
    p_val = fields.get(b"p") if b"p" in fields else fields.get("p")
    if h_val is None or p_val is None:
        raise ValueError("Invalid entry fields: missing 'h' or 'p'")

    if isinstance(h_val, str):
        h_bytes = h_val.encode("utf-8")
    else:
        h_bytes = h_val

    if isinstance(p_val, str):
        p_bytes = p_val.encode("utf-8")
    else:
        p_bytes = p_val

    header = json.loads(h_bytes.decode("utf-8"))
    payload_kind = header.get("payload_kind")
    if payload_kind == "arrow_ipc":
        payload = base64.b64encode(p_bytes).decode("ascii")
    elif payload_kind == "control":
        payload = json.loads(p_bytes.decode("utf-8"))
    else:
        raise ValueError(f"Unknown payload_kind: {payload_kind}")

    envelope = StreamEnvelope(
        payload=payload,
        topic=header["topic"],
        schema_major=header["schema_major"],
        seq=header["seq"],
        epoch=header["epoch"],
        producer_id=header["producer_id"],
        origin_ts=header["origin_ts"],
        payload_kind=header["payload_kind"],
        payload_schema=header["payload_schema"],
        key=header.get("key"),
    )
    return envelope, p_bytes


def envelope_to_dict(envelope: StreamEnvelope) -> dict[str, Any]:
    """Convert a stream envelope to a JSON-serializable mapping."""
    data: dict[str, Any] = {
        "topic": envelope.topic,
        "schema_major": envelope.schema_major,
        "seq": envelope.seq,
        "epoch": envelope.epoch,
        "producer_id": envelope.producer_id,
        "origin_ts": envelope.origin_ts,
        "payload_kind": envelope.payload_kind,
        "payload_schema": envelope.payload_schema,
        "payload": envelope.payload,
    }
    if envelope.key is not None:
        data["key"] = envelope.key
    return data


def outbox_event_to_envelope(event: OutboxEvent) -> StreamEnvelope:
    """Rebuild a logical envelope from a durable outbox row."""
    origin_ts = event.origin_ts.isoformat()
    if event.origin_ts.tzinfo is not None:
        origin_ts = origin_ts.replace("+00:00", "Z")
    return StreamEnvelope(
        topic=event.topic,  # type: ignore[arg-type]
        schema_major=1,
        seq=event.seq,
        epoch=event.epoch,
        producer_id=event.producer_id,
        origin_ts=origin_ts,
        payload_kind=event.payload_kind,  # type: ignore[arg-type]
        payload_schema=event.payload_schema,
        payload=dict(event.payload),
        key=dict(event.routing_key) if event.routing_key is not None else None,
    )


def routing_key_string(topic: str, routing_key: Mapping[str, str]) -> str:
    """Values joined in the topic's coalesce_key order, e.g. "WINZ25|M1"."""
    if topic not in TOPICS:
        raise ValueError(f"Unknown topic: {topic!r}")
    policy = TOPICS[topic]
    if not policy.coalesce_key:
        return ""
    values = []
    for k in policy.coalesce_key:
        if k not in routing_key:
            raise KeyError(f"Missing routing key {k!r} for topic {topic!r}")
        values.append(str(routing_key[k]))
    return "|".join(values)
