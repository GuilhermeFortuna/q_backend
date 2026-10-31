"""Runtime of one replay simulator: admin listener always, market and broker listeners per run.

Loopback binding is enforced: the three listeners accept only 127.0.0.1 addresses. The market and
broker listeners start once a run is prepared, because they are built from that run's market.
Ports default to the spec's values (18912 market, 18913 broker edge, 18914 administrative) and
collisions are refused before anything starts.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from q_backend.replay.admin import create_admin_app
from q_backend.replay.edge import create_edge_app
from q_backend.replay.gateway import ServerThread, create_gateway_app
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import Driver, ReplaySimulator

LOOPBACK = "127.0.0.1"
DEFAULT_MARKET_PORT = 18912
DEFAULT_EDGE_PORT = 18913
DEFAULT_ADMIN_PORT = 18914


class PortInUseError(RuntimeError):
    """A configured listener port is already bound; the simulator refuses to start."""


def _ensure_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((LOOPBACK, port))
        except OSError as exc:
            raise PortInUseError(f"port {port} on {LOOPBACK} is not available") from exc


class ReplayServer:
    def __init__(
        self,
        *,
        store_path: Path,
        dataset_root: Path,
        market_port: int = DEFAULT_MARKET_PORT,
        edge_port: int = DEFAULT_EDGE_PORT,
        admin_port: int = DEFAULT_ADMIN_PORT,
        admin_token: str | None = None,
        driver_interval_s: float = 0.005,
    ) -> None:
        ports = [market_port, edge_port, admin_port]
        if len(set(ports)) != len(ports):
            raise PortInUseError("market, broker and administrative ports must differ")
        self._ports = {"market": market_port, "edge": edge_port, "admin": admin_port}
        self._store = ReplayRunStore(store_path)
        self._simulator = ReplaySimulator(self._store, dataset_root=dataset_root)
        self._admin_token = admin_token
        self._driver = Driver(self._simulator, interval_s=driver_interval_s)
        self._servers: dict[str, ServerThread] = {}
        self._run_lock = threading.Lock()
        self._run_started = False

    @property
    def simulator(self) -> ReplaySimulator:
        return self._simulator

    def start(self) -> None:
        for port in self._ports.values():
            _ensure_free(port)
        admin = create_admin_app(self._simulator, token=self._admin_token, on_created=self._start_run_listeners)
        self._servers["admin"] = ServerThread(admin, host=LOOPBACK, port=self._ports["admin"])
        self._servers["admin"].start()
        if self._simulator.configured:
            self._start_run_listeners()
        self._driver.start()

    def _start_run_listeners(self) -> None:
        with self._run_lock:
            if self._run_started or not self._simulator.configured:
                return
            market = create_gateway_app(market=self._simulator.market, ingress=self._simulator.gateway_ingress)
            edge = create_edge_app(self._simulator)
            self._servers["market"] = ServerThread(market, host=LOOPBACK, port=self._ports["market"])
            self._servers["edge"] = ServerThread(edge, host=LOOPBACK, port=self._ports["edge"])
            self._servers["market"].start()
            self._servers["edge"].start()
            self._run_started = True

    def stop(self) -> None:
        self._driver.stop()
        for server in self._servers.values():
            server.stop()
        self._store.close()
