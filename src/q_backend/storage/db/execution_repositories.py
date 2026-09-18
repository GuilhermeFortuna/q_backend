"""Focused repositories for forward execution persistence.

All mutation helpers accept an existing SQLAlchemy session so the execution worker
can atomically commit intent, ledger, and position changes in one transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionOrderType,
    ExecutionSide,
    IllegalLifecycleTransition,
    LedgerEntryType,
    PositionSide,
    ReconciliationState,
    RiskRejectionCode,
    SignalAction,
    StrategyIdentity,
    validate_deployment_transition,
    validate_order_transition,
)
from q_backend.storage.db.execution_models import (
    ExecutionControlState,
    ExecutionDecision,
    ExecutionDeployment,
    ExecutionFill,
    ExecutionLedgerEntry,
    ExecutionNetPosition,
    ExecutionOrder,
    ExecutionRiskEvent,
    ExecutionWorkerHeartbeat,
    ExecutionWorkerLease,
    PaperAccount,
)
from q_backend.streaming import execution_events


class LeaseConflictError(ValueError):
    """Raised when another worker holds the active deployment lease."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --- Paper accounts ---


def create_paper_account(
    session: Session,
    *,
    name: str,
    initial_balance: Decimal,
    currency: str = "BRL",
    sizing_config: Optional[dict[str, Any]] = None,
    risk_config: Optional[dict[str, Any]] = None,
) -> PaperAccount:
    account = PaperAccount(
        name=name,
        currency=currency,
        initial_balance=initial_balance,
        cash_balance=initial_balance,
        sizing_config=sizing_config or {},
        risk_config=risk_config or {},
    )
    session.add(account)
    session.flush()
    return account


def get_paper_account(session: Session, account_id: uuid.UUID) -> Optional[PaperAccount]:
    return session.get(PaperAccount, account_id)


def get_paper_account_by_name(session: Session, name: str) -> Optional[PaperAccount]:
    return session.execute(select(PaperAccount).where(PaperAccount.name == name)).scalar_one_or_none()


def update_paper_cash_balance(
    session: Session,
    account_id: uuid.UUID,
    cash_balance: Decimal,
) -> PaperAccount:
    account = session.get(PaperAccount, account_id)
    if account is None:
        raise ValueError(f"PaperAccount {account_id} not found")
    account.cash_balance = cash_balance
    session.flush()
    return account


# --- Deployments ---


def create_execution_deployment(
    session: Session,
    *,
    paper_account_id: uuid.UUID,
    name: str,
    identity: StrategyIdentity,
    broker_mode: BrokerMode = BrokerMode.PAPER,
    lifecycle: DeploymentLifecycle = DeploymentLifecycle.DRAFT,
    live_activation_enabled: bool = False,
    producer: str = "api",
) -> ExecutionDeployment:
    deployment = ExecutionDeployment(
        paper_account_id=paper_account_id,
        name=name,
        broker_mode=broker_mode.value,
        lifecycle=lifecycle.value,
        strategy_name=identity.strategy_name,
        strategy_version=identity.strategy_version,
        compiled_config=identity.compiled_config,
        config_hash=identity.config_hash,
        symbol=identity.symbol,
        timeframe=identity.timeframe,
        sizing_config=identity.sizing_config,
        risk_config=identity.risk_config,
        live_activation_enabled=live_activation_enabled,
    )
    session.add(deployment)
    session.flush()
    execution_events.emit(
        session,
        "deployments",
        execution_events.deployment_state(deployment),
        producer_id=producer,
    )
    return deployment


def get_execution_deployment(session: Session, deployment_id: uuid.UUID) -> Optional[ExecutionDeployment]:
    return session.get(ExecutionDeployment, deployment_id)


def transition_deployment_lifecycle(
    session: Session,
    deployment_id: uuid.UUID,
    target: DeploymentLifecycle,
    *,
    at: Optional[datetime] = None,
    producer: str = "api",
) -> ExecutionDeployment:
    deployment = session.get(ExecutionDeployment, deployment_id)
    if deployment is None:
        raise ValueError(f"ExecutionDeployment {deployment_id} not found")
    current = DeploymentLifecycle(deployment.lifecycle)
    validate_deployment_transition(current, target)
    deployment.lifecycle = target.value
    now = at or _utcnow()
    if target == DeploymentLifecycle.RUNNING and deployment.started_at is None:
        deployment.started_at = now
    if target in {DeploymentLifecycle.STOPPED, DeploymentLifecycle.ERROR}:
        deployment.stopped_at = now
    session.flush()
    execution_events.emit(
        session,
        "deployments",
        execution_events.deployment_state(deployment),
        producer_id=producer,
    )
    return deployment


