from __future__ import annotations

import importlib.util
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from tests.gateway.fake_metatrader5 import make_fake_mt5

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PATH = REPO_ROOT / "gateway" / "mt5_gateway.py"
EDGE_PATH = REPO_ROOT / "gateway" / "mt5_execution_edge.py"
SCHEMA_ROOT = REPO_ROOT / "contracts" / "schema" / "edge"


def load_module(path: Path, module_name: str, fake_mt5):
    saved = sys.modules.get("MetaTrader5")
    sys.modules["MetaTrader5"] = fake_mt5
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is not None:
            sys.modules["MetaTrader5"] = saved
        else:
            sys.modules.pop("MetaTrader5", None)


@pytest.fixture
def fake_mt5():
    return make_fake_mt5()


@pytest.fixture
def gateway(fake_mt5):
    return load_module(GATEWAY_PATH, "mt5_gateway_under_test", fake_mt5)


@pytest.fixture
def edge(fake_mt5):
    return load_module(EDGE_PATH, "mt5_execution_edge_under_test", fake_mt5)


@contextmanager
def running_gateway_server(gateway_module, token: str | None = None):
    server = gateway_module.build_server("127.0.0.1", 0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def running_edge_server(edge_module):
    server = edge_module.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http_get(base_url: str, path: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None):
    url = base_url + path
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(url, headers=headers or {}, method="GET")
    try:
        with urlopen(request, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def http_post(base_url: str, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        base_url + path,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()
