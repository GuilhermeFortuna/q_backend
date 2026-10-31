"""Demo-account capability: mt5_live is opened only for a demo edge account (Q-145 step 6)."""

from __future__ import annotations

import pytest

from q_backend.api.services import execution as service
from q_backend.execution.edge_client import AccountResponse, EdgeUnavailable
from q_backend.storage.settings import Settings


def _gated(**overrides) -> Settings:
    """Settings with the normal live gates open: enabled, allowlisted and validated."""
    values = {
        "live_execution_enabled": True,
        "live_execution_account_allowlist": "900001",
        "live_execution_validated": True,
    }
    values.update(overrides)
    return Settings(**values)


def _account(trade_mode: str) -> AccountResponse:
    return AccountResponse(
        balance=100000.0,
        currency="BRL",
        equity=100000.0,
        login=900001,
        margin_free=100000.0,
        server="q-replay-demo",
        terminal_trade_allowed=True,
        trade_allowed=True,
        trade_mode=trade_mode,
    )


@pytest.fixture
def edge(monkeypatch):
    state = {"account": _account("demo"), "error": None, "calls": 0}

    class FakeEdge:
        def __init__(self, *args, **kwargs):
            pass

        def account(self):
            state["calls"] += 1
            if state["error"] is not None:
                raise state["error"]
            return state["account"]

    monkeypatch.setattr(service.EdgeClient, "from_settings", classmethod(lambda cls, s: FakeEdge()))
    return state


def test_demo_edge_account_is_available(edge) -> None:
    capability = service.demo_account_capability(_gated())
    assert capability.available is True and capability.login == 900001


def test_real_edge_account_is_never_reported_available(edge) -> None:
    edge["account"] = _account("real")
    capability = service.demo_account_capability(_gated())
    assert capability.available is False and "not a demo account" in capability.reason


def test_unreachable_edge_is_unavailable_with_a_reason(edge) -> None:
    edge["error"] = EdgeUnavailable("connection refused")
    capability = service.demo_account_capability(_gated())
    assert capability.available is False and "connection refused" in capability.reason


def test_closed_gates_keep_a_demo_account_unavailable(edge) -> None:
    capability = service.demo_account_capability(Settings(live_execution_enabled=False))
    assert capability.available is False and "disabled" in capability.reason


def test_disconnected_terminal_skips_the_edge_call(edge) -> None:
    capability = service.demo_account_capability(Settings(), mt5_connected=False)
    assert capability.available is False and edge["calls"] == 0


def test_demo_account_opens_the_live_lock_only_for_mt5_live(edge) -> None:
    settings = _gated(execution_live_capability_locked=True)
    assert service.effective_live_capability_locked(settings, broker_mode="mt5_live") is False
    assert service.effective_live_capability_locked(settings, broker_mode="paper") is True


def test_demo_account_with_closed_gates_keeps_the_lock(edge) -> None:
    settings = Settings(execution_live_capability_locked=True, live_execution_enabled=False)
    assert service.effective_live_capability_locked(settings, broker_mode="mt5_live") is True


def test_real_account_keeps_the_global_lock(edge) -> None:
    edge["account"] = _account("real")
    settings = _gated(execution_live_capability_locked=True)
    assert service.effective_live_capability_locked(settings, broker_mode="mt5_live") is True


def test_a_locked_setting_off_stays_open_regardless(edge) -> None:
    settings = _gated(execution_live_capability_locked=False)
    assert service.effective_live_capability_locked(settings, broker_mode="mt5_live") is False