# --- Decisions ---


def create_execution_decision(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    bar_close_time: datetime,
    identity: StrategyIdentity,
    signal_action: SignalAction,
    outcome: DecisionOutcome,
    requested_quantity: Optional[Decimal] = None,
    reason: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
    producer: str = "api",
) -> ExecutionDecision:
    decision = ExecutionDecision(
        deployment_id=deployment_id,
        bar_close_time=bar_close_time,
        strategy_name=identity.strategy_name,
        strategy_version=identity.strategy_version,
        compiled_config=identity.compiled_config,
        config_hash=identity.config_hash,
        symbol=identity.symbol,
        timeframe=identity.timeframe,
        sizing_config=identity.sizing_config,
        risk_config=identity.risk_config,
        signal_action=signal_action.value,
        outcome=outcome.value,
        requested_quantity=requested_quantity,
        reason=reason,
        context=context or {},
    )
    session.add(decision)
    session.flush()
    execution_events.emit(
        session,
        "decisions",
        execution_events.decision_state(decision),
        producer_id=producer,
    )
    return decision


def get_decision_for_bar(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    bar_close_time: datetime,
) -> Optional[ExecutionDecision]:
    return session.execute(
        select(ExecutionDecision).where(
            ExecutionDecision.deployment_id == deployment_id,
            ExecutionDecision.bar_close_time == bar_close_time,
        )
    ).scalar_one_or_none()


# --- Orders ---


def create_execution_order_intent(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    decision_id: Optional[uuid.UUID],
    broker_mode: BrokerMode,
    side: ExecutionSide,
    quantity: Decimal,
    order_type: ExecutionOrderType = ExecutionOrderType.MARKET,
    metadata: Optional[dict[str, Any]] = None,
    intent_committed_at: Optional[datetime] = None,
    producer: str = "api",
) -> ExecutionOrder:
    order = ExecutionOrder(
        deployment_id=deployment_id,
        decision_id=decision_id,
        broker_mode=broker_mode.value,
        side=side.value,
        order_type=order_type.value,
        quantity=quantity,
        status=ExecutionOrderStatus.INTENT.value,
        reconciliation_state=ReconciliationState.NOT_APPLICABLE.value,
        intent_committed_at=intent_committed_at or _utcnow(),
        details=metadata or {},
    )
    session.add(order)
    session.flush()
    execution_events.emit(
        session,
        "orders",
        execution_events.order_state(order),
        producer_id=producer,
    )
    return order


def transition_execution_order(
    session: Session,
    order_id: uuid.UUID,
    target: ExecutionOrderStatus,
    *,
    reconciliation_state: Optional[ReconciliationState] = None,
    external_order_id: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    submitted_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    producer: str = "api",
) -> ExecutionOrder:
    order = session.get(ExecutionOrder, order_id)
    if order is None:
        raise ValueError(f"ExecutionOrder {order_id} not found")
    current = ExecutionOrderStatus(order.status)
    validate_order_transition(current, target)
    order.status = target.value
    if reconciliation_state is not None:
        order.reconciliation_state = reconciliation_state.value
    if external_order_id is not None:
        order.external_order_id = external_order_id
    if rejection_reason is not None:
        order.rejection_reason = rejection_reason
    if submitted_at is not None:
        order.submitted_at = submitted_at
    if completed_at is not None:
        order.completed_at = completed_at
    session.flush()
    execution_events.emit(
        session,
        "orders",
        execution_events.order_state(order),
        producer_id=producer,
    )
    return order


# --- Fills ---


