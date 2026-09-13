import base64
import json
import pytest

from q_contracts.stream import StreamEnvelope
from q_backend.streaming.codec import decode_entry, encode_entry, routing_key_string
from q_backend.streaming.keys import (
    STREAM_EPOCH_KEY,
    latest_key,
    seq_key,
    stream_key,
    topic_epoch_key,
)


def test_keys():
    assert STREAM_EPOCH_KEY == "q:stream:epoch"
    assert stream_key("quotes") == "q:stream:quotes"
    assert seq_key("quotes") == "q:seq:quotes"
    assert topic_epoch_key("quotes") == "q:epoch:quotes"
    assert latest_key("quotes") == "q:latest:quotes"


def test_encode_decode_quotes_arrow_payload():
    raw_bytes = b"\x00\x01\x02\x03" * 16  # 64 bytes
    payload_b64 = base64.b64encode(raw_bytes).decode("ascii")

    envelope = StreamEnvelope(
        epoch="20260912-00000001",
        origin_ts="2026-09-12T10:00:00.000000Z",
        payload=payload_b64,
        payload_kind="arrow_ipc",
        payload_schema="schema/api/arrow/ticks.schema.json",
        producer_id="test-producer-1",
        schema_major=1,
        seq=42,
        topic="quotes",
        key={"symbol": "WINZ25"},
    )

    fields = encode_entry(envelope, payload_bytes=raw_bytes)
    assert isinstance(fields, dict)
    assert b"p" in fields
    assert b"h" in fields
    assert fields[b"p"] == raw_bytes

    header = json.loads(fields[b"h"].decode("utf-8"))
    assert "payload" not in header
    assert header["topic"] == "quotes"
    assert header["seq"] == 42
    assert header["epoch"] == "20260912-00000001"
    assert header["producer_id"] == "test-producer-1"
    assert header["payload_kind"] == "arrow_ipc"
    assert header["key"] == {"symbol": "WINZ25"}

    decoded_env, decoded_payload = decode_entry(fields)
    assert decoded_payload == raw_bytes
    assert decoded_env == envelope


def test_encode_decode_control_payload():
    payload_dict = {
        "job_id": "job-123",
        "kind": "backtest",
        "status": "running",
        "progress": 0.5,
        "message": "working",
    }
    envelope = StreamEnvelope(
        epoch="20260912-00000001",
        origin_ts="2026-09-12T10:00:00.000000Z",
        payload=payload_dict,
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        producer_id="test-producer-2",
        schema_major=1,
        seq=1,
        topic="jobs.progress",
        key={"kind": "backtest", "job_id": "job-123"},
    )

    fields = encode_entry(envelope)
    assert fields[b"p"] == json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    header = json.loads(fields[b"h"].decode("utf-8"))
    assert "payload" not in header

    decoded_env, decoded_payload = decode_entry(fields)
    assert decoded_env == envelope
    assert json.loads(decoded_payload.decode("utf-8")) == payload_dict


def test_routing_key_string():
    assert routing_key_string("bars.forming", {"timeframe": "M1", "symbol": "WINZ25"}) == "WINZ25|M1"
    assert routing_key_string("quotes", {"symbol": "WINZ25"}) == "WINZ25"
    assert routing_key_string("jobs.progress", {"kind": "backtest", "job_id": "job-99"}) == "backtest|job-99"
    assert routing_key_string("bars.completed", {}) == ""

    with pytest.raises(ValueError, match="Unknown topic"):
        routing_key_string("invalid.topic", {})

    with pytest.raises(KeyError, match="Missing routing key"):
        routing_key_string("bars.forming", {"symbol": "WINZ25"})
