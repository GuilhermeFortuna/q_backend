"""Replay simulator composition: one run, one lock, one market timeline driving every listener.

The service owns the store, controller, broker and market. Its public methods are called from HTTP
handlers and the driver thread. Every state change happens under ``self._lock`` inside one store
transaction; HTTP waits for visibility happen outside the lock, so a submit that is waiting for
its response time never blocks the market or other requests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from q_backend.replay.broker import SimulatedBroker
from q_backend.replay.config import ReplayRunConfig
from q_backend.replay.controller import ReplayController, RunStatus
from q_backend.replay.dataset import DatasetError, DatasetManifest, load_manifest, verify_manifest
from q_backend.replay.gateway import Ingress
from q_backend.replay.journal import ReplayRunStore, RunRecord
from q_backend.replay.market import ReplayMarket, load_market
from q_backend.replay.timeline import ReplayTimeline

_WAIT_POLL_SECONDS = 0.005


class SubmitTimeoutError(TimeoutError):
    """The response could not be delivered within the real-time wait; the intent stays unresolved."""


class ReplaySimulator:
    """Owns one replay run and serializes every state change through a single lock."""

    def __init__(
        self,
        store: ReplayRunStore,
        *,
        dataset_root: Path,
        mono_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._store = store
        self._root = dataset_root
        self._mono = mono_ns
        self._lock = threading.RLock()
        self._controller: ReplayController | None = None
        self._broker: SimulatedBroker | None = None
        self._market: ReplayMarket | None = None
        self._config: ReplayRunConfig | None = None
        self._manifest: DatasetManifest | None = None
        run = store.load_run()
        if run is not None:
            self._attach(ReplayRunConfig.model_validate_json(run.config_json), run)

    # -- composition -------------------------------------------------------
    def _attach(self, config: ReplayRunConfig, run: RunRecord) -> None:
        from zoneinfo import ZoneInfo

        manifest = load_manifest(self._root / config.manifest_path)
        self._manifest = manifest
        checksums = verify_manifest(
            manifest,
            self._root,
            symbols=list(config.symbols),
            start_utc=config.start_utc,
            end_utc=config.end_utc,
            warmup_start_utc=config.warmup_start_utc,
        )
        if tuple(checksums) != run.dataset_checksums:
            raise DatasetError("dataset checksums differ from the frozen run")
        self._market = load_market(
            manifest,
            self._root,
            symbols=config.symbols,
            generation=run.generation,
            zone=ZoneInfo(manifest.exchange_timezone),
        )
        self._config = config
        timeline = ReplayTimeline(self._store)
        self._broker = SimulatedBroker(store=self._store, market=self._market, config=config, timeline=timeline)
        self._controller = ReplayController(
            self._store,
            handler=self._broker.handle,
            mono_ns=self._mono,
            on_finish=self._broker.finish,
        )

    @property
    def configured(self) -> bool:
        return self._controller is not None

    def create(self, config: ReplayRunConfig, *, run_id: str, generation: str) -> RunStatus:
        """Verify inputs, freeze the dataset checksums and prepare the run (paused)."""
        manifest = load_manifest(self._root / config.manifest_path)
        checksums = verify_manifest(
            manifest,
            self._root,
            symbols=list(config.symbols),
            start_utc=config.start_utc,
            end_utc=config.end_utc,
            warmup_start_utc=config.warmup_start_utc,
        )
        from zoneinfo import ZoneInfo

        market = load_market(
            manifest,
            self._root,
            symbols=config.symbols,
            generation=generation,
            zone=ZoneInfo(manifest.exchange_timezone),
        )
        broker = SimulatedBroker(store=self._store, market=market, config=config, timeline=ReplayTimeline(self._store))
        with self._lock:
            controller = ReplayController(
                self._store,
                handler=broker.handle,
                mono_ns=self._mono,
                on_finish=broker.finish,
            )
            controller.create(
                config,
                run_id=run_id,
                generation=generation,
                dataset_checksums=tuple(checksums),
                setup=lambda conn: broker.install_feed(
                    conn,
                    start_us=config.start_us,
                    end_us=config.end_us,
                    max_queued=config.limits.max_queued_events,
                ),
            )
            self._market, self._broker, self._config, self._controller = market, broker, config, controller
            self._manifest = manifest
        return self.status()

    # -- control ---------------------------------------------------------------
    def _require(self) -> ReplayController:
        if self._controller is None:
            raise LookupError("no replay run exists on this simulator")
        return self._controller

    def status(self) -> RunStatus:
        with self._lock:
            status = self._require().status()
            outstanding = self._store._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM broker_intents WHERE outcome IS NULL OR outcome_visible_us > ?",
                (self._cursor_us(),),
            ).fetchone()[0]
            manifest = self._manifest
            assert manifest is not None
            return replace(
                status,
                outstanding_intents=outstanding,
                source_coverage={
                    "coverage_start_utc": manifest.coverage_start_utc.isoformat(),
                    "coverage_end_utc": manifest.coverage_end_utc.isoformat(),
                    "symbols": list(self.config.symbols),
                },
            )

    def command(self, **kwargs):
        with self._lock:
            return self._require().command(**kwargs)

    def tick(self) -> int:
        with self._lock:
            if self._controller is None:
                return 0
            return self._controller.tick()

    def journal(self):
        with self._lock:
            return self._require().journal()

    @property
    def config(self) -> ReplayRunConfig:
        if self._config is None:
            raise LookupError("no replay run exists on this simulator")
        return self._config

    @property
    def market(self) -> ReplayMarket:
        if self._market is None:
            raise LookupError("no replay run exists on this simulator")
        return self._market

    # -- market gateway ingress --------------------------------------------------
    def gateway_ingress(self) -> Ingress:
        status = self.status()
        return Ingress(
            visible_us=status.visible_cursor_us,
            clock={
                "sequence": status.revision,
                "generation": status.generation,
                "market_time_us": status.canonical_cursor_us,
                "market_time_utc": datetime.fromtimestamp(status.canonical_cursor_us / 1e6, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "rate": status.rate,
                "state": (
                    "running"
                    if status.state == "running"
                    else ("completed" if status.state == "completed" else "paused")
                ),
            },
        )

    # -- broker edge --------------------------------------------------------------
    def _cursor_us(self) -> int:
        with self._lock:
            run = self._store.load_run()
            if run is None:
                raise LookupError("no replay run exists on this simulator")
            return run.cursor_us

    def _canonical_us(self) -> int:
        return self.status().canonical_cursor_us

    def _wait_cursor(self, target_us: int, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                run = self._store.load_run()
                if run is None:
                    raise LookupError("no replay run exists on this simulator")
                if run.cursor_us >= target_us or run.state in ("completed", "stopped", "failed"):
                    return
            if time.monotonic() >= deadline:
                raise SubmitTimeoutError("market time did not reach the response instant in time")
            time.sleep(_WAIT_POLL_SECONDS)

    def submit(self, intent_id: str, action: str, body: dict[str, Any], *, timeout_s: float) -> dict:
        """Receive one intent and answer when its outcome becomes visible (market time)."""
        with self._lock:
            ingress = self._canonical_us()
            with self._store.transaction() as conn:
                self._broker.receive(conn, intent_id=intent_id, action=action, body=body, ingress_us=ingress)
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                outcome = self._broker.intent_outcome(intent_id, self._cursor_us())
            if outcome is not None:
                outcome.pop("closes", None)
                return outcome
            if time.monotonic() >= deadline:
                raise SubmitTimeoutError("outcome not yet visible")
            time.sleep(_WAIT_POLL_SECONDS)

    def read(self, fn: Callable[[int], Any], *, response_us: int | None = None, timeout_s: float) -> Any:
        """Snapshot broker state at ingress, delivered once market time passes the response delay."""
        with self._lock:
            run = self._store.load_run()
            terminal = run is not None and run.state in ("completed", "stopped", "failed")
            ingress = self._cursor_us() if terminal else self._canonical_us()
            delay = self.config.latency.broker_response_ms * 1000 if response_us is None else response_us
            snapshot = fn(ingress)
        if terminal:
            return snapshot
        self._wait_cursor(ingress + delay, timeout_s)
        return snapshot

    def broker(self) -> SimulatedBroker:
        if self._broker is None:
            raise LookupError("no replay run exists on this simulator")
        return self._broker

    def ingress_intent(self, intent_id: str, action: str, body: dict[str, Any]) -> int:
        """Test helper: receive an intent and return its ingress instant."""
        with self._lock:
            ingress = self._canonical_us()
            with self._store.transaction() as conn:
                self._broker.receive(conn, intent_id=intent_id, action=action, body=body, ingress_us=ingress)
            return ingress


class Driver:
    """Real-time loop advancing the run: market time follows the wall clock at the run rate."""

    def __init__(self, simulator: ReplaySimulator, interval_s: float = 0.005) -> None:
        self._simulator = simulator
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.errors: list[BaseException] = []

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._simulator.tick()
            except Exception as exc:  # noqa: BLE001 - recorded; the controller already marked the run failed
                self.errors.append(exc)
                return
            self._stop.wait(self._interval)


__all__ = ["Driver", "ReplaySimulator", "SubmitTimeoutError"]
