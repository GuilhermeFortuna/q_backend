"""ResearchStrategy ABC definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

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
    Decision hooks (``entry_strategy`` and ``exit_strategy``) run per closed bar:
    ``exit_strategy`` first, then ``entry_strategy`` once the exit's priced closes have
    resolved. Either may return a list of orders whose fills the bar's ticks decide.
    Both hooks can optionally declare a ``positions`` parameter to receive an immutable
    tuple of currently filled :class:`~q_backend.research.ResearchPosition` snapshots.
    ``compute_indicators`` and both decision hooks can declare a ``ticks`` parameter to read
    the backtest's tick data through :class:`~q_backend.research.TickData`.
    All hook calculations must remain strictly causal: no looking ahead into future bars.

    A strategy that decides on a finer timeframe than its signals sets ``decision_timeframe`` to the
    timeframe of the frame it runs on, for example ``"M1"``, and reads coarser bars with
    :func:`~q_backend.research.closed_bars`. Stack runs and deployments refuse a frame of any other
    timeframe.
    """

    decision_timeframe: ClassVar[str | None] = None

    def chart_indicators(self) -> Sequence[ChartIndicator]:
        """
        Declare the computed indicator columns to render on the Trade Chart.

        Returns
        -------
        Sequence of ChartIndicator
            Sequence of indicator configurations specifying column names, panes,
            colors, line styles, line widths, and plot kinds. A ``kind="segment"``
            entry draws from the bar named by its ``start_column`` to the bar
            holding its value, such as a pivot from the bar where it started to
            the bar that confirmed it.
        """
        return ()

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        """
        Compute indicator columns over the full historical price frame.

        Parameters
        ----------
        frame : pandas.DataFrame
            Complete historical OHLCV frame.
        ticks : TickData, optional
            Declare a ``ticks`` parameter to read the tick data up to the end of the frame's last
            bar. It is ``None`` when the backtest has no ``ticks=`` store and the parameter has a
            default; a parameter without a default makes ``backtest()`` require ``ticks=``.

        Returns
        -------
        pandas.DataFrame
            DataFrame augmented with computed indicator columns. Must not alter,
            drop, or reorder the original rows or index.
        """
        return frame

    @abstractmethod
    def entry_strategy(
        self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()
    ) -> TradeOrder | Sequence[TradeOrder] | None:
        """
        Evaluate entry logic for the current bar given historical prefix.

        Evaluated after ``exit_strategy`` and its priced closes on each bar, so ``positions``
        no longer holds a position one of them closed.

        Parameters
        ----------
        frame : pandas.DataFrame
            Closed-bar history up to and including the current bar.
        positions : tuple of ResearchPosition, optional
            Tuple of currently filled open positions.
        ticks : TickData, optional
            Declare a ``ticks`` parameter to read the current bar's ticks, up to the bar's end.
            Same ``None`` and required rules as ``compute_indicators``.

        Returns
        -------
        TradeOrder, list of TradeOrder or None
            A buy or sell order request of any kind, ``TradeOrder.cancel()`` to remove the working
            entry order, or ``None`` to take no action. A list holds priced market entries
            (``TradeOrder.buy/sell(price=...)``): the first one the bar's ticks touch fills with its
            own levels and reason, and the others are cancelled.
        """
        ...

    def exit_strategy(
        self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()
    ) -> TradeOrder | Sequence[TradeOrder] | None:
        """
        Evaluate exit logic for the current bar given historical prefix.

        Evaluated prior to ``entry_strategy`` on each bar. When a priced entry fills on the
        bar and stays open, it is evaluated once more with that position, and only a
        ``TradeOrder.close()`` without a price, which exits at the next bar's open, is
        accepted from that call.

        Parameters
        ----------
        frame : pandas.DataFrame
            Closed-bar history up to and including the current bar.
        positions : tuple of ResearchPosition, optional
            Tuple of currently filled open positions.
        ticks : TickData, optional
            Declare a ``ticks`` parameter to read ticks through the current bar's end. Same
            ``None`` and required rules as ``compute_indicators``.

        Returns
        -------
        TradeOrder, list of TradeOrder or None
            A close request (:meth:`TradeOrder.close()`), optionally with an explicit traded exit
            price, an amendment of the open position's levels (:meth:`TradeOrder.amend`), or ``None``.
            A list holds any number of priced closes, at most one close without a price and at most
            one amend: the position closes at the priced close its ticks reach first, under that
            close's reason; the close without a price exits at the next open if none is reached.
            List only prices the strategy could have placed before the bar, such as its stop and
            target levels; the engine orders them by the ticks but cannot verify their causality.

        Notes
        -----
        Declaring ``ticks`` does not change when the hook runs: it is called once on each closed bar.
        A ``phase`` parameter was removed and is rejected during adapter setup.
        """
        return None
