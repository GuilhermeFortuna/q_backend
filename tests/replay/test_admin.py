"""Administrative API, runtime listeners and the q-replay controller, over loopback HTTP."""

from __future__ import annotations

import socket
import uuid

import httpx
import pytest

from q_backend.cli.q_replay import main as q_replay
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.replay.admin import create_admin_app
from q_backend.replay.gateway import ServerThread
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.server import PortInUseError, ReplayServer
from q_backend.replay.service import ReplaySimulator

from .support import QUOTE, config_for, ms, spec, write_dataset


def _config_dict(**overrides) -> dict:
    config = config_for(end_seconds=600, latency={"broker_response_ms": 100}).model_dump(mode="json")
    config.update(overrides)
    return config


@pytest.fixture
def dataset(tmp_path):
    ticks = [(ms(t / 10), 99.0, 100.0, 0.0, 0.0, QUOTE) for t in range(0, 300)]
    write_dataset(tmp_path, {"WIN$N": ticks}, {"WIN$N": spec()})
    return tmp_path


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_admin_run_lifecycle_and_refusals(dataset) -> None:
    store = ReplayRunStore(dataset / "admin.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=dataset)
    server = ServerThread(create_admin_app(simulator))
    url = server.start()
    try:
        with httpx.Client(base_url=url) as http:
            assert http.get("/replay/v1/runs/current").status_code == 404
            created = http.post("/replay/v1/runs", json=_config_dict())
            assert created.status_code == 201
            run = created.json()
            assert run["state"] == "prepared" and run["revision"] == 0
            assert run["dataset_checksums"][0].startswith("sha256:")

            conflict = http.post("/replay/v1/runs", json=_config_dict())
            assert conflict.status_code == 409 and conflict.json()["code"] == "run_exists"

            stale = http.post(
                "/replay/v1/runs/current/commands",
                json={"command_id": str(uuid.uuid4()), "expected_revision": 7, "action": "resume"},
            )
            assert stale.json()["accepted"] is False and stale.json()["reason"] == "stale_revision"

            resumed = http.post(
                "/replay/v1/runs/current/commands",
                json={"command_id": str(uuid.uuid4()), "expected_revision": 0, "action": "resume"},
            )
            assert resumed.json()["accepted"] is True and resumed.json()["state"] == "running"

            bad = http.post(
                "/replay/v1/runs/current/commands",
                json={"command_id": str(uuid.uuid4()), "expected_revision": 1, "action": "speed"},
            )
            assert bad.status_code == 422

            entries = http.get("/replay/v1/runs/current/journal").json()
            assert any("resume" in entry["payload"].get("action", "") for entry in entries)
    finally:
        server.stop()
        store.close()


def test_admin_token_is_enforced(dataset) -> None:
    store = ReplayRunStore(dataset / "auth.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=dataset)
    server = ServerThread(create_admin_app(simulator, token="s3cret"))
    url = server.start()
    try:
        with httpx.Client(base_url=url) as http:
            assert http.get("/replay/v1/runs/current").status_code == 401
            assert http.get("/replay/v1/runs/current", headers={"X-Replay-Token": "nope"}).status_code == 401
            assert http.get("/replay/v1/runs/current", headers={"X-Replay-Token": "s3cret"}).status_code == 404
    finally:
        server.stop()
        store.close()


def test_runtime_starts_market_and_broker_listeners_after_create(dataset) -> None:
    admin_port, market_port, edge_port = _free_port(), _free_port(), _free_port()
    server = ReplayServer(
        store_path=dataset / "runtime.sqlite3",
        dataset_root=dataset,
        market_port=market_port,
        edge_port=edge_port,
        admin_port=admin_port,
    )
    server.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{admin_port}") as http:
            assert http.post("/replay/v1/runs", json=_config_dict()).status_code == 201
        market = RemoteMt5Client(base_url=f"http://127.0.0.1:{market_port}", timeout=5.0)
        assert market.is_available() is True
        edge = EdgeClient(
            f"http://127.0.0.1:{edge_port}", timeouts=EdgeTimeouts(connect_s=2.0, read_s=5.0, submit_read_s=5.0)
        )
        assert edge.health().schema_version == "1.2"
    finally:
        server.stop()


def test_runtime_refuses_a_busy_port(dataset) -> None:
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        port = busy.getsockname()[1]
        server = ReplayServer(
            store_path=dataset / "busy.sqlite3",
            dataset_root=dataset,
            market_port=_free_port(),
            edge_port=_free_port(),
            admin_port=port,
        )
        with pytest.raises(PortInUseError):
            server.start()
    finally:
        busy.close()


def test_q_replay_prepare_verifies_the_dataset_offline(dataset, tmp_path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(_config_dict()), encoding="utf-8")
    assert q_replay(["prepare", "--config", str(config_path), "--root", str(dataset)]) == 0
    assert "sha256:" in capsys.readouterr().out
    (dataset / "WINN.npz").write_bytes(b"changed")  # the tick file name derived from WIN$N
    assert q_replay(["prepare", "--config", str(config_path), "--root", str(dataset)]) == 2
