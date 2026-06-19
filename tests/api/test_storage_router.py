"""Storage router assembly tests (WO60)."""

from __future__ import annotations

from q_backend.api.main import app

STORAGE_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/v1/storage/inventory"),
    ("POST", "/api/v1/storage/ingest"),
    ("GET", "/api/v1/storage/ingest/{job_id}"),
    ("DELETE", "/api/v1/storage/{symbol}/{timeframe}"),
]

STORAGE_OPENAPI_PATHS: list[str] = [
    "/api/v1/storage/inventory",
    "/api/v1/storage/ingest",
    "/api/v1/storage/ingest/{job_id}",
    "/api/v1/storage/{symbol}/{timeframe}",
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


def test_storage_routes_exist_with_original_paths():
    inventory = _route_inventory()
    for method, path in STORAGE_ROUTES:
        assert (method, path) in inventory


def test_storage_openapi_paths_present():
    paths = app.openapi()["paths"]
    for path in STORAGE_OPENAPI_PATHS:
        assert path in paths
