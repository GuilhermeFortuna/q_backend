"""Pydantic schemas for the execution control-plane API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

DecimalStr = Annotated[
    Decimal,
    PlainSerializer(lambda value: format(value, "f"), return_type=str),
]


class PaginatedResponse(BaseModel):
    total: int
    limit: int
    offset: int


class PaperAccountCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    initial_balance: DecimalStr
    currency: str = Field(default="BRL", min_length=3, max_length=8)
    sizing_config: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] = Field(default_factory=dict)


class PaperAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    currency: str
    initial_balance: DecimalStr
    cash_balance: DecimalStr
    sizing_config: dict[str, Any]
    risk_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PaperAccountListResponse(PaginatedResponse):
    items: list[PaperAccountResponse]


class DeploymentIdentityInput(BaseModel):
    strategy_name: str
    strategy_version: int = Field(..., ge=1)
    compiled_config: dict[str, Any]
    config_hash: str = Field(..., min_length=8, max_length=128)
    symbol: str
    timeframe: str
    sizing_config: dict[str, Any]
    risk_config: dict[str, Any] = Field(default_factory=dict)


class DeploymentCreateRequest(BaseModel):
    paper_account_id: UUID
    name: str = Field(..., min_length=1, max_length=255)
    broker_mode: Literal["paper", "mt5_live"] = "paper"
    live_activation_enabled: bool = False
    source_backtest_run_id: Optional[UUID] = None
    identity: Optional[DeploymentIdentityInput] = None


class DeploymentSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    paper_account_id: UUID
    name: str
    broker_mode: str
    lifecycle: str
    strategy_name: str
    strategy_version: int
    config_hash: str
    symbol: str
    timeframe: str
    live_activation_enabled: bool
    pending_action: Optional[str] = None
    pending_action_requested_at: Optional[datetime] = None
    last_bar_close_time: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class DeploymentDetailResponse(DeploymentSummaryResponse):
    compiled_config: dict[str, Any]
    sizing_config: dict[str, Any]
    risk_config: dict[str, Any]
    open_position: Optional["PositionResponse"] = None
    worker_lease: Optional["WorkerLeaseResponse"] = None
    latest_decision: Optional["DecisionResponse"] = None
    unknown_order_count: int = 0


class DeploymentListResponse(PaginatedResponse):
    items: list[DeploymentSummaryResponse]


class DeploymentActionRequest(BaseModel):
    action: Literal["start", "pause", "stop", "flatten"]
    confirm: bool = False
    actor: Optional[str] = None


class DeploymentActionResponse(BaseModel):
    accepted: bool
    deployment_id: UUID
    lifecycle: str
    pending_action: Optional[str] = None
    message: str


class DecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    bar_close_time: datetime
    strategy_name: str
    strategy_version: int
    config_hash: str
    symbol: str
    timeframe: str
    signal_action: str
    outcome: str
    requested_quantity: Optional[DecimalStr] = None
    reason: Optional[str] = None
    created_at: datetime


class DecisionListResponse(PaginatedResponse):
    items: list[DecisionResponse]


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    decision_id: Optional[UUID] = None
    broker_mode: str
    side: str
    order_type: str
    quantity: DecimalStr
    status: str
    reconciliation_state: str
    rejection_reason: Optional[str] = None
    intent_committed_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    reconciliation_attempted_at: Optional[datetime] = None
    reconciliation_error: Optional[str] = None
    reconciled_at: Optional[datetime] = None
    reconciled_by: Optional[str] = None
    reconciliation_detail: Optional[str] = None
    created_at: datetime


class OrderListResponse(PaginatedResponse):
    items: list[OrderResponse]


class PendingReconciliationListResponse(PaginatedResponse):
    items: list[OrderResponse]


class OrderResolutionRequest(BaseModel):
    """Operator assertion resolving an unknown order. Outcome is mandatory."""

    outcome: Literal["filled", "not_filled"]
    actor: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1)
    # Required when outcome == "filled".
    price: Optional[DecimalStr] = None
    quantity: Optional[DecimalStr] = None
    fee: Optional[DecimalStr] = None
    filled_at: Optional[datetime] = None
    external_fill_id: Optional[str] = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _require_fill_details(self) -> "OrderResolutionRequest":
        if self.outcome == "filled" and self.price is None:
            raise ValueError("filled resolution requires a price")
        return self


class OrderResolutionResponse(BaseModel):
    accepted: bool
    order: OrderResponse
    resolution: str
    message: str


class FillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    order_id: UUID
    broker_mode: str
    external_fill_id: str
    side: str
    quantity: DecimalStr
    price: DecimalStr
    fee: DecimalStr
    slippage: DecimalStr
    filled_at: datetime
    created_at: datetime


class FillListResponse(PaginatedResponse):
    items: list[FillResponse]


class PositionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    side: str
    quantity: DecimalStr
    average_entry_price: Optional[DecimalStr] = None
    is_open: bool
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    updated_at: datetime


class PositionListResponse(PaginatedResponse):
    items: list[PositionResponse]


class LedgerEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    paper_account_id: UUID
    deployment_id: UUID
    fill_id: Optional[UUID] = None
    entry_type: str
    amount: DecimalStr
    balance_after: DecimalStr
    description: Optional[str] = None
    created_at: datetime


class LedgerEntryListResponse(PaginatedResponse):
    items: list[LedgerEntryResponse]


class RiskEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    decision_id: Optional[UUID] = None
    order_id: Optional[UUID] = None
    rejection_code: str
    message: str
    context: dict[str, Any]
    created_at: datetime


class RiskEventListResponse(PaginatedResponse):
    items: list[RiskEventResponse]


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    actor: Optional[str] = None
    deployment_id: Optional[UUID] = None
    message: str
    payload: dict[str, Any]
    created_at: datetime


class AuditEventListResponse(PaginatedResponse):
    items: list[AuditEventResponse]


class WorkerLeaseResponse(BaseModel):
    worker_id: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    is_active: bool


class DeploymentHealthResponse(BaseModel):
    deployment_id: UUID
    lifecycle: str
    worker_lease: Optional[WorkerLeaseResponse] = None
    last_bar_close_time: Optional[datetime] = None
    latest_decision: Optional[DecisionResponse] = None
    unknown_order_count: int = 0
    pending_action: Optional[str] = None


class ExecutionHealthResponse(BaseModel):
    api_status: Literal["ok", "degraded", "unavailable"]
    worker_status: Literal["healthy", "stale", "offline"]
    market_data_status: Literal["online", "offline", "stale"]
    kill_switch_enabled: bool
    live_capability_locked: bool
    unknown_order_count: int
    deployments: list[DeploymentHealthResponse]
    checked_at: datetime


class DeploymentChartBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class DeploymentChartIndicator(BaseModel):
    key: str
    label: str
    pane: Literal["price", "oscillator"]
    color: Optional[str] = None
    values: list[Optional[float]]


class DeploymentChartResponse(BaseModel):
    symbol: str
    timeframe: str
    window_bound_bars: int
    last_bar_close_time: Optional[datetime] = None
    next_bar_close_time: Optional[datetime] = None
    bars: list[DeploymentChartBar]
    indicators: list[DeploymentChartIndicator]


class KillSwitchResponse(BaseModel):
    enabled: bool
    reason: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class KillSwitchUpdateRequest(BaseModel):
    enabled: bool
    confirm: bool = False
    reason: Optional[str] = None
    updated_by: Optional[str] = None


class KillSwitchUpdateResponse(BaseModel):
    accepted: bool
    kill_switch: KillSwitchResponse
    audit_event_id: UUID


DeploymentDetailResponse.model_rebuild()
