from __future__ import annotations

from abc import ABC, abstractmethod
from enum import IntEnum

from q_backend.backtesting.strategy_registry import StrategyParamSpec


class Stance(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1


class SignalManager(ABC):
    id: str
    label: str
    description: str

    def param_specs(self) -> list[StrategyParamSpec]:
        return []

    @abstractmethod
    def combine(self, stances: list[Stance]) -> Stance:
        ...
