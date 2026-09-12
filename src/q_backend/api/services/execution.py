"""Control-plane service for paper execution (no broker/evaluator/MT5 calls)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from q_backend.api.schemas.execution import (
    AuditEventListResponse,
    AuditEventResponse,
    DecisionListResponse,
    DecisionResponse,
    DeploymentActionRequest,
    DeploymentActionResponse,
    DeploymentCreateRequest,
    DeploymentDetailResponse,
    DeploymentHealthResponse,
    DeploymentListResponse,
    DeploymentSummaryResponse,
    ExecutionHealthResponse,
    FillListResponse,
    FillResponse,
    KillSwitchResponse,
    KillSwitchUpdateRequest,
    KillSwitchUpdateResponse,
    LedgerEntryListResponse,
    LedgerEntryResponse,
    OrderListResponse,
    OrderResolutionRequest,
    OrderResolutionResponse,
    OrderResponse,
    PaperAccountCreateRequest,
    PendingReconciliationListResponse,
    PaperAccountListResponse,
    PaperAccountResponse,
    PositionListResponse,
    PositionResponse,
    RiskEventListResponse,
    RiskEventResponse,
    WorkerLeaseResponse,
)
from q_backend.execution.commands import (
    disable_kill_switch,
    enable_kill_switch,
    pause_deployment,
    start_deployment,
    stop_deployment,
)
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
    ExecutionSide,
    FillRecord,
    IllegalLifecycleTransition,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.reconciliation import (
    OrderNotPendingError,
    resolve_order_manually,
)
from q_backend.execution.validation import (
    ExecutionValidationError,
    identity_from_saved_backtest,
    validate_broker_mode,
    validate_risk_config,
    validate_strategy_identity,
)
from q_backend.storage.db.execution_models import ExecutionDeployment
from q_backend.storage.db.execution_repositories import (
    clear_pending_deployment_action,
    count_unknown_orders,
    create_execution_deployment,
    create_paper_account,
    get_execution_control_state,
    get_execution_deployment,
    get_execution_order,
    get_latest_decision,
    get_open_net_position,
    get_paper_account,
    get_paper_account_by_name,
    get_worker_lease,
    list_active_worker_leases,
    list_audit_events_page,
    list_decisions_page,
    list_deployments_page,
    list_fills_page,
    list_ledger_entries_page,
    list_orders_page,
    list_paper_accounts,
    list_pending_reconciliation_orders_page,
    list_positions_page,
    list_risk_events_page,
    record_audit_event,
    set_pending_deployment_action,
)
from q_backend.storage.settings import get_settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _http_from_validation(exc: ExecutionValidationError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _http_from_db(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="database unavailable; command was not accepted",
    )


def _lease_response(lease, *, now: datetime) -> WorkerLeaseResponse:
    return WorkerLeaseResponse(
        worker_id=lease.worker_id,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        heartbeat_at=lease.heartbeat_at,
        is_active=_as_utc(lease.expires_at) > _as_utc(now),
    )


def _decision_response(decision) -> DecisionResponse:
    return DecisionResponse.model_validate(decision)


def _deployment_summary(deployment: ExecutionDeployment) -> DeploymentSummaryResponse:
    return DeploymentSummaryResponse.model_validate(deployment)


def create_account(session: Session, body: PaperAccountCreateRequest) -> PaperAccountResponse:
    if get_paper_account_by_name(session, body.name) is not None:
        raise HTTPException(status_code=409, detail="paper account name already exists")
    try:
        validate_risk_config(body.risk_config)
        account = create_paper_account(
            session,
            name=body.name,
            initial_balance=Decimal(str(body.initial_balance)),
            currency=body.currency,
            sizing_config=body.sizing_config,
            risk_config=validate_risk_config(body.risk_config),
        )
        session.flush()
        return PaperAccountResponse.model_validate(account)
    except ExecutionValidationError as exc:
        raise _http_from_validation(exc) from exc
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc


def list_accounts(session: Session, *, limit: int, offset: int) -> PaperAccountListResponse:
    try:
        items, total = list_paper_accounts(session, limit=limit, offset=offset)
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc
    return PaperAccountListResponse(
        items=[PaperAccountResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_account(session: Session, account_id: uuid.UUID) -> PaperAccountResponse:
    account = get_paper_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    return PaperAccountResponse.model_validate(account)


def create_deployment(session: Session, body: DeploymentCreateRequest) -> DeploymentDetailResponse:
    settings = get_settings()
    account = get_paper_account(session, body.paper_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    try:
        validate_broker_mode(
            body.broker_mode,
            live_capability_locked=settings.execution_live_capability_locked,
            live_activation_enabled=body.live_activation_enabled,
        )
        if body.source_backtest_run_id is not None:
            identity = identity_from_saved_backtest(
                session,
                source_backtest_run_id=body.source_backtest_run_id,
                risk_config=body.identity.risk_config if body.identity else None,
                sizing_config=body.identity.sizing_config if body.identity else None,
            )
        elif body.identity is not None:
            identity = validate_strategy_identity(
                strategy_name=body.identity.strategy_name,
                strategy_version=body.identity.strategy_version,
                compiled_config=body.identity.compiled_config,
                config_hash=body.identity.config_hash,
                symbol=body.identity.symbol,
                timeframe=body.identity.timeframe,
                sizing_config=body.identity.sizing_config,
                risk_config=body.identity.risk_config,
            )
        else:
            raise ExecutionValidationError("either source_backtest_run_id or identity is required")
        deployment = create_execution_deployment(
            session,
            paper_account_id=body.paper_account_id,
            name=body.name,
            identity=identity,
            broker_mode=BrokerMode.PAPER,
            lifecycle=DeploymentLifecycle.DRAFT,
            live_activation_enabled=False,
        )
        session.flush()
        return get_deployment_detail(session, deployment.id)
    except ExecutionValidationError as exc:
        raise _http_from_validation(exc) from exc
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc


def list_deployments(
    session: Session,
    *,
    paper_account_id: Optional[uuid.UUID],
    lifecycle: Optional[str],
    symbol: Optional[str],
    limit: int,
    offset: int,
) -> DeploymentListResponse:
    items, total = list_deployments_page(
        session,
        paper_account_id=paper_account_id,
        lifecycle=lifecycle,
        symbol=symbol,
        limit=limit,
        offset=offset,
    )
    return DeploymentListResponse(
        items=[_deployment_summary(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_deployment_detail(session: Session, deployment_id: uuid.UUID) -> DeploymentDetailResponse:
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    now = _utcnow()
    lease = get_worker_lease(session, deployment_id)
    position = get_open_net_position(session, deployment_id)
    latest = get_latest_decision(session, deployment_id)
    summary = _deployment_summary(deployment)
    return DeploymentDetailResponse(
        **summary.model_dump(),
        compiled_config=deployment.compiled_config,
        sizing_config=deployment.sizing_config,
        risk_config=deployment.risk_config,
        open_position=PositionResponse.model_validate(position) if position is not None else None,
        worker_lease=_lease_response(lease, now=now) if lease is not None else None,
        latest_decision=_decision_response(latest) if latest is not None else None,
        unknown_order_count=count_unknown_orders(session, deployment_id=deployment_id),
    )


def apply_deployment_action(
    session: Session,
    deployment_id: uuid.UUID,
    body: DeploymentActionRequest,
) -> DeploymentActionResponse:
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if body.action == "flatten" and not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="flatten requires confirm=true",
        )
    try:
        if body.action == "start":
            deployment = start_deployment(session, deployment_id)
            message = "start accepted"
            pending = None
        elif body.action == "pause":
            deployment = pause_deployment(session, deployment_id)
            message = "pause accepted"
            pending = None
        elif body.action == "stop":
            deployment = stop_deployment(session, deployment_id)
            message = "stop accepted"
            pending = None
        else:
            deployment = set_pending_deployment_action(session, deployment_id, action="flatten")
            message = "flatten queued for worker"
            pending = deployment.pending_action
        record_audit_event(
            session,
            event_type=f"deployment_{body.action}",
            actor=body.actor,
            deployment_id=deployment_id,
            message=message,
            payload={"lifecycle": deployment.lifecycle, "pending_action": pending},
        )
        session.flush()
        return DeploymentActionResponse(
            accepted=True,
            deployment_id=deployment_id,
            lifecycle=deployment.lifecycle,
            pending_action=pending,
            message=message,
        )
    except IllegalLifecycleTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc


def list_decisions(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    limit: int,
    offset: int,
) -> DecisionListResponse:
    if get_execution_deployment(session, deployment_id) is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    items, total = list_decisions_page(
        session,
        deployment_id=deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    return DecisionListResponse(
        items=[DecisionResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_orders(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    status: Optional[str],
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    limit: int,
    offset: int,
) -> OrderListResponse:
    if get_execution_deployment(session, deployment_id) is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    items, total = list_orders_page(
        session,
        deployment_id=deployment_id,
        status=status,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    return OrderListResponse(
        items=[OrderResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_pending_reconciliation(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> PendingReconciliationListResponse:
    if get_execution_deployment(session, deployment_id) is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    items, total = list_pending_reconciliation_orders_page(
        session,
        deployment_id=deployment_id,
        limit=limit,
        offset=offset,
    )
    return PendingReconciliationListResponse(
        items=[OrderResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def resolve_order(
    session: Session,
    order_id: uuid.UUID,
    body: OrderResolutionRequest,
) -> OrderResolutionResponse:
    order = get_execution_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    deployment = get_execution_deployment(session, order.deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")

    settings = get_settings()
    point_value = Decimal(str(settings.execution_default_point_value))
    ledger = ExecutionLedger()

    fill: Optional[FillRecord] = None
    if body.outcome == "filled":
        fill = FillRecord(
            broker_mode=BrokerMode(order.broker_mode),
            external_fill_id=body.external_fill_id or f"manual:{order.id}",
            side=ExecutionSide(order.side),
            quantity=body.quantity if body.quantity is not None else order.quantity,
            price=body.price,
            fee=body.fee if body.fee is not None else Decimal("0"),
            filled_at=_as_utc(body.filled_at) if body.filled_at is not None else _utcnow(),
            metadata={"manual_resolution": True, "actor": body.actor},
        )
    try:
        outcome = resolve_order_manually(
            session,
            order=order,
            deployment=deployment,
            outcome=body.outcome,
            actor=body.actor,
            note=body.reason,
            ledger=ledger,
            point_value=point_value,
            fill=fill,
        )
        record_audit_event(
            session,
            event_type="order_reconciliation_resolved",
            actor=body.actor,
            deployment_id=deployment.id,
            message=f"order {order.id} resolved: {outcome.resolution.value}",
            payload={
                "order_id": str(order.id),
                "outcome": body.outcome,
                "resolution": outcome.resolution.value,
                "reason": body.reason,
            },
        )
        session.flush()
        session.refresh(order)
        return OrderResolutionResponse(
            accepted=True,
            order=OrderResponse.model_validate(order),
            resolution=outcome.resolution.value,
            message=outcome.message,
        )
    except OrderNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc


def list_fills(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    limit: int,
    offset: int,
) -> FillListResponse:
    if get_execution_deployment(session, deployment_id) is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    items, total = list_fills_page(
        session,
        deployment_id=deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    return FillListResponse(
        items=[FillResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_positions(
    session: Session,
    *,
    deployment_id: Optional[uuid.UUID],
    paper_account_id: Optional[uuid.UUID],
    open_only: bool,
    limit: int,
    offset: int,
) -> PositionListResponse:
    items, total = list_positions_page(
        session,
        deployment_id=deployment_id,
        paper_account_id=paper_account_id,
        open_only=open_only,
        limit=limit,
        offset=offset,
    )
    return PositionListResponse(
        items=[PositionResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_ledger(
    session: Session,
    *,
    paper_account_id: uuid.UUID,
    deployment_id: Optional[uuid.UUID],
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    limit: int,
    offset: int,
) -> LedgerEntryListResponse:
    if get_paper_account(session, paper_account_id) is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    items, total = list_ledger_entries_page(
        session,
        paper_account_id=paper_account_id,
        deployment_id=deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    return LedgerEntryListResponse(
        items=[LedgerEntryResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_risk_events(
    session: Session,
    deployment_id: uuid.UUID,
    *,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    limit: int,
    offset: int,
) -> RiskEventListResponse:
    if get_execution_deployment(session, deployment_id) is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    items, total = list_risk_events_page(
        session,
        deployment_id=deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    return RiskEventListResponse(
        items=[RiskEventResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_audit_events(
    session: Session,
    *,
    deployment_id: Optional[uuid.UUID],
    event_type: Optional[str],
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    limit: int,
    offset: int,
) -> AuditEventListResponse:
    items, total = list_audit_events_page(
        session,
        deployment_id=deployment_id,
        event_type=event_type,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    return AuditEventListResponse(
        items=[AuditEventResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_kill_switch(session: Session) -> KillSwitchResponse:
    state = get_execution_control_state(session)
    return KillSwitchResponse(
        enabled=state.kill_switch_enabled,
        reason=state.kill_switch_reason,
        updated_by=state.updated_by,
        updated_at=state.updated_at,
    )


def update_kill_switch(session: Session, body: KillSwitchUpdateRequest) -> KillSwitchUpdateResponse:
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="kill switch changes require confirm=true",
        )
    try:
        if body.enabled:
            enable_kill_switch(
                session,
                reason=body.reason,
                updated_by=body.updated_by,
            )
            message = "kill switch enabled"
            event_type = "kill_switch_enabled"
        else:
            disable_kill_switch(session, updated_by=body.updated_by)
            message = "kill switch disabled"
            event_type = "kill_switch_disabled"
        audit = record_audit_event(
            session,
            event_type=event_type,
            actor=body.updated_by,
            message=message,
            payload={"enabled": body.enabled, "reason": body.reason},
        )
        state = get_execution_control_state(session)
        session.flush()
        return KillSwitchUpdateResponse(
            accepted=True,
            kill_switch=KillSwitchResponse(
                enabled=state.kill_switch_enabled,
                reason=state.kill_switch_reason,
                updated_by=state.updated_by,
                updated_at=state.updated_at,
            ),
            audit_event_id=audit.id,
        )
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc


def get_execution_health(
    session: Session,
    *,
    mt5_connected: bool,
) -> ExecutionHealthResponse:
    settings = get_settings()
    now = _utcnow()
    try:
        control = get_execution_control_state(session)
        deployments, _ = list_deployments_page(session, limit=200, offset=0)
        leases = {lease.deployment_id: lease for lease in list_active_worker_leases(session)}
        deployment_health: list[DeploymentHealthResponse] = []
        active_leases = 0
        for deployment in deployments:
            lease = leases.get(deployment.id)
            lease_view = _lease_response(lease, now=now) if lease is not None else None
            if lease_view is not None and lease_view.is_active:
                active_leases += 1
            latest = get_latest_decision(session, deployment.id)
            deployment_health.append(
                DeploymentHealthResponse(
                    deployment_id=deployment.id,
                    lifecycle=deployment.lifecycle,
                    worker_lease=lease_view,
                    last_bar_close_time=deployment.last_bar_close_time,
                    latest_decision=_decision_response(latest) if latest is not None else None,
                    unknown_order_count=count_unknown_orders(session, deployment_id=deployment.id),
                    pending_action=deployment.pending_action,
                )
            )
        unknown_total = count_unknown_orders(session)
        if active_leases > 0:
            worker_status = "healthy"
        elif deployments:
            worker_status = "offline"
        else:
            worker_status = "offline"
        market_data_status = "online" if mt5_connected else "offline"
        api_status = "ok"
        if unknown_total > 0 or control.kill_switch_enabled:
            api_status = "degraded"
        return ExecutionHealthResponse(
            api_status=api_status,
            worker_status=worker_status,
            market_data_status=market_data_status,
            kill_switch_enabled=control.kill_switch_enabled,
            live_capability_locked=settings.execution_live_capability_locked,
            unknown_order_count=unknown_total,
            deployments=deployment_health,
            checked_at=now,
        )
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc
