# GENERATED FILE - DO NOT EDIT. Source schemas: schema/stream/control/cursor-expired.schema.json, schema/stream/control/epoch-changed.schema.json, schema/stream/control/lagging.schema.json, schema/stream/control/rejected.schema.json, schema/stream/control/subscribe.schema.json, schema/stream/control/subscribed.schema.json, schema/stream/envelope.schema.json, schema/stream/payloads/execution-common.schema.json, schema/stream/payloads/execution-decision.schema.json, schema/stream/payloads/execution-deployment.schema.json, schema/stream/payloads/execution-fill.schema.json, schema/stream/payloads/execution-ledger.schema.json, schema/stream/payloads/execution-order.schema.json, schema/stream/payloads/execution-risk.schema.json, schema/stream/payloads/job-progress.schema.json, schema/stream/payloads/job-terminal.schema.json, schema/stream/replay/execution-snapshot.schema.json, schema/stream/replay/history-expired.schema.json, schema/stream/replay/history-page.schema.json, schema/stream/replay/latest.schema.json, schema/stream/replay/watermark.schema.json
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

BrokerMode = Literal["paper", "mt5_live"]


@dataclass(frozen=True)
class CursorExpiredFrame:
    topic: str
    type: Literal["cursor_expired"]
    cursor: str | None = None


Decimal = str


DecisionOutcome = Literal[
    "hold",
    "signal",
    "risk_rejected",
    "order_intent",
    "order_filled",
    "order_rejected",
    "order_unknown",
    "error",
]


DeploymentLifecycle = Literal["draft", "running", "paused", "stopped", "error"]


@dataclass(frozen=True)
class EpochChangedFrame:
    new_epoch: str
    topic: str
    type: Literal["epoch_changed"]
    previous_epoch: str | None = None


@dataclass(frozen=True)
class ExecutionAccount:
    cash_balance: Decimal
    currency: str
    id: UUID
    initial_balance: Decimal
    name: str
    realized_pnl: Decimal
    updated_at: str
    created_at: str | None = None
    risk_config: dict[str, Any] | None = None
    sizing_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionCommon:
    pass


@dataclass(frozen=True)
class ExecutionControl:
    kill_switch_enabled: bool
    updated_at: str
    kill_switch_reason: str | None = None
    updated_by: str | None = None


@dataclass(frozen=True)
class ExecutionDecisionState:
    account_id: UUID
    bar_close_time: str
    config_hash: str
    deployment_id: UUID
    entity: Literal["decision"]
    id: UUID
    outcome: DecisionOutcome
    signal_action: SignalAction
    strategy_name: str
    strategy_version: int
    symbol: str
    timeframe: str
    updated_at: str
    compiled_config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    created_at: str | None = None
    reason: str | None = None
    requested_quantity: Decimal | None = None
    risk_config: dict[str, Any] | None = None
    sizing_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionDeploymentState:
    account_id: UUID
    broker_mode: BrokerMode
    config_hash: str
    deployment_id: UUID
    entity: Literal["deployment"]
    id: UUID
    lifecycle: DeploymentLifecycle
    live_activation_enabled: bool
    name: str
    strategy_name: str
    strategy_version: int
    symbol: str
    timeframe: str
    updated_at: str
    compiled_config: dict[str, Any] | None = None
    created_at: str | None = None
    last_bar_close_time: str | None = None
    paper_account_id: UUID | None = None
    pending_action: str | None = None
    pending_action_requested_at: str | None = None
    risk_config: dict[str, Any] | None = None
    sizing_config: dict[str, Any] | None = None
    started_at: str | None = None
    stopped_at: str | None = None


@dataclass(frozen=True)
class ExecutionFillEvent:
    account_id: UUID
    broker_mode: BrokerMode
    deployment_id: UUID
    entity: Literal["fill"]
    external_fill_id: str
    fee: Decimal
    filled_at: str
    id: UUID
    order_id: UUID
    position_after: ExecutionPosition
    price: Decimal
    quantity: Decimal
    side: ExecutionSide
    slippage: Decimal
    updated_at: str
    created_at: str | None = None
    details: dict[str, Any] | None = None
    quote_ask: Decimal | None = None
    quote_bid: Decimal | None = None
    quote_timestamp: str | None = None


@dataclass(frozen=True)
class ExecutionLedgerEvent:
    account_after: ExecutionAccount
    account_id: UUID
    amount: Decimal
    balance_after: Decimal
    entity: Literal["ledger_entry"]
    entry_type: LedgerEntryType
    id: UUID
    updated_at: str
    created_at: str | None = None
    deployment_id: UUID | None = None
    description: str | None = None
    fill_id: UUID | None = None
    paper_account_id: UUID | None = None


