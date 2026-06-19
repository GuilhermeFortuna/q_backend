"""Market router assembly and service unit tests (WO57)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.api.main import app
from q_backend.market_data import api_service as market_service
from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import set_data_source

MARKET_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/v1/market-data/symbol/{symbol}"),
    ("GET", "/api/v1/market-data/ohlcv"),
    ("GET", "/api/v1/market-data/ticks"),
    ("GET", "/api/v1/market/instruments"),
    ("GET", "/api/v1/market/symbols/search"),
    ("GET", "/api/v1/market/snapshot/{symbol}"),
    ("GET", "/api/v1/market/snapshots"),
    ("GET", "/api/v1/market/ticks/{symbol}"),
    ("GET", "/api/v1/market/instrument-info/{symbol}"),
    ("GET", "/api/v1/market/ohlcv/{symbol}"),
    ("GET", "/api/v1/market/ohlcv/{symbol}/available-range"),
]

MARKET_OPENAPI_PATHS: list[str] = [
    "/api/v1/market-data/symbol/{symbol}",
    "/api/v1/market-data/ohlcv",
    "/api/v1/market-data/ticks",
    "/api/v1/market/instruments",
    "/api/v1/market/symbols/search",
    "/api/v1/market/snapshot/{symbol}",
    "/api/v1/market/snapshots",
    "/api/v1/market/ticks/{symbol}",
    "/api/v1/market/instrument-info/{symbol}",
    "/api/v1/market/ohlcv/{symbol}",
    "/api/v1/market/ohlcv/{symbol}/available-range",
]


def _route_inventory() -> set[tuple[str, str]]:
    inventory: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None:
            continue
        for method in methods:
            if method == "HEAD":
                continue
            inventory.add((method, path))
    return inventory


def test_market_routes_exist_with_original_paths():
    inventory = _route_inventory()
    for method, path in MARKET_ROUTES:
        assert (method, path) in inventory


def test_market_openapi_paths_present():
    paths = app.openapi()["paths"]
    for path in MARKET_OPENAPI_PATHS:
        assert path in paths


def test_merge_instruments_prefers_first_group_on_symbol_collision():
    default = [{"symbol": "PETR4", "name": "Default", "exchange": "BOVESPA", "assetClass": "equity"}]
    stored = [{"symbol": "PETR4", "name": "Stored", "exchange": "LOCAL", "assetClass": "equity"}]

    merged = market_service.merge_instruments_by_symbol(default, stored)

    assert len(merged) == 1
    assert merged[0]["name"] == "Default"


def test_search_instrument_sources_returns_503_when_mt5_required_but_offline():
    service = MarketDataService()
    set_data_source("mt5")

    try:
        with patch.object(service, "mt5_available", return_value=False):
            with pytest.raises(Exception) as exc_info:
                market_service.search_instrument_sources(service, "petr")

        assert exc_info.value.status_code == 503
    finally:
        set_data_source("auto")


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


def test_stored_instruments_include_local_exchange(market_root):
    local_store.write_ohlcv(
        "BBAS3",
        "D1",
        [
            OHLCV(
                time=datetime(2024, 3, 1),
                open=1.0,
                high=2.0,
                low=0.5,
                close=1.5,
                tick_volume=100,
            )
        ],
    )

    instruments = market_service.stored_instruments()

    assert any(item["symbol"] == "BBAS3" and item["exchange"] == "LOCAL" for item in instruments)