def create_execution_fill(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    order_id: uuid.UUID,
    broker_mode: BrokerMode,
    external_fill_id: str,
    side: ExecutionSide,
    quantity: Decimal,
    price: Decimal,
    filled_at: datetime,
    fee: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
    quote_bid: Optional[Decimal] = None,
    quote_ask: Optional[Decimal] = None,
    quote_timestamp: Optional[datetime] = None,
    metadata: Optional[dict[str, Any]] = None,
    position: Optional[ExecutionNetPosition] = None,
    producer: str = "api",
) -> ExecutionFill:
    fill = ExecutionFill(
        deployment_id=deployment_id,
        order_id=order_id,
        broker_mode=broker_mode.value,
        external_fill_id=external_fill_id,
        side=side.value,
        quantity=quantity,
        price=price,
        fee=fee,
        slippage=slippage,
        quote_bid=quote_bid,
        quote_ask=quote_ask,
        quote_timestamp=quote_timestamp,
        filled_at=filled_at,
        details=metadata or {},
    )
    session.add(fill)
    session.flush()
    if position is None:
        position = get_open_net_position(session, deployment_id)
    execution_events.emit(
        session,
        "fills",
        execution_events.fill_event(fill, position),
        producer_id=producer,
    )
    return fill


def get_execution_fill_by_external_id(
    session: Session,
    *,
    broker_mode: str,
    external_fill_id: str,
) -> Optional[ExecutionFill]:
    return session.execute(
        select(ExecutionFill).where(
            ExecutionFill.broker_mode == broker_mode,
            ExecutionFill.external_fill_id == external_fill_id,
        )
    ).scalar_one_or_none()


# --- Net positions ---


def get_open_net_position(session: Session, deployment_id: uuid.UUID) -> Optional[ExecutionNetPosition]:
    return session.execute(
        select(ExecutionNetPosition).where(
            ExecutionNetPosition.deployment_id == deployment_id,
            ExecutionNetPosition.is_open.is_(True),
        )
    ).scalar_one_or_none()


def upsert_open_net_position(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    side: PositionSide,
    quantity: Decimal,
    average_entry_price: Optional[Decimal],
    opened_at: Optional[datetime] = None,
) -> ExecutionNetPosition:
    position = get_open_net_position(session, deployment_id)
    now = opened_at or _utcnow()
    if side == PositionSide.FLAT or quantity == 0:
        if position is not None:
            position.side = PositionSide.FLAT.value
            position.quantity = Decimal("0")
            position.average_entry_price = None
            position.is_open = False
            position.closed_at = now
            session.flush()
            return position
        closed = ExecutionNetPosition(
            deployment_id=deployment_id,
            side=PositionSide.FLAT.value,
            quantity=Decimal("0"),
            average_entry_price=None,
            is_open=False,
            closed_at=now,
        )
        session.add(closed)
        session.flush()
        return closed

    if position is None:
        position = ExecutionNetPosition(
            deployment_id=deployment_id,
            side=side.value,
            quantity=quantity,
            average_entry_price=average_entry_price,
            is_open=True,
            opened_at=now,
        )
        session.add(position)
    else:
        position.side = side.value
        position.quantity = quantity
        position.average_entry_price = average_entry_price
        position.is_open = True
        if position.opened_at is None:
            position.opened_at = now
        position.closed_at = None
    session.flush()
    return position


# --- Ledger ---


def append_ledger_entry(
    session: Session,
    *,
    paper_account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    entry_type: LedgerEntryType,
    amount: Decimal,
    balance_after: Decimal,
    fill_id: Optional[uuid.UUID] = None,
    description: Optional[str] = None,
    account: Optional[PaperAccount] = None,
    producer: str = "api",
) -> ExecutionLedgerEntry:
    entry = ExecutionLedgerEntry(
        paper_account_id=paper_account_id,
        deployment_id=deployment_id,
        fill_id=fill_id,
        entry_type=entry_type.value,
        amount=amount,
        balance_after=balance_after,
        description=description,
    )
    session.add(entry)
    session.flush()
    if account is None:
        account = session.get(PaperAccount, paper_account_id)
    execution_events.emit(
        session,
        "ledger",
        execution_events.ledger_event(entry, account),
        producer_id=producer,
    )
    return entry


# --- Risk events ---


def record_risk_event(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    rejection_code: RiskRejectionCode,
    message: str,
    decision_id: Optional[uuid.UUID] = None,
    order_id: Optional[uuid.UUID] = None,
    context: Optional[dict[str, Any]] = None,
    producer: str = "api",
) -> ExecutionRiskEvent:
    event = ExecutionRiskEvent(
        deployment_id=deployment_id,
        decision_id=decision_id,
        order_id=order_id,
        rejection_code=rejection_code.value,
        message=message,
        context=context or {},
    )
    session.add(event)
    session.flush()
    execution_events.emit(
        session,
        "risk",
        execution_events.risk_rejection_event(event),
        producer_id=producer,
    )
    return event


