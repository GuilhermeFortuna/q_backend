"""ResearchStrategy ABC definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import pandas as pd

from q_backend.research.charting import ChartIndicator
from q_backend.research.orders import TradeOrder
from q_backend.research.positions import ResearchPosition


class ResearchStrategy(ABC):
    """Abstract base class for research strategies.

    User strategies provide exactly three hooks:
    1. ``compute_indicators(frame)``: computes technical indicators over the entire frame.
       Defaults to returning frame unmodified.
    2. ``entry_strategy(frame)``: abstract hook called per closed-bar prefix.
       Returns ``TradeOrder.buy()``, ``TradeOrder.sell()``, or ``None``.
    3. ``exit_strategy(frame)``: hook called per closed-bar prefix before entry_strategy.
       Returns ``TradeOrder.close()`` or ``None``. Defaults to returning ``None``.

    Decision hooks may additionally declare ``positions``, an immutable tuple of
    actual open positions after this bar's fills. Frame-only overrides remain
    supported. Exit and entry receive the same snapshot; requested orders fill
    later and do not change it. Hooks observe only closed-bar history and must
    not infer actual fills from previous returned orders or keep evolving state.
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
    def entry_strategy(self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None:
        """Produce an entry decision request for the current bar given historical prefix."""
        ...

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None:
        """Produce an exit decision request for the current bar given historical prefix."""
        return None
