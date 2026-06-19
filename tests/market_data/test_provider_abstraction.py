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
    monkeypatch.setattr(metatrader, "MT5_IMPORTABLE", False)
    monkeypatch.setattr(service.mt5_client, "is_available", lambda: False)

    runtime_config.set_data_source("local")
    assert service.active_provider() == "local"

    runtime_config.set_data_source("auto")
    assert service.active_provider() == "local"

    monkeypatch.setattr(service.mt5_client, "is_available", lambda: True)
    runtime_config.set_data_source("auto")
    assert service.active_provider() == "mt5"

    runtime_config.set_data_source("mt5")
    assert service.active_provider() == "mt5"

    monkeypatch.setattr(service.mt5_client, "is_available", lambda: False)
    runtime_config.set_data_source("mt5")
    with pytest.raises(ConnectionError, match="data_source is 'mt5'"):
        service._resolve_provider()


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
