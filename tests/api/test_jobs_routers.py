"""Job-backed router assembly tests (WO59)."""

from __future__ import annotations

from q_backend.api.main import app

OPTIMIZATION_ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/v1/optimize"),
    ("GET", "/api/v1/optimize/{study_id}"),
    ("GET", "/api/v1/optimize/{study_id}/results"),
    ("POST", "/api/v1/optimize/{study_id}/cancel"),
    ("GET", "/api/v1/optimizations"),
    ("POST", "/api/v1/optimizations/bulk-delete"),
    ("DELETE", "/api/v1/optimizations/{study_id}"),
]

WALKFORWARD_ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/v1/walkforward"),
    ("GET", "/api/v1/walkforward/{run_id}"),
    ("GET", "/api/v1/walkforward/{run_id}/results"),
    ("POST", "/api/v1/walkforward/{run_id}/cancel"),
    ("GET", "/api/v1/walkforward/{run_id}/artifacts/equity"),
    ("GET", "/api/v1/walkforwards"),
    ("DELETE", "/api/v1/walkforwards/{run_id}"),
]

STRATEGY_SEARCH_ROUTES: list[tuple[str, str]] = [
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
]

OPTIMIZATION_OPENAPI_PATHS: list[str] = [
    "/api/v1/optimize",
    "/api/v1/optimize/{study_id}",
    "/api/v1/optimize/{study_id}/results",
    "/api/v1/optimize/{study_id}/cancel",
    "/api/v1/optimizations",
    "/api/v1/optimizations/bulk-delete",
    "/api/v1/optimizations/{study_id}",
]

WALKFORWARD_OPENAPI_PATHS: list[str] = [
    "/api/v1/walkforward",
    "/api/v1/walkforward/{run_id}",
    "/api/v1/walkforward/{run_id}/results",
    "/api/v1/walkforward/{run_id}/cancel",
    "/api/v1/walkforward/{run_id}/artifacts/equity",
    "/api/v1/walkforwards",
    "/api/v1/walkforwards/{run_id}",
]

STRATEGY_SEARCH_OPENAPI_PATHS: list[str] = [
    "/api/v1/strategy-search",
    "/api/v1/strategy-search/{run_id}",
    "/api/v1/strategy-search/{run_id}/results",
    "/api/v1/strategy-search/{run_id}/cancel",
    "/api/v1/strategy-searches",
    "/api/v1/strategy-searches/{run_id}",
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/artifacts/equity",
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/genome",
]

ALL_JOB_ROUTES = OPTIMIZATION_ROUTES + WALKFORWARD_ROUTES + STRATEGY_SEARCH_ROUTES
ALL_JOB_OPENAPI_PATHS = OPTIMIZATION_OPENAPI_PATHS + WALKFORWARD_OPENAPI_PATHS + STRATEGY_SEARCH_OPENAPI_PATHS


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


def test_optimization_routes_exist_with_original_paths():
    inventory = _route_inventory()
    for method, path in OPTIMIZATION_ROUTES:
        assert (method, path) in inventory


def test_walkforward_routes_exist_with_original_paths():
    inventory = _route_inventory()
    for method, path in WALKFORWARD_ROUTES:
        assert (method, path) in inventory


def test_strategy_search_routes_exist_with_original_paths():
    inventory = _route_inventory()
    for method, path in STRATEGY_SEARCH_ROUTES:
        assert (method, path) in inventory


def test_job_openapi_paths_present():
    paths = app.openapi()["paths"]
    for path in ALL_JOB_OPENAPI_PATHS:
        assert path in paths


def test_singular_plural_walkforward_routes_distinct():
    inventory = _route_inventory()
    assert ("POST", "/api/v1/walkforward") in inventory
    assert ("GET", "/api/v1/walkforwards") in inventory
    assert ("DELETE", "/api/v1/walkforwards/{run_id}") in inventory


def test_singular_plural_strategy_search_routes_distinct():
    inventory = _route_inventory()
    assert ("POST", "/api/v1/strategy-search") in inventory
    assert ("GET", "/api/v1/strategy-searches") in inventory
    assert ("DELETE", "/api/v1/strategy-searches/{run_id}") in inventory


def test_main_has_no_job_domain_routes():
    inventory = _route_inventory()
    for method, path in ALL_JOB_ROUTES:
        assert (method, path) in inventory

    import inspect
    import q_backend.api.main as main_module

    source = inspect.getsource(main_module)
    assert '@app.post("/api/v1/optimize"' not in source
    assert '@app.post("/api/v1/walkforward"' not in source
    assert '@app.post("/api/v1/strategy-search"' not in source
