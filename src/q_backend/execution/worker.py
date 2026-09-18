"""Standalone forward execution worker with leases, polling, and graceful shutdown."""

from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

from q_backend.execution.bar_coordinator import BarCoordinator, DeploymentBarConsumer
from q_backend.execution.brokers.base import ExecutionBroker, PaperCostConfig, QuoteSource
from q_backend.execution.domain import DeploymentLifecycle
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.reconciliation import OrderReconciler
from q_backend.execution.recovery import DeploymentRuntime, ExecutionRecovery, open_trade_for_deployment
from q_backend.execution.service import ExecutionService
from q_backend.storage.db.execution_repositories import (
    LeaseConflictError,
    acquire_worker_lease,
    get_execution_deployment,
    get_paper_account,
    heartbeat_worker_lease,
    list_deployments,
    release_worker_lease,
)
from q_backend.storage.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class WorkerHealth:
    worker_id: str
    running: bool
    active_deployments: int
    last_poll_at: Optional[datetime] = None
    last_error: Optional[str] = None


@dataclass
class ExecutionWorker:
    """Serialized MT5 owner for paper deployment evaluation and order submission."""

    session_factory: sessionmaker[Session]
    coordinator: BarCoordinator
    quote_source: QuoteSource
    broker: ExecutionBroker
    ledger: ExecutionLedger
    service: ExecutionService
    recovery: ExecutionRecovery
    settings: Settings = field(default_factory=get_settings)
    clock: Callable[[], datetime] = field(default_factory=lambda: lambda: datetime.now(timezone.utc))
    poll_interval_seconds: Optional[float] = None
    shutdown_event: threading.Event = field(default_factory=threading.Event)

    _runtimes: dict[uuid.UUID, DeploymentRuntime] = field(default_factory=dict, init=False)
    _lease_tokens: dict[uuid.UUID, str] = field(default_factory=dict, init=False)
    _health: WorkerHealth = field(init=False)
    _running: bool = field(default=False, init=False)
    _reconciler: Optional[OrderReconciler] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._health = WorkerHealth(
            worker_id=self.settings.execution_worker_id,
            running=False,
            active_deployments=0,
        )

    def _order_reconciler(self) -> OrderReconciler:
        if self._reconciler is None:
            self._reconciler = OrderReconciler(
                broker=self.broker,
                ledger=self.ledger,
                point_value=Decimal(str(self.settings.execution_default_point_value)),
                clock=self.clock,
                worker_id=self.settings.execution_worker_id,
            )
        return self._reconciler

    @property
    def health(self) -> WorkerHealth:
        return WorkerHealth(
            worker_id=self._health.worker_id,
            running=self._running,
            active_deployments=len(self._runtimes),
            last_poll_at=self._health.last_poll_at,
            last_error=self._health.last_error,
        )

    def request_shutdown(self) -> None:
        self.shutdown_event.set()

    def run(self) -> None:
        """Block until shutdown is requested."""
        self._install_signal_handlers()
        self._running = True
        self._health.running = True
        try:
            with self.session_factory() as session:
                result = self.recovery.recover(
                    session,
                    worker_id=self.settings.execution_worker_id,
                )
                session.commit()
                if result.failed_closed:
                    raise RuntimeError(result.message)
                logger.info(
                    "execution recovery: leases=%s unknown_orders=%s",
                    result.leases_released,
                    result.unknown_orders_marked,
                )
                # Resolve any pending unknowns before the poll loop emits new
                # decisions, so a restart never trades on top of an ambiguous order.
                reconciled = self._order_reconciler().reconcile_all_pending(session)
                session.commit()
                if reconciled:
                    logger.info("startup reconciliation: resolved=%s", len(reconciled))

            interval = (
                self.poll_interval_seconds
                if self.poll_interval_seconds is not None
                else self.settings.execution_poll_interval_seconds
            )
            while not self.shutdown_event.is_set():
                try:
                    self.poll_once()
                except Exception as exc:
                    self._health.last_error = str(exc)
                    logger.exception("execution worker poll failed")
                self.shutdown_event.wait(interval)
        finally:
            self._shutdown()
            self._running = False
            self._health.running = False

    def poll_once(self) -> None:
        """Single poll cycle: refresh deployments, heartbeat leases, process bars."""
        now = self.clock()
        with self.session_factory() as session:
            self._refresh_deployments(session, now=now)
            # Reconcile pending unknowns for leased deployments before processing
            # any new bars. reconcile_deployment is a no-op when nothing is pending.
            reconciler = self._order_reconciler()
            for runtime in list(self._runtimes.values()):
                reconciler.reconcile_deployment(session, runtime.deployment)
            point_value_float = float(self.settings.execution_default_point_value)
            for runtime in list(self._runtimes.values()):
                self._sync_open_trade(session, runtime, point_value=point_value_float)
            consumers = [
                DeploymentBarConsumer(
                    str(runtime.deployment.id),
                    runtime.deployment.symbol,
                    runtime.deployment.timeframe,
                    last_evaluated_close=runtime.evaluator.last_evaluated_close,
                )
                for runtime in self._runtimes.values()
                if runtime.deployment.lifecycle == DeploymentLifecycle.RUNNING.value
            ]
            if not consumers:
                session.commit()
                self._health.last_poll_at = now
                return

            bar_started = time.perf_counter()
            batches = self.coordinator.poll(
                consumers,
                initial_window_bars=self.settings.execution_initial_window_bars,
            )
            bar_detection_ms = (time.perf_counter() - bar_started) * 1000.0

            cost_config = self._cost_config()
            point_value = Decimal(str(self.settings.execution_default_point_value))

            for runtime in list(self._runtimes.values()):
                deployment = get_execution_deployment(session, runtime.deployment.id)
                if deployment is None:
                    continue
                runtime.deployment = deployment
                if deployment.pending_action == "flatten":
                    lease_token = self._lease_tokens.get(deployment.id)
                    if lease_token is not None:
                        from q_backend.storage.db.execution_repositories import (
                            clear_pending_deployment_action,
                        )

                        self.service.flatten_deployment(
                            session,
                            deployment=deployment,
                            lease_token=lease_token,
                            worker_id=self.settings.execution_worker_id,
                            cost_config=cost_config,
                            point_value=point_value,
                        )
                        clear_pending_deployment_action(
                            session, deployment.id, producer=self.settings.execution_worker_id
                        )
                        self._sync_open_trade(session, runtime, point_value=float(point_value))
                    continue

            for runtime in list(self._runtimes.values()):
                if runtime.deployment.lifecycle != DeploymentLifecycle.RUNNING.value:
                    continue
                deployment = get_execution_deployment(session, runtime.deployment.id)
                if deployment is None:
                    continue
                runtime.deployment = deployment
                key_symbol = deployment.symbol
                key_tf = deployment.timeframe.upper()
                from q_backend.execution.bar_coordinator import BarStreamKey

                batch = batches.get(BarStreamKey(key_symbol, key_tf))
                if batch is None:
                    continue
                consumer = DeploymentBarConsumer(
                    str(deployment.id),
                    deployment.symbol,
                    deployment.timeframe,
                    last_evaluated_close=runtime.evaluator.last_evaluated_close,
                )
                new_bars = self.coordinator.new_bars_for_consumer(batch, consumer)
                if new_bars.empty:
                    continue
                lease_token = self._lease_tokens[deployment.id]
                for open_time in new_bars.sort_index().index:
                    bar = new_bars.loc[[open_time]]
                    eval_result = runtime.evaluator.ingest_completed_bar(bar)
                    if eval_result is None:
                        continue
                    process_result = self.service.process_completed_bar(
                        session,
                        deployment=deployment,
                        eval_result=eval_result,
                        lease_token=lease_token,
                        worker_id=self.settings.execution_worker_id,
                        cost_config=cost_config,
                        point_value=point_value,
                    )
                    self._sync_open_trade(session, runtime, point_value=float(point_value))
                    timing_log = process_result.timing.model_copy(update={"bar_detection_ms": bar_detection_ms})
                    if process_result.timing.total_ms > 0:
                        logger.info(
                            "bar processed deployment=%s outcome=%s timing=%s",
                            deployment.id,
                            process_result.outcome.value,
                            timing_log.to_log_dict(),
                        )

            session.commit()
            self._health.last_poll_at = now
            self._health.last_error = None

    def _sync_open_trade(self, session: Session, runtime: DeploymentRuntime, *, point_value: float) -> None:
        trade = open_trade_for_deployment(session, runtime.deployment, point_value=point_value)
        runtime.evaluator.set_open_trade(trade)

    def _refresh_deployments(self, session: Session, *, now: datetime) -> None:
        active_ids: set[uuid.UUID] = set()
        for deployment in list_deployments(
            session,
            lifecycles=[
                DeploymentLifecycle.RUNNING.value,
                DeploymentLifecycle.PAUSED.value,
            ],
        ):
            active_ids.add(deployment.id)
            lifecycle = DeploymentLifecycle(deployment.lifecycle)
            if lifecycle == DeploymentLifecycle.PAUSED:
                if deployment.id in self._runtimes:
                    self._heartbeat_lease(session, deployment.id, now=now)
                continue

            if deployment.id not in self._runtimes:
                token = str(uuid.uuid4())
                try:
                    acquire_worker_lease(
                        session,
                        deployment_id=deployment.id,
                        worker_id=self.settings.execution_worker_id,
                        lease_token=token,
                        ttl_seconds=self.settings.execution_lease_ttl_seconds,
                        now=now,
                    )
                except LeaseConflictError:
                    logger.warning(
                        "skipping deployment %s: lease held by another worker",
                        deployment.id,
                    )
                    continue
                account = get_paper_account(session, deployment.paper_account_id)
                initial_capital = float(account.initial_balance) if account is not None else 100_000.0
                runtime = self.recovery.build_runtime(
                    session,
                    deployment,
                    worker_id=self.settings.execution_worker_id,
                    lease_token=token,
                    point_value=float(self.settings.execution_default_point_value),
                    initial_capital=initial_capital,
                )
                self._runtimes[deployment.id] = runtime
                self._lease_tokens[deployment.id] = token
            else:
                self._heartbeat_lease(session, deployment.id, now=now)
                self._runtimes[deployment.id].deployment = deployment

        for deployment_id in list(self._runtimes):
            if deployment_id not in active_ids:
                self._release_deployment(session, deployment_id)

    def _heartbeat_lease(self, session: Session, deployment_id: uuid.UUID, *, now: datetime) -> None:
        token = self._lease_tokens.get(deployment_id)
        if token is None:
            return
        heartbeat_worker_lease(
            session,
            deployment_id=deployment_id,
            worker_id=self.settings.execution_worker_id,
            lease_token=token,
            ttl_seconds=self.settings.execution_lease_ttl_seconds,
            now=now,
        )

    def _release_deployment(self, session: Session, deployment_id: uuid.UUID) -> None:
        token = self._lease_tokens.pop(deployment_id, None)
        self._runtimes.pop(deployment_id, None)
        if token is None:
            return
        try:
            release_worker_lease(
                session,
                deployment_id=deployment_id,
                worker_id=self.settings.execution_worker_id,
                lease_token=token,
            )
        except ValueError:
            logger.warning("lease release mismatch for deployment %s", deployment_id)

    def _shutdown(self) -> None:
        with self.session_factory() as session:
            for deployment_id in list(self._runtimes):
                self._release_deployment(session, deployment_id)
            session.commit()

    def _cost_config(self) -> PaperCostConfig:
        return PaperCostConfig(
            point_value=Decimal(str(self.settings.execution_default_point_value)),
            slippage_points=Decimal(str(self.settings.execution_paper_slippage_points)),
            cost_per_contract=Decimal(str(self.settings.execution_paper_cost_per_contract)),
            cost_bps=Decimal(str(self.settings.execution_paper_cost_bps)),
            max_quote_age_seconds=self.settings.execution_max_quote_age_seconds,
        )

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame) -> None:  # noqa: ARG001
            logger.info("shutdown signal %s received", signum)
            self.request_shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:
                pass
