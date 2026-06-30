"""SQLAlchemy models for forward execution persistence (paper and live)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from q_backend.storage.db.base import (
    Base,
    MoneyNumeric,
    PortableJSON,
    PriceNumeric,
    QuantityNumeric,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class PaperAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_accounts"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="BRL")
    initial_balance: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    sizing_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    risk_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )

    deployments: Mapped[list["ExecutionDeployment"]] = relationship(
        back_populates="paper_account",
        cascade="all, delete-orphan",
    )
    ledger_entries: Mapped[list["ExecutionLedgerEntry"]] = relationship(
        back_populates="paper_account",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_paper_accounts_name", "name"),)


class ExecutionDeployment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_deployments"

    paper_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    broker_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(255), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    compiled_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    config_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    sizing_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    risk_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    live_activation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_bar_close_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_action: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    pending_action_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    paper_account: Mapped["PaperAccount"] = relationship(back_populates="deployments")
    decisions: Mapped[list["ExecutionDecision"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    orders: Mapped[list["ExecutionOrder"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    fills: Mapped[list["ExecutionFill"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    net_positions: Mapped[list["ExecutionNetPosition"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    ledger_entries: Mapped[list["ExecutionLedgerEntry"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    risk_events: Mapped[list["ExecutionRiskEvent"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    worker_leases: Mapped[list["ExecutionWorkerLease"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_execution_deployments_paper_account", "paper_account_id"),
        Index("ix_execution_deployments_lifecycle", "lifecycle"),
        Index("ix_execution_deployments_symbol_tf", "symbol", "timeframe"),
    )


class ExecutionDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_decisions"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    bar_close_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    strategy_name: Mapped[str] = mapped_column(String(255), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    compiled_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    config_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    sizing_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    risk_config: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    signal_action: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_quantity: Mapped[Optional[Decimal]] = mapped_column(
        QuantityNumeric, nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )

    deployment: Mapped["ExecutionDeployment"] = relationship(back_populates="decisions")
    orders: Mapped[list["ExecutionOrder"]] = relationship(back_populates="decision")
    risk_events: Mapped[list["ExecutionRiskEvent"]] = relationship(
        back_populates="decision",
    )

    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "bar_close_time",
            name="uq_execution_decisions_deployment_bar_close",
        ),
        Index("ix_execution_decisions_deployment", "deployment_id"),
    )


class ExecutionOrder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_orders"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("execution_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    broker_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QuantityNumeric, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reconciliation_state: Mapped[str] = mapped_column(String(32), nullable=False)
    external_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intent_committed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )

    deployment: Mapped["ExecutionDeployment"] = relationship(back_populates="orders")
    decision: Mapped[Optional["ExecutionDecision"]] = relationship(
        back_populates="orders",
    )
    fills: Mapped[list["ExecutionFill"]] = relationship(back_populates="order")
    risk_events: Mapped[list["ExecutionRiskEvent"]] = relationship(
        back_populates="order",
    )

    __table_args__ = (
        Index("ix_execution_orders_deployment", "deployment_id"),
        Index("ix_execution_orders_status", "status"),
    )


class ExecutionFill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_fills"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    broker_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    external_fill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QuantityNumeric, nullable=False)
    price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False, default=Decimal("0"))
    slippage: Mapped[Decimal] = mapped_column(
        MoneyNumeric, nullable=False, default=Decimal("0")
    )
    quote_bid: Mapped[Optional[Decimal]] = mapped_column(PriceNumeric, nullable=True)
    quote_ask: Mapped[Optional[Decimal]] = mapped_column(PriceNumeric, nullable=True)
    quote_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )

    deployment: Mapped["ExecutionDeployment"] = relationship(back_populates="fills")
    order: Mapped["ExecutionOrder"] = relationship(back_populates="fills")
    ledger_entries: Mapped[list["ExecutionLedgerEntry"]] = relationship(
        back_populates="fill",
    )

    __table_args__ = (
        UniqueConstraint(
            "broker_mode",
            "external_fill_id",
            name="uq_execution_fills_broker_external_id",
        ),
        Index("ix_execution_fills_deployment", "deployment_id"),
        Index("ix_execution_fills_order", "order_id"),
    )


class ExecutionNetPosition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_net_positions"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QuantityNumeric, nullable=False)
    average_entry_price: Mapped[Optional[Decimal]] = mapped_column(
        PriceNumeric, nullable=True
    )
    is_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    deployment: Mapped["ExecutionDeployment"] = relationship(
        back_populates="net_positions",
    )

    __table_args__ = (
        Index(
            "uq_execution_net_positions_one_open",
            "deployment_id",
            unique=True,
            sqlite_where=text("is_open = 1"),
            postgresql_where=text("is_open = true"),
        ),
        Index("ix_execution_net_positions_deployment", "deployment_id"),
    )


class ExecutionLedgerEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_ledger_entries"

    paper_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    fill_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("execution_fills.id", ondelete="SET NULL"),
        nullable=True,
    )
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    paper_account: Mapped["PaperAccount"] = relationship(back_populates="ledger_entries")
    deployment: Mapped["ExecutionDeployment"] = relationship(
        back_populates="ledger_entries",
    )
    fill: Mapped[Optional["ExecutionFill"]] = relationship(back_populates="ledger_entries")

    __table_args__ = (
        Index("ix_execution_ledger_entries_account", "paper_account_id"),
        Index("ix_execution_ledger_entries_deployment", "deployment_id"),
    )


class ExecutionRiskEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_risk_events"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("execution_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("execution_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    rejection_code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )

    deployment: Mapped["ExecutionDeployment"] = relationship(back_populates="risk_events")
    decision: Mapped[Optional["ExecutionDecision"]] = relationship(
        back_populates="risk_events",
    )
    order: Mapped[Optional["ExecutionOrder"]] = relationship(back_populates="risk_events")

    __table_args__ = (
        Index("ix_execution_risk_events_deployment", "deployment_id"),
    )


class ExecutionWorkerLease(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_worker_leases"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    deployment: Mapped["ExecutionDeployment"] = relationship(
        back_populates="worker_leases",
    )

    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            name="uq_execution_worker_leases_deployment",
        ),
        Index("ix_execution_worker_leases_expires", "expires_at"),
    )


class ExecutionControlState(TimestampMixin, Base):
    """Singleton row (id=1) for global execution controls such as kill switch."""

    __tablename__ = "execution_control_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    kill_switch_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    kill_switch_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class ExecutionAuditEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Operator audit trail for lifecycle and global risk controls."""

    __tablename__ = "execution_audit_events"

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    deployment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("execution_deployments.id", ondelete="SET NULL"),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_execution_audit_events_deployment", "deployment_id"),
        Index("ix_execution_audit_events_type", "event_type"),
    )
