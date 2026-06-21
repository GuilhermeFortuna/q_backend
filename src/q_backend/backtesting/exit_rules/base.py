from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from q_backend.backtesting.models import Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class ExitRule(ABC):
    id: str
    exit_group: str
    label: str
    description: str
    enable_param: str
    enable_value: float | int

    @abstractmethod
    def param_specs(self) -> list[StrategyParamSpec]:
        ...

    @abstractmethod
    def is_enabled(self, params: dict[str, Any]) -> bool:
        ...

    def param_names(self) -> list[str]:
        return [
            spec.name
            for spec in self.param_specs()
            if spec.exit_group != "general"
        ]

    def required_param_names(self) -> list[str]:
        return []

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        return []

    def on_bar(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        return None

    @abstractmethod
    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        ...
