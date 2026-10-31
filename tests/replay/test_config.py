"""Run configuration validation: unsafe values fail before a run starts."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pydantic
import pytest

from q_backend.replay.config import ReplayRunConfig, from_market_us, to_market_us

VALID = {
    "manifest_path": "manifests/wdo.json",
    "symbols": ["WDO$N"],
    "start_utc": "2026-10-01T12:00:00Z",
    "end_utc": "2026-10-01T15:00:00Z",
    "initial_balance": "100000.00",
    "account": {"login": 900001, "currency": "BRL"},
    "latency": {
        "order_transport_ms": 50,
        "broker_accept_ms": 50,
        "broker_execution_ms": 150,
        "broker_response_ms": 100,
    },
    "rate": 1,
    "limits": {"max_queued_events": 1000, "max_journal_bytes": 1_000_000},
}


def _config(**overrides) -> dict:
    raw = copy.deepcopy(VALID)
    raw.update(overrides)
    return raw


def test_valid_config_parses_and_exposes_market_bounds() -> None:
    config = ReplayRunConfig.model_validate(VALID)
    assert config.latency.market_data_ms == 0  # unspecified latency defaults to zero
    assert config.start_us == to_market_us(datetime(2026, 10, 1, 12, tzinfo=UTC))
    assert config.end_us - config.start_us == 3 * 3600 * 1_000_000


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"start_utc": "2026-10-01T12:00:00"}, id="naive-start"),
        pytest.param({"end_utc": "2026-10-01T12:00:00Z"}, id="end-not-after-start"),
        pytest.param({"symbols": ["WDO$N", "WDO$N"]}, id="duplicate-symbols"),
        pytest.param({"symbols": []}, id="no-symbols"),
        pytest.param({"warmup_start_utc": "2026-10-01T12:00:00Z"}, id="warmup-not-before-start"),
        pytest.param({"initial_balance": "0"}, id="zero-balance"),
        pytest.param({"initial_balance": "1e5"}, id="scientific-balance"),
        pytest.param({"rate": 0}, id="zero-rate"),
        pytest.param({"rate": 10.01}, id="rate-over-ceiling"),
        pytest.param({"rate": float("nan")}, id="nan-rate"),
        pytest.param({"latency": {"order_transport_ms": -5}}, id="negative-latency"),
        pytest.param({"latency": {"broker_execution_ms": 1.5}}, id="fractional-latency"),
        pytest.param({"latency": {"unknown_ms": 1}}, id="unknown-latency-field"),
        pytest.param({"account": {"login": 0, "currency": "BRL"}}, id="zero-login"),
        pytest.param({"account": {"login": 1, "currency": "brl"}}, id="lowercase-currency"),
        pytest.param({"limits": {"max_queued_events": 0, "max_journal_bytes": 1}}, id="zero-queue-limit"),
        pytest.param({"extra": True}, id="unknown-top-level-field"),
    ],
)
def test_unsafe_config_is_rejected(overrides: dict) -> None:
    with pytest.raises(pydantic.ValidationError):
        ReplayRunConfig.model_validate(_config(**overrides))


def test_config_hash_changes_with_latency_and_is_key_order_independent() -> None:
    base = ReplayRunConfig.model_validate(VALID)
    slower = ReplayRunConfig.model_validate(_config(latency={"broker_response_ms": 900}))
    assert base.config_hash() != slower.config_hash()
    assert base.config_hash().startswith("sha256:")


def test_market_microseconds_round_trip() -> None:
    instant = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)
    assert from_market_us(to_market_us(instant)) == instant