@dataclass(frozen=True)
class ExecutionOrderState:
    account_id: UUID
    broker_mode: BrokerMode
    deployment_id: UUID
    entity: Literal["order"]
    id: UUID
    intent_id: UUID
    order_type: ExecutionOrderType
    quantity: Decimal
    reconciliation_state: ReconciliationState
    side: ExecutionSide
    status: ExecutionOrderStatus
    updated_at: str
    completed_at: str | None = None
    created_at: str | None = None
    decision_id: UUID | None = None
    details: dict[str, Any] | None = None
    external_order_id: str | None = None
    intent_committed_at: str | None = None
    reconciled_at: str | None = None
    reconciled_by: str | None = None
    reconciliation_attempted_at: str | None = None
    reconciliation_detail: str | None = None
    reconciliation_error: str | None = None
    rejection_reason: str | None = None
    submitted_at: str | None = None


ExecutionOrderStatus = Literal[
    "intent", "submitted", "filled", "rejected", "unknown", "cancelled"
]


ExecutionOrderType = Literal["market"]


@dataclass(frozen=True)
class ExecutionPosition:
    deployment_id: UUID
    id: UUID
    is_open: bool
    quantity: Decimal
    side: PositionSide
    updated_at: str
    average_entry_price: Decimal | None = None
    closed_at: str | None = None
    opened_at: str | None = None


ExecutionRiskEvent = dict[str, Any] | dict[str, Any]


ExecutionSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class ExecutionSnapshot:
    accounts: list[ExecutionAccount]
    control: ExecutionControl
    deployments: list[ExecutionDeploymentState]
    limits: dict[str, Any]
    orders: list[ExecutionOrderState]
    positions: list[ExecutionPosition]
    recent: dict[str, Any]
    watermark: dict[str, Any]


@dataclass(frozen=True)
class HistoryExpiredResponse:
    requested_from_seq: int
    topic: str
    oldest_available_seq: int | None = None


@dataclass(frozen=True)
class HistoryPageResponse:
    entries: list[StreamEnvelope]
    epoch: str
    next_seq: int | None
    topic: str


@dataclass(frozen=True)
class JobProgressPayload:
    job_id: str
    kind: Literal[
        "alpha_research",
        "backtest",
        "discovery_ab",
        "encoder_ablation",
        "neural_training",
        "optimization",
        "storage_ingest",
        "strategy_search",
        "walkforward",
    ]
    progress: float | None
    status: Literal["queued", "running"]
    message: str | None = None


@dataclass(frozen=True)
class JobTerminalPayload:
    finished_at: str
    job_id: str
    kind: Literal[
        "alpha_research",
        "backtest",
        "discovery_ab",
        "encoder_ablation",
        "neural_training",
        "optimization",
        "storage_ingest",
        "strategy_search",
        "walkforward",
    ]
    status: Literal["completed", "failed", "cancelled"]
    error: str | None = None


@dataclass(frozen=True)
class LaggingFrame:
    from_seq: int
    topic: str
    type: Literal["lagging"]


@dataclass(frozen=True)
class LatestValuesResponse:
    entries: dict[str, Any]
    topic: str


LedgerEntryType = Literal[
    "initial_balance", "fill_cash", "realized_pnl", "fee", "adjustment"
]


PositionSide = Literal["long", "short", "flat"]


ReconciliationState = Literal["not_applicable", "pending", "reconciled", "ambiguous"]


@dataclass(frozen=True)
class RejectedFrame:
    reason: Literal[
        "unknown_topic",
        "unsupported_schema_major",
        "stream_unavailable",
        "invalid_frame",
    ]
    type: Literal["rejected"]
    detail: str | None = None
    topic: str | None = None


RiskRejectionCode = Literal[
    "kill_switch",
    "lifecycle",
    "lease_lost",
    "stale_bar",
    "stale_quote",
    "symbol_unavailable",
    "invalid_quantity",
    "notional_limit",
    "one_position_violation",
    "insufficient_equity",
    "daily_loss_limit",
    "database_unavailable",
    "broker_unavailable",
    "unknown_prior_order",
]


SignalAction = Literal["buy", "sell", "hold", "close"]


@dataclass(frozen=True)
class SnapshotWatermark:
    pass


@dataclass(frozen=True)
class StreamEnvelope:
    epoch: str
    origin_ts: str
    payload: str | dict[str, Any]
    payload_kind: Literal["arrow_ipc", "control"]
    payload_schema: str
    producer_id: str
    schema_major: int
    seq: int
    topic: Literal[
        "bars.completed",
        "bars.forming",
        "decisions",
        "deployments",
        "fills",
        "jobs.progress",
        "jobs.terminal",
        "ledger",
        "orders",
        "quotes",
        "risk",
    ]
    key: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubscribeFrame:
    topics: list[str]
    cursors: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubscribedFrame:
    topics: dict[str, Any]
    type: Literal["subscribed"]


UUID = str