# --- Worker heartbeat ---


@dataclass(frozen=True)
class EdgeHealthSnapshot:
    """Result of one worker-side edge health check."""

    reachable: bool
    mt5_connected: Optional[bool]
    terminal_build: Optional[int]
    checked_at: datetime


def record_worker_heartbeat(
    session: Session,
    *,
    worker_id: str,
    started_at: datetime,
    version: str,
    edge: EdgeHealthSnapshot,
    now: datetime,
) -> None:
    row = session.get(ExecutionWorkerHeartbeat, worker_id)
    if row is None:
        row = ExecutionWorkerHeartbeat(worker_id=worker_id)
        session.add(row)
    row.started_at = started_at
    row.heartbeat_at = now
    row.stopped_at = None
    row.version = version
    row.edge_reachable = edge.reachable
    row.edge_mt5_connected = edge.mt5_connected
    row.edge_terminal_build = edge.terminal_build
    row.edge_checked_at = edge.checked_at
    session.flush()


def record_worker_stopped(session: Session, *, worker_id: str, now: datetime) -> None:
    row = session.get(ExecutionWorkerHeartbeat, worker_id)
    if row is None:
        return
    row.stopped_at = now
    session.flush()


def get_worker_heartbeat(session: Session, worker_id: Optional[str] = None) -> Optional[ExecutionWorkerHeartbeat]:
    """Return the named worker's heartbeat, or the most recent one when no id is given."""
    if worker_id is not None:
        return session.get(ExecutionWorkerHeartbeat, worker_id)
    return session.execute(
        select(ExecutionWorkerHeartbeat).order_by(ExecutionWorkerHeartbeat.heartbeat_at.desc()).limit(1)
    ).scalar_one_or_none()


# --- Worker leases ---


def acquire_worker_lease(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    worker_id: str,
    lease_token: str,
    ttl_seconds: int,
    now: Optional[datetime] = None,
) -> ExecutionWorkerLease:
    current = session.execute(
        select(ExecutionWorkerLease).where(ExecutionWorkerLease.deployment_id == deployment_id)
    ).scalar_one_or_none()
    ts = now or _utcnow()
    expires = ts + timedelta(seconds=ttl_seconds)
    if current is not None:
        if _as_utc(current.expires_at) > _as_utc(ts) and current.worker_id != worker_id:
            raise LeaseConflictError(f"deployment {deployment_id} lease held by {current.worker_id}")
        current.worker_id = worker_id
        current.lease_token = lease_token
        current.acquired_at = ts
        current.expires_at = expires
        current.heartbeat_at = ts
        session.flush()
        return current

    lease = ExecutionWorkerLease(
        deployment_id=deployment_id,
        worker_id=worker_id,
        lease_token=lease_token,
        acquired_at=ts,
        expires_at=expires,
        heartbeat_at=ts,
    )
    session.add(lease)
    session.flush()
    return lease


def heartbeat_worker_lease(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    worker_id: str,
    lease_token: str,
    ttl_seconds: int,
    now: Optional[datetime] = None,
) -> ExecutionWorkerLease:
    lease = session.execute(
        select(ExecutionWorkerLease).where(ExecutionWorkerLease.deployment_id == deployment_id)
    ).scalar_one_or_none()
    if lease is None:
        raise ValueError(f"No lease for deployment {deployment_id}")
    if lease.worker_id != worker_id or lease.lease_token != lease_token:
        raise ValueError("lease token mismatch")
    ts = now or _utcnow()
    lease.heartbeat_at = ts
    lease.expires_at = ts + timedelta(seconds=ttl_seconds)
    session.flush()
    return lease


def release_worker_lease(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    worker_id: str,
    lease_token: str,
) -> bool:
    lease = session.execute(
        select(ExecutionWorkerLease).where(ExecutionWorkerLease.deployment_id == deployment_id)
    ).scalar_one_or_none()
    if lease is None:
        return False
    if lease.worker_id != worker_id or lease.lease_token != lease_token:
        raise ValueError("lease token mismatch")
    session.delete(lease)
    session.flush()
    return True


# --- Global control state ---


