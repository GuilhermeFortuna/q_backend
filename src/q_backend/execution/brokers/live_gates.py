"""Activation gates for MT5 live order submission (WO172)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from q_backend.execution.brokers.base import BrokerRejection, BrokerRejectionCode

DEMO_TRADE_MODE = "demo"


@dataclass(frozen=True)
class LiveExecutionGates:
    """Three explicit gates plus controlled-account validation — all default deny."""

    enabled: bool = False
    account_allowlist: frozenset[int] = frozenset()
    deployment_live_activation_enabled: bool = False
    controlled_account_validated: bool = False

    @classmethod
    def from_settings(
        cls,
        *,
        enabled: bool,
        account_allowlist: str,
        deployment_live_activation_enabled: bool,
        controlled_account_validated: bool,
    ) -> LiveExecutionGates:
        accounts: set[int] = set()
        for part in account_allowlist.split(","):
            token = part.strip()
            if not token:
                continue
            accounts.add(int(token))
        return cls(
            enabled=enabled,
            account_allowlist=frozenset(accounts),
            deployment_live_activation_enabled=deployment_live_activation_enabled,
            controlled_account_validated=controlled_account_validated,
        )


def evaluate_account_gates(
    gates: LiveExecutionGates,
    *,
    account_login: Optional[int],
) -> Optional[BrokerRejection]:
    """Gates one to three: the global enable flag, a connected account and the allowlist."""
    if not gates.enabled:
        return BrokerRejection(
            code=BrokerRejectionCode.LIVE_LOCKED,
            message="live execution is disabled (Q_LIVE_EXECUTION_ENABLED=false)",
            context={"capability": "live_locked"},
        )
    if account_login is None:
        return BrokerRejection(
            code=BrokerRejectionCode.BROKER_UNAVAILABLE,
            message="MT5 account is not connected",
        )
    if account_login not in gates.account_allowlist:
        return BrokerRejection(
            code=BrokerRejectionCode.ACCOUNT_NOT_ALLOWLISTED,
            message="account is not on the live execution allowlist",
            context={"account_login": account_login},
        )
    return None


def evaluate_deployment_gate(gates: LiveExecutionGates) -> Optional[BrokerRejection]:
    """Gate four: the deployment's own live activation flag."""
    if not gates.deployment_live_activation_enabled:
        return BrokerRejection(
            code=BrokerRejectionCode.LIVE_LOCKED,
            message="deployment live activation is disabled",
            context={"capability": "live_locked"},
        )
    return None


def evaluate_controlled_account_gate(
    gates: LiveExecutionGates,
    *,
    trade_mode: Optional[str],
) -> Optional[BrokerRejection]:
    """Gate five: a demo account passes on its own trade mode; anything else needs validation.

    ``trade_mode`` is the mode the terminal reports for the account (``demo``, ``contest``,
    ``real``), or ``None`` when the edge did not report one. Only ``demo`` skips validation.
    """
    if trade_mode == DEMO_TRADE_MODE or gates.controlled_account_validated:
        return None
    reported = f"account trade mode is {trade_mode}" if trade_mode else "account reports no trade mode"
    return BrokerRejection(
        code=BrokerRejectionCode.LIVE_LOCKED,
        message=f"{reported}; live execution on it requires controlled-account validation (Q_LIVE_EXECUTION_VALIDATED)",
        context={"capability": "live_locked", "trade_mode": trade_mode},
    )


def evaluate_live_gates(
    gates: LiveExecutionGates,
    *,
    account_login: Optional[int],
    trade_mode: Optional[str] = None,
) -> Optional[BrokerRejection]:
    """Return the first failing gate, or ``None`` when an order may be submitted."""
    rejection = evaluate_account_gates(gates, account_login=account_login)
    if rejection is None:
        rejection = evaluate_deployment_gate(gates)
    if rejection is None:
        rejection = evaluate_controlled_account_gate(gates, trade_mode=trade_mode)
    return rejection
