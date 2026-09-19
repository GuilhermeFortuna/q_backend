"""Execution control-plane routes (no broker/evaluator/MT5 order path)."""

from __future__ import annotations

import uuid
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.dependencies import get_market_data_service
from q_backend.api.idempotency import IdempotentCommand, IdempotentRoute
from q_backend.api.schemas.execution import (
    AuditEventListResponse,
    DecisionListResponse,
    DeploymentActionRequest,
    DeploymentActionResponse,
    DeploymentChartResponse,
    DeploymentCreateRequest,
    DeploymentDetailResponse,
    DeploymentListResponse,
    ExecutionHealthResponse,
    FillListResponse,
    KillSwitchResponse,
    KillSwitchUpdateRequest,
    KillSwitchUpdateResponse,
    LedgerEntryListResponse,
    OrderListResponse,
    OrderResolutionRequest,
    OrderResolutionResponse,
    PaperAccountCreateRequest,
    PaperAccountListResponse,
    PaperAccountResponse,
    PendingReconciliationListResponse,
    PositionListResponse,
    RiskEventListResponse,
)
from q_backend.api.services import execution as execution_service
from q_backend.market_data.service import MarketDataService

router = APIRouter(tags=["execution"], route_class=IdempotentRoute)


def _session_or_503(session: Session = Depends(get_session)) -> Session:
    try:
        session.connection()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="database unavailable; command was not accepted",
        ) from exc
    return session


async def _idempotency_dependency(
    request: Request,
    session: Session = Depends(_session_or_503),
    key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
) -> IdempotentCommand:
    existing = getattr(request.state, "idempotency_command", None)
    if existing is not None:
        return existing
    raw_body = await request.body()
    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        body = {"_raw": raw_body.decode("utf-8", errors="replace")}
    command = IdempotentCommand.from_request(request, key, body)
    command.prepare(session)
    request.state.idempotency_command = command
    request.state.idempotency_session = session
    return command


@router.post("/api/v1/execution/accounts", response_model=PaperAccountResponse)
def create_execution_account(
    body: PaperAccountCreateRequest,
    session: Session = Depends(_session_or_503),
    command: IdempotentCommand = Depends(_idempotency_dependency),
):
    return command.execute(session, lambda: execution_service.create_account(session, body))


