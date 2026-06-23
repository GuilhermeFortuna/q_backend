"""API assembly tests for the decomposed router layout (WO56+)."""

from __future__ import annotations

import inspect

from q_backend.api.dependencies import market_data_service
from q_backend.api.lifespan import lifespan
from q_backend.api.main import app
from q_backend.market_data.service import MarketDataService

FULL_ROUTE_INVENTORY: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/api/v1/system/health"),
    ("GET", "/api/v1/system/data-source"),
    ("PUT", "/api/v1/system/data-source"),
    ("GET", "/api/v1/strategies"),
    ("GET", "/api/v1/strategy-builder/capabilities"),
    ("POST", "/api/v1/strategy-builder/validate"),
    ("POST", "/api/v1/strategy-builder/compile"),
    ("POST", "/api/v1/strategy-builder/interpret"),
    ("GET", "/api/v1/strategies/custom"),
    ("POST", "/api/v1/strategies/custom"),
    ("DELETE", "/api/v1/strategies/custom/{name}"),
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
    ("POST", "/api/v1/backtest"),
    ("GET", "/api/v1/backtest/{run_id}"),
    ("GET", "/api/v1/backtest/{run_id}/result"),
    ("GET", "/api/v1/backtests"),
    ("POST", "/api/v1/backtests/bulk-delete"),
    ("GET", "/api/v1/backtests/{run_id}"),
    ("PATCH", "/api/v1/backtests/{run_id}"),
    ("DELETE", "/api/v1/backtests/{run_id}"),
    ("GET", "/api/v1/backtests/{run_id}/artifacts/equity"),
    ("GET", "/api/v1/backtests/{run_id}/artifacts/trades"),
    ("POST", "/api/v1/optimize"),
    ("GET", "/api/v1/optimize/{study_id}"),
    ("GET", "/api/v1/optimize/{study_id}/results"),
    ("POST", "/api/v1/optimize/{study_id}/cancel"),
    ("GET", "/api/v1/optimizations"),
    ("POST", "/api/v1/optimizations/bulk-delete"),
    ("DELETE", "/api/v1/optimizations/{study_id}"),
    ("POST", "/api/v1/walkforward"),
    ("GET", "/api/v1/walkforward/{run_id}"),
    ("GET", "/api/v1/walkforward/{run_id}/results"),
    ("POST", "/api/v1/walkforward/{run_id}/cancel"),
    ("GET", "/api/v1/walkforward/{run_id}/artifacts/equity"),
    ("GET", "/api/v1/walkforwards"),
    ("DELETE", "/api/v1/walkforwards/{run_id}"),
    ("POST", "/api/v1/strategy-search"),
    ("GET", "/api/v1/strategy-search/{run_id}"),
    ("GET", "/api/v1/strategy-search/{run_id}/results"),
    ("POST", "/api/v1/strategy-search/{run_id}/cancel"),
    ("GET", "/api/v1/strategy-searches"),
    ("DELETE", "/api/v1/strategy-searches/{run_id}"),
    (
        "GET",
        "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/artifacts/equity",
    ),
    (
        "GET",
        "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/genome",
    ),
    ("GET", "/api/v1/storage/inventory"),
    ("POST", "/api/v1/storage/ingest"),
    ("GET", "/api/v1/storage/ingest/{job_id}"),
    ("DELETE", "/api/v1/storage/{symbol}/{timeframe}"),
    ("GET", "/api/v1/news"),
    ("GET", "/api/v1/news/{article_id}"),
]

FULL_OPENAPI_PATHS: list[str] = [
    "/",
    "/api/v1/system/health",
    "/api/v1/system/data-source",
    "/api/v1/strategies",
    "/api/v1/strategy-builder/capabilities",
    "/api/v1/strategy-builder/validate",
    "/api/v1/strategy-builder/compile",
    "/api/v1/strategy-builder/interpret",
    "/api/v1/strategies/custom",
    "/api/v1/strategies/custom/{name}",
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
    "/api/v1/backtest",
    "/api/v1/backtest/{run_id}",
    "/api/v1/backtest/{run_id}/result",
    "/api/v1/backtests",
    "/api/v1/backtests/bulk-delete",
    "/api/v1/backtests/{run_id}",
    "/api/v1/backtests/{run_id}/artifacts/equity",
    "/api/v1/backtests/{run_id}/artifacts/trades",
    "/api/v1/optimize",
    "/api/v1/optimize/{study_id}",
    "/api/v1/optimize/{study_id}/results",
    "/api/v1/optimize/{study_id}/cancel",
    "/api/v1/optimizations",
    "/api/v1/optimizations/bulk-delete",
    "/api/v1/optimizations/{study_id}",
    "/api/v1/walkforward",
    "/api/v1/walkforward/{run_id}",
    "/api/v1/walkforward/{run_id}/results",
    "/api/v1/walkforward/{run_id}/cancel",
    "/api/v1/walkforward/{run_id}/artifacts/equity",
    "/api/v1/walkforwards",
    "/api/v1/walkforwards/{run_id}",
    "/api/v1/strategy-search",
    "/api/v1/strategy-search/{run_id}",
    "/api/v1/strategy-search/{run_id}/results",
    "/api/v1/strategy-search/{run_id}/cancel",
    "/api/v1/strategy-searches",
    "/api/v1/strategy-searches/{run_id}",
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/artifacts/equity",
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/genome",
    "/api/v1/storage/inventory",
    "/api/v1/storage/ingest",
    "/api/v1/storage/ingest/{job_id}",
    "/api/v1/storage/{symbol}/{timeframe}",
    "/api/v1/news",
    "/api/v1/news/{article_id}",
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


def test_full_route_inventory_present_across_all_routers():
    inventory = _route_inventory()
    missing = [
        (method, path)
        for method, path in FULL_ROUTE_INVENTORY
        if (method, path) not in inventory
    ]
    assert not missing, f"Missing routes: {missing}"


def test_full_openapi_paths_present():
    paths = app.openapi()["paths"]
    missing = [path for path in FULL_OPENAPI_PATHS if path not in paths]
    assert not missing, f"Missing OpenAPI paths: {missing}"


def test_main_is_assembly_only():
    import q_backend.api.main as main_module

    source = inspect.getsource(main_module)
    assert "@app.get(" not in source
    assert "@app.post(" not in source
    assert "@app.put(" not in source
    assert "@app.patch(" not in source
    assert "@app.delete(" not in source
    assert "urllib.request" not in source
    assert "xml.etree.ElementTree" not in source


def test_market_data_service_singleton_used_by_lifespan():
    from q_backend.api.dependencies import get_market_data_service

    source = inspect.getsource(lifespan)
    assert "market_data_service" in source
    # The DI provider hands out the same singleton the lifespan initializes.
    assert get_market_data_service() is market_data_service
    assert isinstance(market_data_service, MarketDataService)


def test_api_dependencies_has_single_market_data_service_instance():
    import q_backend.api.dependencies as dependencies

    assert dependencies.market_data_service is market_data_service
    assert isinstance(dependencies.get_market_data_service(), MarketDataService)