def get_execution_control_state(session: Session) -> ExecutionControlState:
    state = session.get(ExecutionControlState, 1)
    if state is None:
        state = ExecutionControlState(id=1, kill_switch_enabled=False)
        session.add(state)
        session.flush()
    return state


def set_kill_switch(
    session: Session,
    *,
    enabled: bool,
    reason: Optional[str] = None,
    updated_by: Optional[str] = None,
    producer: str = "api",
) -> ExecutionControlState:
    state = get_execution_control_state(session)
    state.kill_switch_enabled = enabled
    state.kill_switch_reason = reason
    state.updated_by = updated_by
    session.flush()
    execution_events.emit(
        session,
        "risk",
        execution_events.kill_switch_event(state),
        producer_id=producer,
    )
    return state


def list_deployments(
    session: Session,
    *,
    lifecycles: Optional[list[str]] = None,
) -> list[ExecutionDeployment]:
    stmt = select(ExecutionDeployment).order_by(ExecutionDeployment.created_at)
    if lifecycles is not None:
        stmt = stmt.where(ExecutionDeployment.lifecycle.in_(lifecycles))
    return list(session.execute(stmt).scalars().all())


def update_deployment_last_bar_close(
    session: Session,
    deployment_id: uuid.UUID,
    bar_close_time: datetime,
    *,
    producer: str = "api",
) -> ExecutionDeployment:
    deployment = session.get(ExecutionDeployment, deployment_id)
    if deployment is None:
        raise ValueError(f"ExecutionDeployment {deployment_id} not found")
    deployment.last_bar_close_time = bar_close_time
    session.flush()
    execution_events.emit(
        session,
        "deployments",
        execution_events.deployment_state(deployment),
        producer_id=producer,
    )
    return deployment


def update_execution_decision_outcome(
    session: Session,
    decision_id: uuid.UUID,
    *,
    outcome: DecisionOutcome,
    context: Optional[dict[str, Any]] = None,
    producer: str = "api",
) -> ExecutionDecision:
    decision = session.get(ExecutionDecision, decision_id)
    if decision is None:
        raise ValueError(f"ExecutionDecision {decision_id} not found")
    decision.outcome = outcome.value
    if context is not None:
        decision.context = context
    session.flush()
    execution_events.emit(
        session,
        "decisions",
        execution_events.decision_state(decision),
        producer_id=producer,
    )
    return decision


def list_orders_for_deployment(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    statuses: Optional[list[str]] = None,
) -> list[ExecutionOrder]:
    stmt = select(ExecutionOrder).where(ExecutionOrder.deployment_id == deployment_id)
    if statuses is not None:
        stmt = stmt.where(ExecutionOrder.status.in_(statuses))
    return list(session.execute(stmt.order_by(ExecutionOrder.created_at)).scalars().all())


def mark_incomplete_orders_unknown(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    reason: str = "recovery: incomplete order intent",
    producer: str = "api",
) -> int:
    incomplete = list_orders_for_deployment(
        session,
        deployment_id,
        statuses=[
            ExecutionOrderStatus.INTENT.value,
            ExecutionOrderStatus.SUBMITTED.value,
        ],
    )
    marked = 0
    for order in incomplete:
        current = ExecutionOrderStatus(order.status)
        if current == ExecutionOrderStatus.INTENT:
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.UNKNOWN,
                reconciliation_state=ReconciliationState.PENDING,
                rejection_reason=reason,
                producer=producer,
            )
            marked += 1
        elif current == ExecutionOrderStatus.SUBMITTED:
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.UNKNOWN,
                reconciliation_state=ReconciliationState.PENDING,
                rejection_reason=reason,
                producer=producer,
            )
            marked += 1
    return marked


def get_execution_order(session: Session, order_id: uuid.UUID) -> Optional[ExecutionOrder]:
    return session.get(ExecutionOrder, order_id)


def list_pending_reconciliation_orders(
    session: Session,
    *,
    deployment_id: Optional[uuid.UUID] = None,
) -> list[ExecutionOrder]:
    """Orders still awaiting reconciliation (UNKNOWN + PENDING)."""
    stmt = select(ExecutionOrder).where(
        ExecutionOrder.status == ExecutionOrderStatus.UNKNOWN.value,
        ExecutionOrder.reconciliation_state == ReconciliationState.PENDING.value,
    )
    if deployment_id is not None:
        stmt = stmt.where(ExecutionOrder.deployment_id == deployment_id)
    return list(session.execute(stmt.order_by(ExecutionOrder.created_at)).scalars().all())