@router.get("/api/v1/execution/accounts", response_model=PaperAccountListResponse)
def list_execution_accounts(
    session: Session = Depends(_session_or_503),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_accounts(session, limit=limit, offset=offset)


@router.get(
    "/api/v1/execution/accounts/{account_id}",
    response_model=PaperAccountResponse,
)
def get_execution_account(
    account_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
):
    return execution_service.get_account(session, account_id)


@router.post("/api/v1/execution/deployments", response_model=DeploymentDetailResponse)
def create_execution_deployment(
    body: DeploymentCreateRequest,
    session: Session = Depends(_session_or_503),
    command: IdempotentCommand = Depends(_idempotency_dependency),
):
    return command.execute(session, lambda: execution_service.create_deployment(session, body))


@router.get("/api/v1/execution/deployments", response_model=DeploymentListResponse)
def list_execution_deployments(
    session: Session = Depends(_session_or_503),
    paper_account_id: Optional[uuid.UUID] = None,
    lifecycle: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_deployments(
        session,
        paper_account_id=paper_account_id,
        lifecycle=lifecycle,
        symbol=symbol,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/execution/deployments/{deployment_id}",
    response_model=DeploymentDetailResponse,
)
def get_execution_deployment(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
):
    return execution_service.get_deployment_detail(session, deployment_id)


@router.post(
    "/api/v1/execution/deployments/{deployment_id}/actions",
    response_model=DeploymentActionResponse,
)
def deployment_action(
    deployment_id: uuid.UUID,
    body: DeploymentActionRequest,
    session: Session = Depends(_session_or_503),
    command: IdempotentCommand = Depends(_idempotency_dependency),
):
    return command.execute(
        session,
        lambda: execution_service.apply_deployment_action(session, deployment_id, body),
    )


@router.get(
    "/api/v1/execution/deployments/{deployment_id}/decisions",
    response_model=DecisionListResponse,
)
def list_deployment_decisions(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_decisions(
        session,
        deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/execution/deployments/{deployment_id}/orders",
    response_model=OrderListResponse,
)
def list_deployment_orders(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    status: Optional[str] = None,
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_orders(
        session,
        deployment_id,
        status=status,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/execution/deployments/{deployment_id}/reconciliation",
    response_model=PendingReconciliationListResponse,
)
def list_deployment_pending_reconciliation(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_pending_reconciliation(
        session,
        deployment_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/v1/execution/orders/{order_id}/resolve",
    response_model=OrderResolutionResponse,
)
def resolve_execution_order(
    order_id: uuid.UUID,
    body: OrderResolutionRequest,
    session: Session = Depends(_session_or_503),
    command: IdempotentCommand = Depends(_idempotency_dependency),
):
    return command.execute(session, lambda: execution_service.resolve_order(session, order_id, body))


@router.get(
    "/api/v1/execution/deployments/{deployment_id}/fills",
    response_model=FillListResponse,
)
def list_deployment_fills(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_fills(
        session,
        deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/execution/deployments/{deployment_id}/chart",
    response_model=DeploymentChartResponse,
)
def get_deployment_chart(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    mds: MarketDataService = Depends(get_market_data_service),
    bars: int = Query(200, ge=1, le=1000),
):
    # Imported lazily to avoid a circular import at app-construction time:
    # execution_chart -> execution.strategy_build -> api.schemas.backtest -> api.__init__.
    from q_backend.api.services import execution_chart as execution_chart_service

    return execution_chart_service.get_deployment_chart(
        session,
        mds,
        deployment_id,
        bars=bars,
    )


@router.get("/api/v1/execution/positions", response_model=PositionListResponse)
def list_execution_positions(
    session: Session = Depends(_session_or_503),
    deployment_id: Optional[uuid.UUID] = None,
    paper_account_id: Optional[uuid.UUID] = None,
    open_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_positions(
        session,
        deployment_id=deployment_id,
        paper_account_id=paper_account_id,
        open_only=open_only,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/execution/accounts/{account_id}/ledger",
    response_model=LedgerEntryListResponse,
)
def list_account_ledger(
    account_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    deployment_id: Optional[uuid.UUID] = None,
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_ledger(
        session,
        paper_account_id=account_id,
        deployment_id=deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/execution/deployments/{deployment_id}/risk-events",
    response_model=RiskEventListResponse,
)
def list_deployment_risk_events(
    deployment_id: uuid.UUID,
    session: Session = Depends(_session_or_503),
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_risk_events(
        session,
        deployment_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/execution/audit-events", response_model=AuditEventListResponse)
def list_execution_audit_events(
    session: Session = Depends(_session_or_503),
    deployment_id: Optional[uuid.UUID] = None,
    event_type: Optional[str] = None,
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return execution_service.list_audit_events(
        session,
        deployment_id=deployment_id,
        event_type=event_type,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/execution/health", response_model=ExecutionHealthResponse)
def get_execution_health(
    session: Session = Depends(_session_or_503),
    mds: MarketDataService = Depends(get_market_data_service),
):
    return execution_service.get_execution_health(
        session,
        mt5_connected=mds.mt5_connected(),
    )


@router.get("/api/v1/execution/kill-switch", response_model=KillSwitchResponse)
def get_kill_switch(session: Session = Depends(_session_or_503)):
    return execution_service.get_kill_switch(session)


@router.put("/api/v1/execution/kill-switch", response_model=KillSwitchUpdateResponse)
def update_kill_switch(
    body: KillSwitchUpdateRequest,
    session: Session = Depends(_session_or_503),
    command: IdempotentCommand = Depends(_idempotency_dependency),
):
    return command.execute(session, lambda: execution_service.update_kill_switch(session, body))
