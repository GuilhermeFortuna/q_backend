"""API assembly tests for the decomposed router layout (WO56+)."""

from __future__ import annotations

import inspect

from q_backend.api.dependencies import market_data_service
from q_backend.api.lifespan import lifespan
from q_backend.api.main import app
from q_backend.market_data.service import MarketDataService

MIGRATED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/api/v1/system/health"),
    ("GET", "/api/v1/system/data-source"),
    ("PUT", "/api/v1/system/data-source"),
    ("GET", "/api/v1/strategies"),
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

MIGRATED_OPENAPI_PATHS: list[str] = [
    "/",
    "/api/v1/system/health",
    "/api/v1/system/data-source",
    "/api/v1/strategies",
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


def test_migrated_routes_exist_with_same_methods():
    inventory = _route_inventory()
    for method, path in MIGRATED_ROUTES:
        assert (method, path) in inventory


def test_migrated_openapi_paths_present():
    paths = app.openapi()["paths"]
    for path in MIGRATED_OPENAPI_PATHS:
        assert path in paths


def test_market_data_service_singleton_used_by_lifespan():
    source = inspect.getsource(lifespan)
    assert "market_data_service" in source
    assert market_data_service is market_data_service
    assert isinstance(market_data_service, MarketDataService)


def test_api_dependencies_has_single_market_data_service_instance():
    import q_backend.api.dependencies as dependencies

    assert dependencies.market_data_service is market_data_service
    assert isinstance(dependencies.get_market_data_service(), MarketDataService)
