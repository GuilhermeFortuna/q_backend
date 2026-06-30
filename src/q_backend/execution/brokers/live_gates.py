"""Activation gates for MT5 live order submission (WO172)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from q_backend.execution.brokers.base import BrokerRejection, BrokerRejectionCode


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


def evaluate_live_gates(
    gates: LiveExecutionGates,
    *,
    account_login: Optional[int],
) -> Optional[BrokerRejection]:
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
    if not gates.deployment_live_activation_enabled:
        return BrokerRejection(
            code=BrokerRejectionCode.LIVE_LOCKED,
            message="deployment live activation is disabled",
            context={"capability": "live_locked"},
        )
    if not gates.controlled_account_validated:
        return BrokerRejection(
            code=BrokerRejectionCode.LIVE_LOCKED,
            message=(
                "live execution is implemented but operationally unvalidated; "
                "controlled-account validation record is missing"
            ),
            context={"capability": "live_locked"},
        )
    return None
