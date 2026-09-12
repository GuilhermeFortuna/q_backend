"""execution domain tables for paper and live forward trading

Revision ID: 20260630_0014
Revises: 20260628_0013
Create Date: 2026-06-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260630_0014"
down_revision: Union[str, Sequence[str], None] = "20260628_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MONEY = sa.Numeric(20, 8)
_QUANTITY = sa.Numeric(20, 8)
_PRICE = sa.Numeric(20, 8)


def upgrade() -> None:
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("initial_balance", _MONEY, nullable=False),
        sa.Column("cash_balance", _MONEY, nullable=False),
        sa.Column("sizing_config", sa.JSON(), nullable=False),
        sa.Column("risk_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_paper_accounts_name", "paper_accounts", ["name"], unique=False)

    op.create_table(
        "execution_control_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kill_switch_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("kill_switch_reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO execution_control_state "
            "(id, kill_switch_enabled, created_at, updated_at) "
            "VALUES (1, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )

    op.create_table(
        "execution_deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("broker_mode", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("strategy_name", sa.String(length=255), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("compiled_config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("sizing_config", sa.JSON(), nullable=False),
        sa.Column("risk_config", sa.JSON(), nullable=False),
        sa.Column(
            "live_activation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("last_bar_close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_account_id"], ["paper_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_deployments_paper_account",
        "execution_deployments",
        ["paper_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_deployments_lifecycle",
        "execution_deployments",
        ["lifecycle"],
        unique=False,
    )
    op.create_index(
        "ix_execution_deployments_symbol_tf",
        "execution_deployments",
        ["symbol", "timeframe"],
        unique=False,
    )

    op.create_table(
        "execution_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("bar_close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_name", sa.String(length=255), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("compiled_config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("sizing_config", sa.JSON(), nullable=False),
        sa.Column("risk_config", sa.JSON(), nullable=False),
        sa.Column("signal_action", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("requested_quantity", _QUANTITY, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "bar_close_time",
            name="uq_execution_decisions_deployment_bar_close",
        ),
    )
    op.create_index(
        "ix_execution_decisions_deployment",
        "execution_decisions",
        ["deployment_id"],
        unique=False,
    )

    op.create_table(
        "execution_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("broker_mode", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("quantity", _QUANTITY, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reconciliation_state", sa.String(length=32), nullable=False),
        sa.Column("external_order_id", sa.String(length=128), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("intent_committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decision_id"], ["execution_decisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_orders_deployment",
        "execution_orders",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_orders_status",
        "execution_orders",
        ["status"],
        unique=False,
    )

    op.create_table(
        "execution_fills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("broker_mode", sa.String(length=32), nullable=False),
        sa.Column("external_fill_id", sa.String(length=128), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", _QUANTITY, nullable=False),
        sa.Column("price", _PRICE, nullable=False),
        sa.Column("fee", _MONEY, nullable=False),
        sa.Column("slippage", _MONEY, nullable=False),
        sa.Column("quote_bid", _PRICE, nullable=True),
        sa.Column("quote_ask", _PRICE, nullable=True),
        sa.Column("quote_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["execution_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "broker_mode",
            "external_fill_id",
            name="uq_execution_fills_broker_external_id",
        ),
    )
    op.create_index(
        "ix_execution_fills_deployment",
        "execution_fills",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_fills_order",
        "execution_fills",
        ["order_id"],
        unique=False,
    )

    op.create_table(
        "execution_net_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", _QUANTITY, nullable=False),
        sa.Column("average_entry_price", _PRICE, nullable=True),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_net_positions_deployment",
        "execution_net_positions",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "uq_execution_net_positions_one_open",
        "execution_net_positions",
        ["deployment_id"],
        unique=True,
        postgresql_where=sa.text("is_open = true"),
        sqlite_where=sa.text("is_open = 1"),
    )

    op.create_table(
        "execution_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_account_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("fill_id", sa.Uuid(), nullable=True),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount", _MONEY, nullable=False),
        sa.Column("balance_after", _MONEY, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_account_id"], ["paper_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fill_id"], ["execution_fills.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_ledger_entries_account",
        "execution_ledger_entries",
        ["paper_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_ledger_entries_deployment",
        "execution_ledger_entries",
        ["deployment_id"],
        unique=False,
    )

    op.create_table(
        "execution_risk_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("rejection_code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decision_id"], ["execution_decisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["execution_orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_risk_events_deployment",
        "execution_risk_events",
        ["deployment_id"],
        unique=False,
    )

    op.create_table(
        "execution_worker_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("lease_token", sa.String(length=128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            name="uq_execution_worker_leases_deployment",
        ),
    )
    op.create_index(
        "ix_execution_worker_leases_expires",
        "execution_worker_leases",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_worker_leases_expires", table_name="execution_worker_leases")
    op.drop_table("execution_worker_leases")
    op.drop_index("ix_execution_risk_events_deployment", table_name="execution_risk_events")
    op.drop_table("execution_risk_events")
    op.drop_index("ix_execution_ledger_entries_deployment", table_name="execution_ledger_entries")
    op.drop_index("ix_execution_ledger_entries_account", table_name="execution_ledger_entries")
    op.drop_table("execution_ledger_entries")
    op.drop_index("uq_execution_net_positions_one_open", table_name="execution_net_positions")
    op.drop_index("ix_execution_net_positions_deployment", table_name="execution_net_positions")
    op.drop_table("execution_net_positions")
    op.drop_index("ix_execution_fills_order", table_name="execution_fills")
    op.drop_index("ix_execution_fills_deployment", table_name="execution_fills")
    op.drop_table("execution_fills")
    op.drop_index("ix_execution_orders_status", table_name="execution_orders")
    op.drop_index("ix_execution_orders_deployment", table_name="execution_orders")
    op.drop_table("execution_orders")
    op.drop_index("ix_execution_decisions_deployment", table_name="execution_decisions")
    op.drop_table("execution_decisions")
    op.drop_index("ix_execution_deployments_symbol_tf", table_name="execution_deployments")
    op.drop_index("ix_execution_deployments_lifecycle", table_name="execution_deployments")
    op.drop_index("ix_execution_deployments_paper_account", table_name="execution_deployments")
    op.drop_table("execution_deployments")
    op.drop_table("execution_control_state")
    op.drop_index("ix_paper_accounts_name", table_name="paper_accounts")
    op.drop_table("paper_accounts")