def list_pending_reconciliation_orders_page(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionOrder], int]:
    stmt = (
        select(ExecutionOrder)
        .where(
            ExecutionOrder.deployment_id == deployment_id,
            ExecutionOrder.status == ExecutionOrderStatus.UNKNOWN.value,
            ExecutionOrder.reconciliation_state == ReconciliationState.PENDING.value,
        )
        .order_by(ExecutionOrder.created_at.desc())
    )
    return _paginate(session, stmt, limit=limit, offset=offset)


def record_reconciliation_attempt(
    session: Session,
    order_id: uuid.UUID,
    *,
    at: Optional[datetime] = None,
    error: Optional[str] = None,
    producer: str = "api",
) -> ExecutionOrder:
    """Record a (failed) reconciliation attempt without resolving the order."""
    order = session.get(ExecutionOrder, order_id)
    if order is None:
        raise ValueError(f"ExecutionOrder {order_id} not found")
    order.reconciliation_attempted_at = at or _utcnow()
    order.reconciliation_error = error
    session.flush()
    execution_events.emit(
        session,
        "orders",
        execution_events.order_state(order),
        producer_id=producer,
    )
    return order


def finalize_order_reconciliation(
    session: Session,
    order_id: uuid.UUID,
    *,
    reconciled_by: str,
    detail: Optional[str] = None,
    at: Optional[datetime] = None,
    producer: str = "api",
) -> ExecutionOrder:
    """Stamp who/when/why on a resolved order and clear its pending state."""
    order = session.get(ExecutionOrder, order_id)
    if order is None:
        raise ValueError(f"ExecutionOrder {order_id} not found")
    order.reconciliation_state = ReconciliationState.RECONCILED.value
    order.reconciled_at = at or _utcnow()
    order.reconciled_by = reconciled_by
    order.reconciliation_detail = detail
    order.reconciliation_error = None
    session.flush()
    execution_events.emit(
        session,
        "orders",
        execution_events.order_state(order),
        producer_id=producer,
    )
    return order


def release_expired_leases(session: Session, *, now: Optional[datetime] = None) -> int:
    ts = now or _utcnow()
    leases = list(session.execute(select(ExecutionWorkerLease)).scalars().all())
    released = 0
    for lease in leases:
        if _as_utc(lease.expires_at) <= _as_utc(ts):
            session.delete(lease)
            released += 1
    if released:
        session.flush()
    return released


def get_worker_lease(session: Session, deployment_id: uuid.UUID) -> Optional[ExecutionWorkerLease]:
    return session.execute(
        select(ExecutionWorkerLease).where(ExecutionWorkerLease.deployment_id == deployment_id)
    ).scalar_one_or_none()


def sum_realized_pnl_since(
    session: Session,
    paper_account_id: uuid.UUID,
    since: datetime,
) -> Decimal:
    from sqlalchemy import func

    total = session.execute(
        select(func.coalesce(func.sum(ExecutionLedgerEntry.amount), 0)).where(
            ExecutionLedgerEntry.paper_account_id == paper_account_id,
            ExecutionLedgerEntry.entry_type == LedgerEntryType.REALIZED_PNL.value,
            ExecutionLedgerEntry.created_at >= since,
        )
    ).scalar_one()
    return Decimal(str(total))


