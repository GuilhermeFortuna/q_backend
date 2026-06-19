"""Tests for market-data provider abstraction (WO47)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from q_backend.api.main import (
    get_data_source_setting,
    get_system_health,
    update_data_source_setting,
)
from q_backend.api.main import DataSourceUpdateRequest
from q_backend.market_data.clients import metatrader
from q_backend.market_data.service import MarketDataService
from q_backend.storage import runtime_config


@pytest.fixture
def runtime_config_file(tmp_path, monkeypatch):
    path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(path))
    return path


def test_market_data_service_without_mt5(monkeypatch):
    monkeypatch.setattr(metatrader, "mt5", None)
    monkeypatch.setattr(metatrader, "MT5_IMPORTABLE", False)

    service = MarketDataService()
    assert service.mt5_available() is False
    assert service.active_provider() == "local"


@pytest.mark.skipif(not metatrader.MT5_IMPORTABLE, reason="MetaTrader5 not installed")
def test_mt5_timeframe_matches_constants():
    for name in metatrader.TIMEFRAME_NAMES:
        assert metatrader._mt5_timeframe(name) == getattr(
            metatrader.mt5, f"TIMEFRAME_{name}"
        )


def test_resolve_provider_respects_runtime_config(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(config_path))

    service = MarketDataService()

    # Platform cannot run MT5 (e.g. Linux): auto and local both resolve to local,
    # and an explicit 'mt5' selection errors.
    monkeypatch.setattr(service.mt5_client, "is_supported", lambda: False)

    runtime_config.set_data_source("local")
    assert service.active_provider() == "local"
    assert service._resolve_provider() is service._local_client

    runtime_config.set_data_source("auto")
    assert service.active_provider() == "local"
    assert service._resolve_provider() is service._local_client

    runtime_config.set_data_source("mt5")
    with pytest.raises(ConnectionError, match="data_source is 'mt5'"):
        service._resolve_provider()

    # Platform supports MT5 (e.g. Windows) but the terminal is momentarily
    # disconnected: auto and mt5 must still resolve to MT5 — the outage surfaces
    # from the MT5 call, never a silent fall back to local.
    monkeypatch.setattr(service.mt5_client, "is_supported", lambda: True)
    monkeypatch.setattr(service.mt5_client, "is_available", lambda: False)

    runtime_config.set_data_source("auto")
    assert service.active_provider() == "mt5"
    assert service._resolve_provider() is service.mt5_client

    runtime_config.set_data_source("mt5")
    assert service.active_provider() == "mt5"
    assert service._resolve_provider() is service.mt5_client


def test_data_source_endpoints_round_trip(runtime_config_file):
    body = get_data_source_setting()
    assert body["source"] in ("auto", "mt5", "local")
    assert "mt5_available" in body
    assert body["active_provider"] in ("mt5", "local")

    updated = update_data_source_setting(DataSourceUpdateRequest(source="local"))
    assert updated["source"] == "local"
    assert runtime_config_file.read_text(encoding="utf-8").strip().startswith("{")

    update_data_source_setting(DataSourceUpdateRequest(source="auto"))


def test_system_health_includes_provider_fields():
    with patch(
        "q_backend.api.main.storage_status",
        return_value={"postgres": {"status": "ok"}, "redis": {"status": "ok"}},
    ):
        body = get_system_health()

    assert "mt5_available" in body
    assert body["active_provider"] in ("mt5", "local")
    assert "market_data_root" in body
    assert "market_data_inventory_count" in body
