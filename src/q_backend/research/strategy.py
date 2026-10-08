"""ResearchStrategy ABC definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import pandas as pd

from q_backend.research.charting import ChartIndicator
from q_backend.research.orders import TradeOrder


class ResearchStrategy(ABC):
    """Abstract base class for research strategies.

    User strategies provide exactly three hooks:
    1. ``compute_indicators(frame)``: computes technical indicators over the entire frame.
       Defaults to returning frame unmodified.
    2. ``entry_strategy(frame)``: abstract hook called per closed-bar prefix.
       Returns ``TradeOrder.buy()``, ``TradeOrder.sell()``, or ``None``.
    3. ``exit_strategy(frame)``: hook called per closed-bar prefix before entry_strategy.
       Returns ``TradeOrder.close()`` or ``None``. Defaults to returning ``None``.

    Hooks observe only closed-bar history up to the current bar, have no fill or position state,
    and must not keep evolving state or assume order execution.
    """

    def chart_indicators(self) -> Sequence[ChartIndicator]:
        """Declare the computed columns the Trade Chart draws, in order.

        Optional. Columns must exist in the frame returned by ``compute_indicators``.
        Called once per backtest; it does not receive the frame.
        """
        return ()

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Compute indicator columns over the historical frame.

        Defaults to returning the input frame unchanged. Must not drop or reorder
        rows or modify the index.
        """
        return frame

    @abstractmethod
    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        """Produce an entry decision request for the current bar given historical prefix."""
        ...

    def exit_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        """Produce an exit decision request for the current bar given historical prefix."""
        return None
