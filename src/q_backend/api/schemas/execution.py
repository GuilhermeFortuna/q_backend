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


class PaperCostConfigInput(BaseModel):
    point_value: DecimalStr = Decimal("1")
    slippage_points: DecimalStr = Decimal("0")
    cost_per_contract: DecimalStr = Decimal("0")
    cost_bps: DecimalStr = Decimal("0")


class PaperCostConfigResponse(BaseModel):
    point_value: DecimalStr
    slippage_points: DecimalStr
    cost_per_contract: DecimalStr
    cost_bps: DecimalStr


class CatalogDeploymentInput(BaseModel):
    """Server-validated catalog strategy selection for a new paper deployment."""

    strategy_name: str = Field(..., min_length=1)
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    exit_params: dict[str, Any] = Field(default_factory=dict)
    symbol: str = Field(..., min_length=1)
    timeframe: str = Field(..., min_length=1)
    sizing_config: dict[str, Any]
    risk_config: dict[str, Any] = Field(default_factory=dict)
    paper_cost_config: PaperCostConfigInput = Field(default_factory=PaperCostConfigInput)


class DeploymentCreateRequest(BaseModel):
    paper_account_id: UUID
    name: str = Field(..., min_length=1, max_length=255)
    broker_mode: Literal["paper", "mt5_live"] = "paper"
    live_activation_enabled: bool = False
    source_backtest_run_id: Optional[UUID] = None
    identity: Optional[DeploymentIdentityInput] = None
    catalog: Optional[CatalogDeploymentInput] = None

    @model_validator(mode="after")
    def _one_source(self) -> "DeploymentCreateRequest":
        provided = [
            self.source_backtest_run_id is not None,
            self.identity is not None,
            self.catalog is not None,
        ]
        if sum(provided) != 1:
            raise ValueError("exactly one of source_backtest_run_id, identity, or catalog is required")
        return self


class DeploymentConfigurationUpdateRequest(BaseModel):
    """Complete replacement of a draft/paused-flat deployment's mutable configuration."""

    expected_revision: int = Field(..., ge=1)
    actor: str = Field(..., min_length=1, max_length=128)
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    exit_params: dict[str, Any] = Field(default_factory=dict)
    sizing_config: dict[str, Any]
    risk_config: dict[str, Any] = Field(default_factory=dict)
    paper_cost_config: PaperCostConfigInput = Field(default_factory=PaperCostConfigInput)


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
    config_revision: int
    source_kind: str
    source_strategy_name: Optional[str] = None
    symbol: str
    timeframe: str
    live_activation_enabled: bool
    pending_action: Optional[str] = None
    pending_action_requested_at: Optional[datetime] = None
    last_bar_close_time: Optional[datetime] = None
    activation_cutoff_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class DeploymentDetailResponse(DeploymentSummaryResponse):
    compiled_config: dict[str, Any]
    sizing_config: dict[str, Any]
    risk_config: dict[str, Any]
    paper_cost_config: dict[str, Any]
    open_position: Optional["PositionResponse"] = None
    worker_lease: Optional["WorkerLeaseResponse"] = None
    latest_decision: Optional["DecisionResponse"] = None
    unknown_order_count: int = 0


class DeploymentListResponse(PaginatedResponse):
    items: list[DeploymentSummaryResponse]


class DeploymentActionRequest(BaseModel):
    action: Literal["start", "pause", "stop", "flatten", "archive"]
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
    config_revision: int
    paper_cost_config: dict[str, Any]
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
    config_revision: Optional[int] = None
    broker_mode: str
    side: str
    order_type: str
    quantity: DecimalStr
    status: str
    reconciliation_state: str
    rejection_reason: Optional[str] = None
    intent_committed_at: Optional[datetime] = None
    dispatch_attempted_at: Optional[datetime] = None
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
    decision_id: Optional[UUID] = None
    config_revision: Optional[int] = None
    broker_mode: str
    external_fill_id: str
    side: str
    quantity: DecimalStr
    price: DecimalStr
    fee: DecimalStr
    slippage: DecimalStr
    quote_bid: Optional[DecimalStr] = None
    quote_ask: Optional[DecimalStr] = None
    quote_timestamp: Optional[datetime] = None
    filled_at: datetime
    created_at: datetime


class FillListResponse(PaginatedResponse):
    items: list[FillResponse]


class DeploymentPerformanceResponse(BaseModel):
    deployment_id: UUID
    realized_pnl: DecimalStr
    unrealized_pnl: Optional[DecimalStr] = None
    fees: DecimalStr
    net_pnl: Optional[DecimalStr] = None
    closed_trade_count: int
    win_count: int
    win_rate: Optional[DecimalStr] = None
    mark_status: str
    marked_at: Optional[datetime] = None
    config_revision: int


class PerformanceMarkResponse(BaseModel):
    bar_close_time: datetime
    config_revision: int
    mark_status: str
    quote_bid: Optional[DecimalStr] = None
    quote_ask: Optional[DecimalStr] = None
    quote_timestamp: Optional[datetime] = None
    quote_source: Optional[str] = None
    mark_price: Optional[DecimalStr] = None
    realized_pnl: DecimalStr
    fees: DecimalStr
    unrealized_pnl: Optional[DecimalStr] = None
    equity_delta: Optional[DecimalStr] = None


class PerformanceMarkListResponse(PaginatedResponse):
    items: list[PerformanceMarkResponse]


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
    deployment_id: Optional[UUID] = None
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


class EdgeStatusResponse(BaseModel):
    reachable: bool
    mt5_connected: Optional[bool] = None
    terminal_build: Optional[int] = None
    checked_at: Optional[datetime] = None


class ExecutionHealthResponse(BaseModel):
    api_status: Literal["ok", "degraded", "unavailable"]
    worker_status: Literal["healthy", "stale", "offline"]
    worker_heartbeat_age_s: Optional[float] = None
    worker_started_at: Optional[datetime] = None
    edge: EdgeStatusResponse = EdgeStatusResponse(reachable=False)
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
    line_style: Optional[Literal["solid", "dashed", "dotted"]] = None
    line_width: Optional[float] = Field(default=None, gt=0)
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
