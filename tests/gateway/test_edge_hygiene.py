from __future__ import annotations

import ast
from pathlib import Path

import yaml
import pytest

from tests.gateway.conftest import EDGE_PATH, REPO_ROOT, load_module
from tests.gateway.fake_metatrader5 import make_fake_mt5

CONTRACT_ENDPOINTS = {
    "/v1/health",
    "/v1/quote",
    "/v1/check",
    "/v1/submit",
    "/v1/lookup",
    "/v1/positions",
    "/v1/deals",
    "/v1/account",
}

DERIVATION_VECTORS = [
    ("00000000-0000-0000-0000-000000000000", 0, "q:000000000000000000000000"),
    ("ffffffff-ffff-ffff-ffff-ffffffffffff", 2147483647, "q:ffffffffffffffffffffffff"),
    ("00000000-0000-0000-0000-000040000000", 1073741824, "q:000000000000000000000000"),
    ("12345678-1234-5678-1234-567812345678", 305419896, "q:123456781234567812345678"),
    ("a1b2c3d4-e5f6-7890-abcd-ef1234567890", 878082192, "q:a1b2c3d4e5f67890abcdef12"),
]


def _edge_source() -> str:
    return EDGE_PATH.read_text()


def _import_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_edge_imports_are_stdlib_mt5_or_numpy_only():
    allowed = {
        "MetaTrader5",
        "numpy",
        "__future__",
        "argparse",
        "json",
        "logging",
        "os",
        "signal",
        "sys",
        "threading",
        "time",
        "datetime",
        "http",
        "typing",
        "urllib",
        "uuid",
    }
    imports = _import_names(_edge_source())
    assert imports <= allowed


def test_edge_routes_match_contract_endpoints(edge):
    route_paths = {path for (_method, path) in edge._EdgeHandler._ROUTES}
    assert route_paths == CONTRACT_ENDPOINTS


def test_intent_derivation_vectors(edge):
    for intent_id, magic, comment in DERIVATION_VECTORS:
        assert edge.intent_magic(intent_id) == magic
        assert edge.intent_comment(intent_id) == comment


def test_non_loopback_host_exits_with_config_status(edge):
    assert edge.main(["--host", "0.0.0.0", "--port", "0"]) == 78


def test_contract_vectors_match_yaml(edge):
    contract_path = REPO_ROOT.parent.parent / "q_contracts/schema/edge/execution.yaml"
    if not contract_path.exists():
        pytest.skip("q_contracts checkout not available beside q_backend")
    data = yaml.safe_load(contract_path.read_text())
    for row in data["safety_invariants"]["intent_derivation"]["vectors"]:
        assert edge.intent_magic(row["intent_id"]) == row["magic"]
        assert edge.intent_comment(row["intent_id"]) == row["comment"]


def test_route_table_is_read_from_source_not_runtime_import():
    source = _edge_source()
    assert "_ROUTES = {" in source
    module = load_module(EDGE_PATH, "edge_hygiene_reload", make_fake_mt5())
    assert set(module._EdgeHandler._ROUTES) == {
        ("GET", "/v1/health"),
        ("GET", "/v1/quote"),
        ("GET", "/v1/account"),
        ("GET", "/v1/positions"),
        ("POST", "/v1/check"),
        ("POST", "/v1/submit"),
        ("POST", "/v1/lookup"),
        ("POST", "/v1/deals"),
    }
