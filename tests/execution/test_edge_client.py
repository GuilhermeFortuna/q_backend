"""EdgeClient contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from q_contracts.edge import ExecutionOrder
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts, EdgeUnavailable
from tests.execution.fake_edge import fake_edge_server, resetting_transport


def _client(base_url: str, *, transport: httpx.BaseTransport | None = None) -> EdgeClient:
    return EdgeClient(
        base_url,
        timeouts=EdgeTimeouts(connect_s=1.0, read_s=1.0, submit_read_s=1.0),
        transport=transport,
    )


def test_health_refuses_mismatched_schema_major():
    with fake_edge_server() as (base_url, state):
        state.schema_version = "2.0"
        client = _client(base_url)
        with pytest.raises(EdgeUnavailable, match="schema major"):
            client.health()


def test_submit_timeout_returns_indeterminate():
    with fake_edge_server() as (base_url, state):
        state.submit_delay_s = 2.0
        client = EdgeClient(
            base_url,
            timeouts=EdgeTimeouts(connect_s=1.0, read_s=1.0, submit_read_s=0.05),
        )
        order = ExecutionOrder(symbol="WIN$", volume=1.0, side="buy")
        outcome = client.submit(uuid4(), order)
        assert outcome["outcome"] == "indeterminate"
        assert len([r for r in state.requests if r[1] == "/v1/submit"]) == 1


def test_submit_transport_error_returns_indeterminate():
    with fake_edge_server() as (base_url, state):
        client = _client(base_url, transport=resetting_transport())
        order = ExecutionOrder(symbol="WIN$", volume=1.0, side="buy")
        outcome = client.submit(uuid4(), order)
        assert outcome["outcome"] == "indeterminate"
        assert state.requests == []


def test_account_raises_when_edge_unavailable():
    with fake_edge_server() as (base_url, state):
        state.unavailable = True
        client = _client(base_url)
        with pytest.raises(EdgeUnavailable):
            client.account()


def test_lookup_raises_when_edge_unavailable():
    with fake_edge_server() as (base_url, state):
        state.unavailable = True
        client = _client(base_url)
        now = datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc)
        with pytest.raises(EdgeUnavailable):
            client.lookup(uuid4(), now, now)


def test_submit_duplicate_intent_returns_indeterminate():
    intent_id = uuid4()
    with fake_edge_server() as (base_url, state):
        state.submitted_intents.add(str(intent_id))
        client = _client(base_url)
        order = ExecutionOrder(symbol="WIN$", volume=1.0, side="buy")
        outcome = client.submit(intent_id, order)
        assert outcome["outcome"] == "indeterminate"
        assert "already submitted" in outcome["reason"].lower()
