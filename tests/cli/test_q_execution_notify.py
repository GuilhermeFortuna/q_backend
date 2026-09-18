"""sd_notify sequence of the execution worker CLI."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from q_backend.cli import q_execution
from q_backend.execution.worker import RecoveryFailedClosed
from q_backend.observability.systemd import EX_FAILED_CLOSED
from tests.deploy.notify_socket import bound_notify_socket  # noqa: F401


def _run(monkeypatch, receiver, run_side_effect):
    monkeypatch.setenv("NOTIFY_SOCKET", str(receiver.path))
    with (
        patch.object(q_execution, "_market_data_session"),
        patch.object(q_execution, "_build_components", return_value=MagicMock()),
        patch.object(q_execution, "_build_worker") as build_worker,
    ):
        worker = MagicMock()
        worker.run.side_effect = run_side_effect(build_worker)
        build_worker.return_value = worker
        return q_execution.cmd_run(q_execution.build_parser().parse_args(["run"]))


def test_worker_is_wired_to_ready_and_watchdog(monkeypatch, bound_notify_socket):  # noqa: F811
    monkeypatch.setenv("NOTIFY_SOCKET", str(bound_notify_socket.path))
    components = MagicMock()
    with patch.object(q_execution, "create_session_factory"):
        worker = q_execution._build_worker(components)
    assert worker.edge_health is components.edge_client.health

    worker.on_ready()
    worker.on_poll()
    worker.on_poll()
    received = bound_notify_socket.drain()
    assert b"READY=1" in received
    assert received.index(b"READY=1") == max(i for i, m in enumerate(received) if m.startswith(b"STATUS=")) + 1
    assert received.count(b"WATCHDOG=1") == 2
    assert received[-1] == b"WATCHDOG=1"


def test_failed_closed_recovery_exits_79_with_status(monkeypatch, bound_notify_socket):  # noqa: F811
    def side_effect(_build_worker):
        return RecoveryFailedClosed("quote unavailable")

    code = _run(monkeypatch, bound_notify_socket, side_effect)
    assert code == EX_FAILED_CLOSED == 79
    received = bound_notify_socket.drain()
    assert received == [b"STATUS=failed closed: quote unavailable"]
    assert b"READY=1" not in received