def list_paper_accounts(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PaperAccount], int]:
    from sqlalchemy import func

    total = session.execute(select(func.count()).select_from(PaperAccount)).scalar_one()
    rows = list(
        session.execute(select(PaperAccount).order_by(PaperAccount.created_at).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return rows, int(total)


def list_deployments_page(
    session: Session,
    *,
    paper_account_id: Optional[uuid.UUID] = None,
    lifecycle: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionDeployment], int]:
    from sqlalchemy import func

    stmt = select(ExecutionDeployment)
    if paper_account_id is not None:
        stmt = stmt.where(ExecutionDeployment.paper_account_id == paper_account_id)
    if lifecycle is not None:
        stmt = stmt.where(ExecutionDeployment.lifecycle == lifecycle)
    if symbol is not None:
        stmt = stmt.where(ExecutionDeployment.symbol == symbol)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = session.execute(count_stmt).scalar_one()
    rows = list(
        session.execute(stmt.order_by(ExecutionDeployment.created_at.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return rows, int(total)


def _paginate(
    session: Session,
    stmt,
    *,
    limit: int,
    offset: int,
) -> tuple[list, int]:
    from sqlalchemy import func, select as sa_select

    count_stmt = sa_select(func.count()).select_from(stmt.subquery())
    total = session.execute(count_stmt).scalar_one()
    rows = list(session.execute(stmt.limit(limit).offset(offset)).scalars().all())
    return rows, int(total)


def list_decisions_page(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionDecision], int]:
    stmt = select(ExecutionDecision).where(ExecutionDecision.deployment_id == deployment_id)
    if from_time is not None:
        stmt = stmt.where(ExecutionDecision.bar_close_time >= from_time)
    if to_time is not None:
        stmt = stmt.where(ExecutionDecision.bar_close_time <= to_time)
    stmt = stmt.order_by(ExecutionDecision.bar_close_time.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def list_orders_page(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    status: Optional[str] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionOrder], int]:
    stmt = select(ExecutionOrder).where(ExecutionOrder.deployment_id == deployment_id)
    if status is not None:
        stmt = stmt.where(ExecutionOrder.status == status)
    if from_time is not None:
        stmt = stmt.where(ExecutionOrder.created_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(ExecutionOrder.created_at <= to_time)
    stmt = stmt.order_by(ExecutionOrder.created_at.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def list_fills_page(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionFill], int]:
    stmt = select(ExecutionFill).where(ExecutionFill.deployment_id == deployment_id)
    if from_time is not None:
        stmt = stmt.where(ExecutionFill.filled_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(ExecutionFill.filled_at <= to_time)
    stmt = stmt.order_by(ExecutionFill.filled_at.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def list_positions_page(
    session: Session,
    *,
    deployment_id: Optional[uuid.UUID] = None,
    paper_account_id: Optional[uuid.UUID] = None,
    open_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionNetPosition], int]:
    stmt = select(ExecutionNetPosition)
    if deployment_id is not None:
        stmt = stmt.where(ExecutionNetPosition.deployment_id == deployment_id)
    if paper_account_id is not None:
        stmt = stmt.join(
            ExecutionDeployment,
            ExecutionNetPosition.deployment_id == ExecutionDeployment.id,
        ).where(ExecutionDeployment.paper_account_id == paper_account_id)
    if open_only:
        stmt = stmt.where(ExecutionNetPosition.is_open.is_(True))
    stmt = stmt.order_by(ExecutionNetPosition.updated_at.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def list_ledger_entries_page(
    session: Session,
    *,
    paper_account_id: uuid.UUID,
    deployment_id: Optional[uuid.UUID] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionLedgerEntry], int]:
    stmt = select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.paper_account_id == paper_account_id)
    if deployment_id is not None:
        stmt = stmt.where(ExecutionLedgerEntry.deployment_id == deployment_id)
    if from_time is not None:
        stmt = stmt.where(ExecutionLedgerEntry.created_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(ExecutionLedgerEntry.created_at <= to_time)
    stmt = stmt.order_by(ExecutionLedgerEntry.created_at.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def list_risk_events_page(
    session: Session,
    *,
    deployment_id: uuid.UUID,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionRiskEvent], int]:
    stmt = select(ExecutionRiskEvent).where(ExecutionRiskEvent.deployment_id == deployment_id)
    if from_time is not None:
        stmt = stmt.where(ExecutionRiskEvent.created_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(ExecutionRiskEvent.created_at <= to_time)
    stmt = stmt.order_by(ExecutionRiskEvent.created_at.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def get_latest_decision(session: Session, deployment_id: uuid.UUID) -> Optional[ExecutionDecision]:
    return session.execute(
        select(ExecutionDecision)
        .where(ExecutionDecision.deployment_id == deployment_id)
        .order_by(ExecutionDecision.bar_close_time.desc())
        .limit(1)
    ).scalar_one_or_none()


def count_unknown_orders(session: Session, *, deployment_id: Optional[uuid.UUID] = None) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(ExecutionOrder)
        .where(ExecutionOrder.status == ExecutionOrderStatus.UNKNOWN.value)
    )
    if deployment_id is not None:
        stmt = stmt.where(ExecutionOrder.deployment_id == deployment_id)
    return int(session.execute(stmt).scalar_one())


def list_active_worker_leases(session: Session) -> list[ExecutionWorkerLease]:
    return list(session.execute(select(ExecutionWorkerLease)).scalars().all())


def record_audit_event(
    session: Session,
    *,
    event_type: str,
    message: str,
    actor: Optional[str] = None,
    deployment_id: Optional[uuid.UUID] = None,
    payload: Optional[dict[str, Any]] = None,
) -> "ExecutionAuditEvent":
    from q_backend.storage.db.execution_models import ExecutionAuditEvent

    event = ExecutionAuditEvent(
        event_type=event_type,
        actor=actor,
        deployment_id=deployment_id,
        message=message,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event


def list_audit_events_page(
    session: Session,
    *,
    deployment_id: Optional[uuid.UUID] = None,
    event_type: Optional[str] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list, int]:
    from q_backend.storage.db.execution_models import ExecutionAuditEvent

    stmt = select(ExecutionAuditEvent)
    if deployment_id is not None:
        stmt = stmt.where(ExecutionAuditEvent.deployment_id == deployment_id)
    if event_type is not None:
        stmt = stmt.where(ExecutionAuditEvent.event_type == event_type)
    if from_time is not None:
        stmt = stmt.where(ExecutionAuditEvent.created_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(ExecutionAuditEvent.created_at <= to_time)
    stmt = stmt.order_by(ExecutionAuditEvent.created_at.desc())
    return _paginate(session, stmt, limit=limit, offset=offset)


def set_pending_deployment_action(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    action: str,
    at: Optional[datetime] = None,
    producer: str = "api",
) -> ExecutionDeployment:
    deployment = session.get(ExecutionDeployment, deployment_id)
    if deployment is None:
        raise ValueError(f"ExecutionDeployment {deployment_id} not found")
    deployment.pending_action = action
    deployment.pending_action_requested_at = at or _utcnow()
    session.flush()
    execution_events.emit(
        session,
        "deployments",
        execution_events.deployment_state(deployment),
        producer_id=producer,
    )
    return deployment


def clear_pending_deployment_action(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    producer: str = "api",
) -> ExecutionDeployment:
    deployment = session.get(ExecutionDeployment, deployment_id)
    if deployment is None:
        raise ValueError(f"ExecutionDeployment {deployment_id} not found")
    deployment.pending_action = None
    deployment.pending_action_requested_at = None
    session.flush()
    execution_events.emit(
        session,
        "deployments",
        execution_events.deployment_state(deployment),
        producer_id=producer,
    )
    return deployment


# Re-export domain transition errors for callers.
__all__ = [
    "EdgeHealthSnapshot",
    "get_worker_heartbeat",
    "record_worker_heartbeat",
    "record_worker_stopped",
    "IllegalLifecycleTransition",
    "LeaseConflictError",
    "acquire_worker_lease",
    "append_ledger_entry",
    "create_execution_decision",
    "create_execution_deployment",
    "create_execution_fill",
    "create_execution_order_intent",
    "create_paper_account",
    "get_decision_for_bar",
    "get_execution_control_state",
    "get_execution_deployment",
    "get_execution_fill_by_external_id",
    "get_execution_order",
    "get_open_net_position",
    "get_paper_account",
    "get_paper_account_by_name",
    "get_worker_lease",
    "heartbeat_worker_lease",
    "clear_pending_deployment_action",
    "count_unknown_orders",
    "get_latest_decision",
    "list_active_worker_leases",
    "list_audit_events_page",
    "list_decisions_page",
    "list_deployments",
    "list_deployments_page",
    "list_fills_page",
    "list_ledger_entries_page",
    "list_orders_page",
    "list_paper_accounts",
    "list_positions_page",
    "list_risk_events_page",
    "record_audit_event",
    "set_pending_deployment_action",
    "list_orders_for_deployment",
    "list_pending_reconciliation_orders",
    "list_pending_reconciliation_orders_page",
    "mark_incomplete_orders_unknown",
    "record_reconciliation_attempt",
    "finalize_order_reconciliation",
    "record_risk_event",
    "release_expired_leases",
    "release_worker_lease",
    "set_kill_switch",
    "sum_realized_pnl_since",
    "transition_deployment_lifecycle",
    "transition_execution_order",
    "update_deployment_last_bar_close",
    "update_execution_decision_outcome",
    "update_paper_cash_balance",
    "upsert_open_net_position",
]
