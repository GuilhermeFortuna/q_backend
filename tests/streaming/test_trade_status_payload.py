"""The trades.status control payload is registered with publisher validation."""

from __future__ import annotations

import fakeredis
import pytest

from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.publisher import EphemeralPublisher

SCHEMA = "schema/stream/payloads/trade-source-status.schema.json"


def _status(**overrides):
    payload = {
        "provider_id": "mt5",
        "symbol": "WINZ26",
        "source_generation": "gen-1",
        "volume_field": "volume_real",
        "volume_unit": "contracts",
        "covered_from": "2026-10-01T12:00:00Z",
        "covered_to": "2026-10-01T20:55:00Z",
        "coverage_state": "partial",
        "classification_coverage": "partial",
        "coverage_reason": "invalid_trade_records",
        "last_trade_watermark": {"epoch": "e1", "seq": 3},
    }
    payload.update(overrides)
    return payload


def test_status_publishes_on_its_own_sequence_and_round_trips():
    redis = fakeredis.FakeRedis()
    status = EphemeralPublisher(redis, "trades.status", producer_id="test")
    epoch, seq = status.publish(
        routing_key={"symbol": "WINZ26"}, payload_kind="control", payload_schema=SCHEMA, payload=_status()
    )

    assert seq == 1
    ((_id, fields),) = redis.xrange(stream_key("trades.status"))
    envelope, _raw = decode_entry(fields)
    assert envelope.topic == "trades.status" and envelope.epoch == epoch
    assert envelope.payload["coverage_state"] == "partial"


def test_status_rejects_payloads_that_do_not_match_the_contract():
    status = EphemeralPublisher(fakeredis.FakeRedis(), "trades.status", producer_id="test")
    with pytest.raises(ValueError):
        status.publish(
            routing_key={"symbol": "WINZ26"},
            payload_kind="control",
            payload_schema=SCHEMA,
            payload={k: v for k, v in _status().items() if k != "coverage_reason"},
        )
