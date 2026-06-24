from __future__ import annotations

from typing import Any

from q_backend.backtesting.signal_managers.and_manager import AndManager
from q_backend.backtesting.signal_managers.base import SignalManager
from q_backend.backtesting.signal_managers.majority import MajorityManager
from q_backend.backtesting.signal_managers.or_manager import OrManager
from q_backend.backtesting.strategy_registry import SignalManagerInfo

_OR_MANAGER = OrManager()
_AND_MANAGER = AndManager()
_MAJORITY_MANAGER = MajorityManager()

SIGNAL_MANAGERS: list[SignalManager] = [
    _OR_MANAGER,
    _AND_MANAGER,
    _MAJORITY_MANAGER,
]


def get_manager(kind: str, params: dict[str, Any]) -> SignalManager:
    if kind == "or":
        return _OR_MANAGER
    if kind == "and":
        return _AND_MANAGER
    if kind == "majority":
        threshold = int(params.get("vote_threshold", 2))
        return MajorityManager(vote_threshold=threshold)
    raise ValueError(f"Unknown signal manager: {kind}")


def list_signal_managers() -> list[SignalManagerInfo]:
    return [
        SignalManagerInfo(
            id=manager.id,
            label=manager.label,
            description=manager.description,
            param_names=[spec.name for spec in manager.param_specs()],
            params=manager.param_specs(),
        )
        for manager in SIGNAL_MANAGERS
    ]
