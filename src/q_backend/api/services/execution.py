"""Control-plane service for paper execution (no broker/evaluator/MT5 calls)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from q_backend.api.schemas.execution import (
    AuditEventListResponse,
    AuditEventResponse,
    DecisionListResponse,
    DecisionResponse,
    DeploymentActionRequest,
    DeploymentActionResponse,
    DeploymentConfigurationUpdateRequest,
    DeploymentCreateRequest,
    DeploymentDetailResponse,
    DeploymentPerformanceResponse,
    DeploymentHealthResponse,
    DeploymentListResponse,
    DeploymentSummaryResponse,
    EdgeStatusResponse,
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
    PerformanceMarkListResponse,
    PerformanceMarkResponse,
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
    resolve_catalog_configuration,
    validate_broker_mode,
    validate_risk_config,
    validate_strategy_identity,
)
from q_backend.storage.db.execution_models import ExecutionDeployment, ExecutionLedgerEntry, ExecutionPaperMark
from q_backend.storage.db.execution_repositories import (
    ConfigurationEditRejected,
    StaleRevisionError,
    apply_deployment_configuration_edit,
    clear_pending_deployment_action,
    count_unknown_orders,
    create_execution_deployment,
    create_paper_account,
    get_execution_control_state,
    get_worker_heartbeat,
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


def _order_response(order) -> OrderResponse:
    response = OrderResponse.model_validate(order)
    revision = order.decision.config_revision if order.decision is not None else None
    return response.model_copy(update={"config_revision": revision})


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


def create_deployment(
    session: Session,
    body: DeploymentCreateRequest,
    *,
    market_data_service: Optional[Any] = None,
) -> DeploymentDetailResponse:
    settings = get_settings()
    if settings.execution_paper_only and (body.broker_mode == "mt5_live" or body.live_activation_enabled):
        raise HTTPException(status_code=403, detail="execution profile is paper-only; live_locked")
    account = get_paper_account(session, body.paper_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    try:
        broker_mode = validate_broker_mode(
            body.broker_mode,
            live_capability_locked=settings.execution_live_capability_locked,
            live_activation_enabled=body.live_activation_enabled,
        )
        source_kind = "explicit"
        source_strategy_name: Optional[str] = None
        paper_cost_config: dict[str, Any] = {}
        if body.source_backtest_run_id is not None:
            identity = identity_from_saved_backtest(
                session,
                source_backtest_run_id=body.source_backtest_run_id,
                risk_config=body.identity.risk_config if body.identity else None,
                sizing_config=body.identity.sizing_config if body.identity else None,
                market_data_service=market_data_service,
            )
            source_kind = "saved_backtest"
        elif body.catalog is not None:
            resolved = resolve_catalog_configuration(
                strategy_name=body.catalog.strategy_name,
                strategy_params=body.catalog.strategy_params,
                exit_params=body.catalog.exit_params,
                symbol=body.catalog.symbol,
                timeframe=body.catalog.timeframe,
                sizing_config=body.catalog.sizing_config,
                risk_config=body.catalog.risk_config,
                paper_cost_config=body.catalog.paper_cost_config.model_dump(),
                market_data_service=market_data_service,
            )
            identity = resolved.identity
            source_kind = resolved.source_kind
            source_strategy_name = resolved.source_strategy_name
            paper_cost_config = resolved.paper_cost_config
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
                market_data_service=market_data_service,
            )
        else:
            raise ExecutionValidationError("one of source_backtest_run_id, identity, or catalog is required")
        deployment = create_execution_deployment(
            session,
            paper_account_id=body.paper_account_id,
            name=body.name,
            identity=identity,
            broker_mode=broker_mode,
            lifecycle=DeploymentLifecycle.DRAFT,
            live_activation_enabled=body.live_activation_enabled,
            source_kind=source_kind,
            source_strategy_name=source_strategy_name,
            paper_cost_config=paper_cost_config,
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
        paper_cost_config=deployment.paper_cost_config,
        open_position=PositionResponse.model_validate(position) if position is not None else None,
        worker_lease=_lease_response(lease, now=now) if lease is not None else None,
        latest_decision=_decision_response(latest) if latest is not None else None,
        unknown_order_count=count_unknown_orders(session, deployment_id=deployment_id),
    )


def deployment_performance(session: Session, deployment_id: uuid.UUID) -> DeploymentPerformanceResponse:
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    entries = list(
        session.execute(
            select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.deployment_id == deployment_id)
        ).scalars()
    )
    realized = sum((e.amount for e in entries if e.entry_type == "realized_pnl"), Decimal("0"))
    fees = -sum((e.amount for e in entries if e.entry_type == "fee"), Decimal("0"))
    per_fill: dict[uuid.UUID, list[Decimal]] = {}
    for entry in entries:
        if entry.fill_id is not None:
            values = per_fill.setdefault(entry.fill_id, [Decimal("0"), Decimal("0")])
            if entry.entry_type == "realized_pnl":
                values[0] += entry.amount
            elif entry.entry_type == "fee":
                values[1] += entry.amount
    closed = [values for values in per_fill.values() if values[0] != 0]
    wins = sum(1 for pnl, fee in closed if pnl + fee > 0)
    latest = session.execute(
        select(ExecutionPaperMark)
        .where(ExecutionPaperMark.deployment_id == deployment_id)
        .order_by(ExecutionPaperMark.bar_close_time.desc())
        .limit(1)
    ).scalar_one_or_none()
    unrealized = latest.unrealized_pnl if latest and latest.mark_status == "marked" else None
    net = realized - fees + unrealized if unrealized is not None else None
    return DeploymentPerformanceResponse(
        deployment_id=deployment_id,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        fees=fees,
        net_pnl=net,
        closed_trade_count=len(closed),
        win_count=wins,
        win_rate=Decimal(wins) / Decimal(len(closed)) if closed else None,
        mark_status=latest.mark_status if latest else "unavailable",
        marked_at=latest.bar_close_time if latest else None,
        config_revision=deployment.config_revision,
    )


def deployment_performance_marks(
    session: Session, deployment_id: uuid.UUID, *, limit: int, offset: int
) -> PerformanceMarkListResponse:
    if get_execution_deployment(session, deployment_id) is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    stmt = select(ExecutionPaperMark).where(ExecutionPaperMark.deployment_id == deployment_id)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(ExecutionPaperMark.bar_close_time.desc()).limit(limit).offset(offset)
    ).scalars()
    items = []
    for row in rows:
        delta = row.realized_pnl - row.fees + row.unrealized_pnl if row.unrealized_pnl is not None else None
        items.append(
            PerformanceMarkResponse(
                bar_close_time=row.bar_close_time,
                config_revision=row.config_revision,
                mark_status=row.mark_status,
                quote_bid=row.quote_bid,
                quote_ask=row.quote_ask,
                quote_timestamp=row.quote_timestamp,
                quote_source=row.quote_source,
                mark_price=row.mark_price,
                realized_pnl=row.realized_pnl,
                fees=row.fees,
                unrealized_pnl=row.unrealized_pnl,
                equity_delta=delta,
            )
        )
    return PerformanceMarkListResponse(items=items, total=total, limit=limit, offset=offset)


def get_strategy_catalog():
    from q_backend.execution.catalog import CatalogResponse, build_catalog

    return CatalogResponse(strategies=build_catalog())


def patch_deployment_configuration(
    session: Session,
    deployment_id: uuid.UUID,
    body: DeploymentConfigurationUpdateRequest,
    *,
    market_data_service: Optional[Any] = None,
) -> DeploymentDetailResponse:
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    catalog_name = deployment.source_strategy_name or deployment.strategy_name
    if deployment.source_kind not in {"builtin", "custom"}:
        raise HTTPException(
            status_code=400,
            detail="configuration edits require a catalog-created deployment",
        )
    try:
        resolved = resolve_catalog_configuration(
            strategy_name=catalog_name,
            strategy_params=body.strategy_params,
            exit_params=body.exit_params,
            symbol=deployment.symbol,
            timeframe=deployment.timeframe,
            sizing_config=body.sizing_config,
            risk_config=body.risk_config,
            paper_cost_config=body.paper_cost_config.model_dump(),
            market_data_service=market_data_service,
        )
        deployment = apply_deployment_configuration_edit(
            session,
            deployment_id,
            expected_revision=body.expected_revision,
            identity=resolved.identity,
            source_kind=resolved.source_kind,
            source_strategy_name=resolved.source_strategy_name,
            paper_cost_config=resolved.paper_cost_config,
            actor=body.actor,
        )
        record_audit_event(
            session,
            event_type="deployment_configuration_revised",
            actor=body.actor,
            deployment_id=deployment_id,
            message=f"configuration revised to revision {deployment.config_revision}",
            payload={"config_revision": deployment.config_revision},
        )
        session.flush()
        return get_deployment_detail(session, deployment_id)
    except ExecutionValidationError as exc:
        raise _http_from_validation(exc) from exc
    except StaleRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigurationEditRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc


def apply_deployment_action(
    session: Session,
    deployment_id: uuid.UUID,
    body: DeploymentActionRequest,
) -> DeploymentActionResponse:
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    settings = get_settings()
    if settings.execution_paper_only and body.action == "start" and deployment.broker_mode == "mt5_live":
        raise HTTPException(status_code=403, detail="execution profile is paper-only; live_locked")
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
        items=[_order_response(item) for item in items],
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
        items=[_order_response(item) for item in items],
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
    configured_point_value = (deployment.paper_cost_config or {}).get("point_value")
    if configured_point_value is None and deployment.config_revision > 1:
        raise HTTPException(status_code=409, detail="deployment revision has no paper cost config")
    point_value = Decimal(str(configured_point_value or settings.execution_default_point_value))
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
            order=_order_response(order),
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
        items=[
            FillResponse.model_validate(item).model_copy(
                update={
                    "decision_id": item.order.decision_id if item.order is not None else None,
                    "config_revision": (
                        item.order.decision.config_revision
                        if item.order is not None and item.order.decision is not None
                        else None
                    ),
                }
            )
            for item in items
        ],
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
        heartbeat = get_worker_heartbeat(session)
        worker_status = "offline"
        heartbeat_age_s: Optional[float] = None
        worker_started_at = None
        edge_status = EdgeStatusResponse(reachable=False)
        if heartbeat is not None:
            beat_at = _as_utc(heartbeat.heartbeat_at)
            heartbeat_age_s = max((now - beat_at).total_seconds(), 0.0)
            worker_started_at = _as_utc(heartbeat.started_at)
            if heartbeat.stopped_at is None:
                stale = heartbeat_age_s > settings.execution_heartbeat_stale_after_s
                worker_status = "stale" if stale else "healthy"
            edge_status = EdgeStatusResponse(
                reachable=heartbeat.edge_reachable,
                mt5_connected=heartbeat.edge_mt5_connected,
                terminal_build=heartbeat.edge_terminal_build,
                checked_at=_as_utc(heartbeat.edge_checked_at),
            )
        market_data_status = "online" if mt5_connected else "offline"
        api_status = "ok"
        if unknown_total > 0 or control.kill_switch_enabled:
            api_status = "degraded"
        return ExecutionHealthResponse(
            api_status=api_status,
            worker_status=worker_status,
            worker_heartbeat_age_s=heartbeat_age_s,
            worker_started_at=worker_started_at,
            edge=edge_status,
            market_data_status=market_data_status,
            kill_switch_enabled=control.kill_switch_enabled,
            live_capability_locked=settings.execution_live_capability_locked,
            unknown_order_count=unknown_total,
            deployments=deployment_health,
            checked_at=now,
        )
    except SQLAlchemyError as exc:
        raise _http_from_db(exc) from exc
