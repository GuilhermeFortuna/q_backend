"""Startup recovery for the execution worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from q_backend.execution.bar_coordinator import (
    BarCoordinator,
    BarStreamKey,
    DeploymentBarConsumer,
)
from q_backend.execution.brokers.base import QuoteSource
from q_backend.execution.domain import DeploymentLifecycle, StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.ledger import ExecutionLedger
from q_backend.backtesting.models import Trade
from q_backend.execution.position_adapter import execution_position_to_trade
from q_backend.storage.db.execution_models import ExecutionDeployment
from q_backend.storage.db.execution_repositories import (
    get_open_net_position,
    list_deployments,
    mark_incomplete_orders_unknown,
    release_expired_leases,
)


@dataclass(frozen=True)
class RecoveryResult:
    deployments_recovered: int
    leases_released: int
    unknown_orders_marked: int
    failed_closed: bool
    message: str


@dataclass
class DeploymentRuntime:
    deployment: ExecutionDeployment
    identity: StrategyIdentity
    evaluator: StrategyEvaluator
    lease_token: str


def open_trade_for_deployment(
    session: Session,
    deployment: ExecutionDeployment,
    *,
    point_value: float,
) -> Optional[Trade]:
    """Read the durable net position and map it to the evaluator's open trade."""
    position = get_open_net_position(session, deployment.id)
    if position is None or not position.is_open:
        return None
    return execution_position_to_trade(
        deployment_id=deployment.id,
        symbol=deployment.symbol,
        side=position.side,
        quantity=position.quantity,
        average_entry_price=position.average_entry_price,
        opened_at=position.opened_at,
        point_value=point_value,
    )


def _identity_from_deployment(deployment: ExecutionDeployment) -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name=deployment.strategy_name,
        strategy_version=deployment.strategy_version,
        compiled_config=deployment.compiled_config,
        config_hash=deployment.config_hash,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        sizing_config=deployment.sizing_config,
        risk_config=deployment.risk_config,
    )


class ExecutionRecovery:
    """Fail-closed startup recovery for leases, orders, and evaluator state."""

    def __init__(
        self,
        *,
        coordinator: BarCoordinator,
        quote_source: QuoteSource,
        ledger: ExecutionLedger,
        point_value: Decimal,
        initial_window_bars: int,
    ) -> None:
        self._coordinator = coordinator
        self._quote_source = quote_source
        self._ledger = ledger
        self._point_value = point_value
        self._initial_window_bars = initial_window_bars

    def recover(
        self,
        session: Session,
        *,
        worker_id: str,
        now: Optional[datetime] = None,
    ) -> RecoveryResult:
        ts = now or datetime.now(timezone.utc)
        leases_released = release_expired_leases(session, now=ts)
        unknown_marked = 0
        for deployment in list_deployments(session):
            unknown_marked += mark_incomplete_orders_unknown(session, deployment.id)

        running = list_deployments(
            session,
            lifecycles=[DeploymentLifecycle.RUNNING.value],
        )
        if running:
            for deployment in running:
                if self._quote_source.get_quote(deployment.symbol) is None:
                    return RecoveryResult(
                        deployments_recovered=0,
                        leases_released=leases_released,
                        unknown_orders_marked=unknown_marked,
                        failed_closed=True,
                        message=f"quote unavailable for {deployment.symbol}",
                    )

        return RecoveryResult(
            deployments_recovered=0,
            leases_released=leases_released,
            unknown_orders_marked=unknown_marked,
            failed_closed=False,
            message="recovery complete",
        )

    def build_runtime(
        self,
        session: Session,
        deployment: ExecutionDeployment,
        *,
        worker_id: str,
        lease_token: str,
        point_value: float,
        initial_capital: float,
    ) -> DeploymentRuntime:
        identity = _identity_from_deployment(deployment)
        trade = open_trade_for_deployment(session, deployment, point_value=point_value)
        evaluator = StrategyEvaluator(
            deployment_id=str(deployment.id),
            identity=identity,
            initial_capital=initial_capital,
            point_value=point_value,
            open_trade=trade,
            last_evaluated_close=deployment.last_bar_close_time,
        )
        consumer = DeploymentBarConsumer(
            str(deployment.id),
            deployment.symbol,
            deployment.timeframe,
            last_evaluated_close=deployment.last_bar_close_time,
        )
        batches = self._coordinator.poll(
            [consumer],
            initial_window_bars=self._initial_window_bars,
        )
        key = BarStreamKey(consumer.symbol, consumer.timeframe.upper())
        batch = batches.get(key)
        if batch is not None and not batch.frame.empty:
            if deployment.last_bar_close_time is not None:
                evaluator.replay_recovery(
                    batch.frame,
                    end_close=deployment.last_bar_close_time,
                )
            else:
                evaluator.seed_window(batch.frame)

        return DeploymentRuntime(
            deployment=deployment,
            identity=identity,
            evaluator=evaluator,
            lease_token=lease_token,
        )
