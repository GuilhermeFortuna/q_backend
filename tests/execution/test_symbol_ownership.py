"""One active mt5_live deployment per paper account and symbol in the replay profile (Q-145)."""

from __future__ import annotations

from decimal import Decimal

from q_backend.api.services.execution import symbol_owner_conflict
from q_backend.execution.domain import BrokerMode, DeploymentLifecycle
from q_backend.storage.db.execution_repositories import create_execution_deployment, create_paper_account
from tests.execution.conftest import strategy_identity


def _deployment(session, account, *, symbol: str, lifecycle, broker_mode=BrokerMode.MT5_LIVE):
    identity = strategy_identity()
    return create_execution_deployment(
        session,
        paper_account_id=account.id,
        name=f"{symbol}-{lifecycle.value}",
        identity=identity.model_copy(update={"symbol": symbol}) if hasattr(identity, "model_copy") else identity,
        broker_mode=broker_mode,
        lifecycle=lifecycle,
        live_activation_enabled=True,
    )


def test_running_mt5_deployment_owns_its_symbol_on_the_account(db_session) -> None:
    account = create_paper_account(db_session, name="owner", initial_balance=Decimal("1000"))
    owner = _deployment(db_session, account, symbol="WIN$N", lifecycle=DeploymentLifecycle.RUNNING)
    db_session.commit()
    conflict = symbol_owner_conflict(db_session, paper_account_id=account.id, symbol=owner.symbol)
    assert conflict is not None and conflict.id == owner.id


def test_other_symbols_and_other_accounts_are_free(db_session) -> None:
    account = create_paper_account(db_session, name="a", initial_balance=Decimal("1000"))
    other = create_paper_account(db_session, name="b", initial_balance=Decimal("1000"))
    _deployment(db_session, account, symbol="WIN$N", lifecycle=DeploymentLifecycle.RUNNING)
    db_session.commit()
    assert symbol_owner_conflict(db_session, paper_account_id=account.id, symbol="WDO$N") is None
    assert symbol_owner_conflict(db_session, paper_account_id=other.id, symbol="WIN$N") is None


def test_stopped_deployments_do_not_hold_the_symbol(db_session) -> None:
    account = create_paper_account(db_session, name="s", initial_balance=Decimal("1000"))
    _deployment(db_session, account, symbol="WIN$N", lifecycle=DeploymentLifecycle.STOPPED)
    db_session.commit()
    assert symbol_owner_conflict(db_session, paper_account_id=account.id, symbol="WIN$N") is None
