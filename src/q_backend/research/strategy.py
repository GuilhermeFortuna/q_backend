"""ResearchStrategy ABC definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import pandas as pd

from q_backend.research.charting import ChartIndicator
from q_backend.research.orders import TradeOrder
from q_backend.research.positions import ResearchPosition


class ResearchStrategy(ABC):
    """
    Abstract base class for custom quantitative research strategies.

    Strategies subclassing ``ResearchStrategy`` define up to four hooks governing
    indicator calculation, entry decisions, exit logic, and chart visualization.

    Notes
    -----
    Decision hooks (``entry_strategy`` and ``exit_strategy``) run per closed bar.
    Both hooks can optionally declare a ``positions`` parameter to receive an immutable
    tuple of currently filled :class:`~q_backend.research.ResearchPosition` snapshots.
    All hook calculations must remain strictly causal: no looking ahead into future bars.
    """

    def chart_indicators(self) -> Sequence[ChartIndicator]:
        """
        Declare the computed indicator columns to render on the Trade Chart.

        Returns
        -------
        Sequence of ChartIndicator
            Sequence of indicator configurations specifying column names, panes,
            colors, line styles, and line widths.
        """
        return ()

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        """
        Compute indicator columns over the full historical price frame.

        Parameters
        ----------
        frame : pandas.DataFrame
            Complete historical OHLCV frame.

        Returns
        -------
        pandas.DataFrame
            DataFrame augmented with computed indicator columns. Must not alter,
            drop, or reorder the original rows or index.
        """
        return frame

    @abstractmethod
    def entry_strategy(self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None:
        """
        Evaluate entry logic for the current bar given historical prefix.

        Parameters
        ----------
        frame : pandas.DataFrame
            Closed-bar history up to and including the current bar.
        positions : tuple of ResearchPosition, optional
            Tuple of currently filled open positions.

        Returns
        -------
        TradeOrder or None
            A buy/sell order request, or ``None`` to take no action.
        """
        ...

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None:
        """
        Evaluate exit logic for the current bar given historical prefix.

        Evaluated prior to ``entry_strategy`` on each bar.

        Parameters
        ----------
        frame : pandas.DataFrame
            Closed-bar history up to and including the current bar.
        positions : tuple of ResearchPosition, optional
            Tuple of currently filled open positions.

        Returns
        -------
        TradeOrder or None
            A close request (:meth:`TradeOrder.close()`), or ``None``.
        """
        return None
