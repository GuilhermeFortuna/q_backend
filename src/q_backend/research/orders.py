"""Research TradeOrder model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from numbers import Integral, Real
from typing import ClassVar, Literal

OrderActionLiteral = Literal["buy", "sell", "close", "amend", "cancel", "oco"]
OrderKindLiteral = Literal["market", "stop", "limit"]


class PricedOrderDeprecationWarning(FutureWarning):
    """A run placed same-candle priced orders (``price=``), which a broker cannot hold.

    See the "Live-placeable orders" migration guide in the research user guide.
    """


def _validate_level(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive price, got {value!r}")


def _validate_reason(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"reason must be a non-blank string or None, got {value!r}")


def _validate_expiry(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"expire_bars must be a positive integer or None, got {value!r}")


def _validate_instant(name: str, value: time | datetime | None) -> None:
    """A time of day (read in the frame's timezone) or a timestamp; None when unset."""
    if value is None:
        return
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError(f"{name} time of day must be naive, read in the frame's timezone, got {value!r}")
        return
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime.time, a datetime or None, got {value!r}")


@dataclass(frozen=True, slots=True)
class TradeOrder:
    """
    An immutable trading order request produced by a strategy hook.

    Parameters
    ----------
    action : {'buy', 'sell', 'close', 'amend', 'cancel', 'oco'}
        Order action requested. ``amend`` moves the levels of open positions, ``cancel`` removes
        the working entry order, and ``oco`` is a one-cancels-the-other group of resting entries
        (see :meth:`oco`).
    stop_loss : float, optional
        Stop-loss exit level. On ``amend``, the new stop-loss of every open position.
    take_profit : float, optional
        Take-profit exit level. On ``amend``, the new take-profit of every open position.
    price : float, optional
        Price of a resting stop or limit entry. Market entries and closes cannot carry a price.
    kind : {'market', 'stop', 'limit'}, optional
        Entry order type. ``stop`` and ``limit`` orders require ``price``.
    expire_bars : int, optional
        Number of bars after the deciding bar on which a stop or limit order may fill.
        ``None`` keeps the order until it fills, is replaced or is cancelled. Only stop
        and limit orders accept it.
    expire_at : datetime.time or datetime, optional
        Instant after which a stop or limit order may no longer fill. A time of day means the
        next occurrence after the deciding bar, in the frame's timezone. A resting order
        expires at whichever of ``expire_bars`` and ``expire_at`` comes first.
    at : datetime.time or datetime, optional
        A timed close: the open position closes at the first trade at or after this instant
        (a time of day means its next occurrence after the deciding bar). Only ``close`` orders
        without a ``price`` accept it.
    legs : tuple of TradeOrder, optional
        The resting entries of an ``oco`` group.
    reason : str, optional
        Free-text label for why the order was placed. On an entry it becomes the
        ``entry_reason`` of the trade it opens; on ``close`` it becomes the ``exit_reason``
        of the trades it closes, when the exit would otherwise be a plain signal exit.
        Blank strings are rejected; ``amend`` and ``cancel`` carry no reason.

    Notes
    -----
    On a buy order, ``stop_loss`` must be strictly below ``price`` (or entry) and
    ``take_profit`` must be strictly above. On a sell order, the reverse applies.
    Close, amend and cancel orders carry no price.
    """

    action: OrderActionLiteral
    stop_loss: float | None = None
    take_profit: float | None = None
    price: float | None = None
    kind: OrderKindLiteral = "market"
    expire_bars: int | None = None
    reason: str | None = None
    expire_at: time | datetime | None = None
    at: time | datetime | None = None
    legs: tuple[TradeOrder, ...] = ()

    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset({"buy", "sell", "close", "amend", "cancel", "oco"})
    _VALID_KINDS: ClassVar[frozenset[str]] = frozenset({"market", "stop", "limit"})
    _MANAGEMENT_ACTIONS: ClassVar[frozenset[str]] = frozenset({"close", "amend", "cancel"})

    def __post_init__(self) -> None:
        if self.action not in self._VALID_ACTIONS:
            raise ValueError(
                f"Invalid TradeOrder action {self.action!r}. Must be 'buy', 'sell', 'close', 'amend', "
                "'cancel' or 'oco'."
            )
        if self.kind not in self._VALID_KINDS:
            raise ValueError(f"Invalid TradeOrder kind {self.kind!r}. Must be 'market', 'stop' or 'limit'.")
        if self.price is not None and self.action in ("buy", "sell") and self.kind == "market":
            raise ValueError("priced entries are removed; use resting stop or limit entries, or an OCO group")
        if self.price is not None and self.action == "close":
            raise ValueError("priced closes are removed; use a timed exit or protective levels with amend")
        _validate_level("stop_loss", self.stop_loss)
        _validate_level("take_profit", self.take_profit)
        _validate_level("price", self.price)
        _validate_expiry(self.expire_bars)
        _validate_reason(self.reason)
        _validate_instant("expire_at", self.expire_at)
        _validate_instant("at", self.at)
        if self.action == "oco":
            self._validate_oco()
            return
        if self.legs:
            raise ValueError("legs apply to oco orders only")
        if self.action in self._MANAGEMENT_ACTIONS:
            self._validate_management()
            return
        if self.at is not None:
            raise ValueError("at applies to close orders only")
        if self.kind == "market":
            if self.expire_bars is not None:
                raise ValueError("expire_bars applies to stop and limit orders only, not to market orders")
            if self.expire_at is not None:
                raise ValueError("expire_at applies to stop and limit orders only, not to market orders")
        elif self.price is None:
            raise ValueError(f"{self.kind} order needs a price")
        if self.price is not None:
            if self.action == "buy":
                if self.stop_loss is not None and self.stop_loss >= self.price:
                    raise ValueError(
                        f"stop_loss must be below price for a buy, got {self.stop_loss!r} and {self.price!r}"
                    )
                if self.take_profit is not None and self.take_profit <= self.price:
                    raise ValueError(
                        f"take_profit must be above price for a buy, got {self.take_profit!r} and {self.price!r}"
                    )
            elif self.action == "sell":
                if self.stop_loss is not None and self.stop_loss <= self.price:
                    raise ValueError(
                        f"stop_loss must be above price for a sell, got {self.stop_loss!r} and {self.price!r}"
                    )
                if self.take_profit is not None and self.take_profit >= self.price:
                    raise ValueError(
                        f"take_profit must be below price for a sell, got {self.take_profit!r} and {self.price!r}"
                    )
            return
        if self.stop_loss is None or self.take_profit is None:
            return
        if self.action == "buy" and self.stop_loss >= self.take_profit:
            raise ValueError(
                f"stop_loss must be below take_profit for a buy, got {self.stop_loss!r} and {self.take_profit!r}"
            )
        if self.action == "sell" and self.stop_loss <= self.take_profit:
            raise ValueError(
                f"stop_loss must be above take_profit for a sell, got {self.stop_loss!r} and {self.take_profit!r}"
            )

    def _validate_oco(self) -> None:
        if (
            self.kind != "market"
            or self.price is not None
            or self.stop_loss is not None
            or self.take_profit is not None
            or self.expire_bars is not None
            or self.expire_at is not None
            or self.at is not None
            or self.reason is not None
        ):
            raise ValueError("oco groups carry no order fields of their own; each leg carries its own")
        if len(self.legs) < 2:
            raise ValueError(f"an oco group needs at least two legs, got {len(self.legs)}")
        for leg in self.legs:
            if not isinstance(leg, TradeOrder):
                raise TypeError(f"oco legs must be TradeOrder, got {type(leg).__name__}")
            if leg.action == "oco":
                raise ValueError("oco groups do not nest")
            if leg.action not in ("buy", "sell") or leg.kind == "market":
                raise ValueError("oco legs are resting stop or limit entries (buy or sell with kind stop or limit)")

    def _validate_management(self) -> None:
        if self.kind != "market" or self.expire_bars is not None or self.expire_at is not None:
            raise ValueError(f"{self.action} orders carry no order kind or expiry")
        if self.action in ("amend", "cancel") and self.reason is not None:
            raise ValueError(f"{self.action} orders carry no reason")
        if self.at is not None and self.action != "close":
            raise ValueError("at applies to close orders only")
        if self.action == "close":
            if self.stop_loss is not None or self.take_profit is not None:
                raise ValueError("close orders carry no levels")
            if self.at is not None and self.price is not None:
                raise ValueError("a close is either priced or timed, not both")
        elif self.action == "cancel":
            if self.stop_loss is not None or self.take_profit is not None:
                raise ValueError("cancel orders carry no levels")
            if self.price is not None:
                raise ValueError("cancel orders carry no price")
        else:
            if self.price is not None:
                raise ValueError("amend orders carry no price")
            if self.stop_loss is None and self.take_profit is None:
                raise ValueError("amend needs at least one of stop_loss or take_profit")

    @classmethod
    def buy(
        cls,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        price: float | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a long entry order.

        Parameters
        ----------
        stop_loss : float, optional
            Protective stop-loss exit price level below entry.
        take_profit : float, optional
            Protective take-profit exit price level above entry.
        price : float, optional
            Not supported; use a resting stop or limit entry.
        reason : str, optional
            Label recorded as the trade's ``entry_reason``.

        Returns
        -------
        TradeOrder
            Configured buy order instance.
        """
        return cls("buy", stop_loss=stop_loss, take_profit=take_profit, price=price, reason=reason)

    @classmethod
    def sell(
        cls,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        price: float | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a short entry order.

        Parameters
        ----------
        stop_loss : float, optional
            Protective stop-loss exit price level above entry.
        take_profit : float, optional
            Protective take-profit exit price level below entry.
        price : float, optional
            Not supported; use a resting stop or limit entry.
        reason : str, optional
            Label recorded as the trade's ``entry_reason``.

        Returns
        -------
        TradeOrder
            Configured sell order instance.
        """
        return cls("sell", stop_loss=stop_loss, take_profit=take_profit, price=price, reason=reason)

    @classmethod
    def buy_stop(
        cls,
        price: float,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        expire_bars: int | None = 1,
        expire_at: time | datetime | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a long stop entry that fills at the first trade at or above ``price``.

        Parameters
        ----------
        price : float
            Order price; the entry fills at the first trade at or beyond it.
        stop_loss : float, optional
            Protective stop-loss level below ``price``.
        take_profit : float, optional
            Protective take-profit level above ``price``.
        expire_bars : int or None, default 1
            Bars after the deciding bar on which the order may fill; ``None`` keeps it working.
        expire_at : datetime.time or datetime, optional
            Instant after which the order may no longer fill.
        reason : str, optional
            Label recorded as the entry reason of the trade the order opens.

        Returns
        -------
        TradeOrder
            Configured resting buy order.
        """
        return cls._resting("buy", "stop", price, stop_loss, take_profit, expire_bars, expire_at, reason)

    @classmethod
    def buy_limit(
        cls,
        price: float,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        expire_bars: int | None = 1,
        expire_at: time | datetime | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a long limit entry that fills at ``price`` once a trade prints strictly below it.

        Parameters
        ----------
        price : float
            Order price, which the entry fills at.
        stop_loss : float, optional
            Protective stop-loss level below ``price``.
        take_profit : float, optional
            Protective take-profit level above ``price``.
        expire_bars : int or None, default 1
            Bars after the deciding bar on which the order may fill; ``None`` keeps it working.
        expire_at : datetime.time or datetime, optional
            Instant after which the order may no longer fill.
        reason : str, optional
            Label recorded as the entry reason of the trade the order opens.

        Returns
        -------
        TradeOrder
            Configured resting buy order.
        """
        return cls._resting("buy", "limit", price, stop_loss, take_profit, expire_bars, expire_at, reason)

    @classmethod
    def sell_stop(
        cls,
        price: float,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        expire_bars: int | None = 1,
        expire_at: time | datetime | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a short stop entry that fills at the first trade at or below ``price``.

        Parameters
        ----------
        price : float
            Order price; the entry fills at the first trade at or beyond it.
        stop_loss : float, optional
            Protective stop-loss level above ``price``.
        take_profit : float, optional
            Protective take-profit level below ``price``.
        expire_bars : int or None, default 1
            Bars after the deciding bar on which the order may fill; ``None`` keeps it working.
        expire_at : datetime.time or datetime, optional
            Instant after which the order may no longer fill.
        reason : str, optional
            Label recorded as the entry reason of the trade the order opens.

        Returns
        -------
        TradeOrder
            Configured resting sell order.
        """
        return cls._resting("sell", "stop", price, stop_loss, take_profit, expire_bars, expire_at, reason)

    @classmethod
    def sell_limit(
        cls,
        price: float,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        expire_bars: int | None = 1,
        expire_at: time | datetime | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a short limit entry that fills at ``price`` once a trade prints strictly above it.

        Parameters
        ----------
        price : float
            Order price, which the entry fills at.
        stop_loss : float, optional
            Protective stop-loss level above ``price``.
        take_profit : float, optional
            Protective take-profit level below ``price``.
        expire_bars : int or None, default 1
            Bars after the deciding bar on which the order may fill; ``None`` keeps it working.
        expire_at : datetime.time or datetime, optional
            Instant after which the order may no longer fill.
        reason : str, optional
            Label recorded as the entry reason of the trade the order opens.

        Returns
        -------
        TradeOrder
            Configured resting sell order.
        """
        return cls._resting("sell", "limit", price, stop_loss, take_profit, expire_bars, expire_at, reason)

    @classmethod
    def oco(cls, *orders: TradeOrder) -> TradeOrder:
        """
        Create a one-cancels-the-other group of resting entries.

        The first member the ticks fill opens the position and the others are cancelled at that
        trade. Return the group from ``entry_strategy`` like a single order. Each member is a
        ``buy_stop``, ``buy_limit``, ``sell_stop`` or ``sell_limit`` with its own levels, reason
        and expiry. Members filling on the same trade resolve to the one listed first.

        Parameters
        ----------
        *orders : TradeOrder
            At least two resting entries.

        Returns
        -------
        TradeOrder
            The group, valid from ``entry_strategy`` only.
        """
        return cls("oco", legs=tuple(orders))

    @classmethod
    def _resting(
        cls,
        action: OrderActionLiteral,
        kind: OrderKindLiteral,
        price: float,
        stop_loss: float | None,
        take_profit: float | None,
        expire_bars: int | None,
        expire_at: time | datetime | None,
        reason: str | None,
    ) -> TradeOrder:
        return cls(
            action,
            stop_loss=stop_loss,
            take_profit=take_profit,
            price=price,
            kind=kind,
            expire_bars=expire_bars,
            expire_at=expire_at,
            reason=reason,
        )

    @classmethod
    def close(
        cls,
        *,
        price: float | None = None,
        at: time | datetime | None = None,
        reason: str | None = None,
    ) -> TradeOrder:
        """
        Create a position close order.

        Parameters
        ----------
        price : float, optional
            Not supported; use a timed exit or protective levels with amend.
        at : datetime.time or datetime, optional
            A timed exit: the position closes at the first trade at or after this instant. A time
            of day means its next occurrence after the deciding bar, in the frame's timezone. A
            later ``close(at=...)`` replaces it, and it is dropped once the position is closed.
        reason : str, optional
            Label recorded as the ``exit_reason`` of the trades it closes, unless a stop,
            target or engine exit closes them first.

        Returns
        -------
        TradeOrder
            Close order request.
        """
        return cls("close", price=price, at=at, reason=reason)

    @classmethod
    def amend(cls, *, stop_loss: float | None = None, take_profit: float | None = None) -> TradeOrder:
        """
        Move the stop-loss and/or take-profit of the open position from the next bar.

        Parameters
        ----------
        stop_loss : float, optional
            New stop-loss level for every open position.
        take_profit : float, optional
            New take-profit level for every open position.

        Returns
        -------
        TradeOrder
            Amend request. It needs at least one level; while flat it does nothing.
        """
        return cls("amend", stop_loss=stop_loss, take_profit=take_profit)

    @classmethod
    def cancel(cls) -> TradeOrder:
        """
        Remove the working stop, limit or OCO entry order.

        Returns
        -------
        TradeOrder
            Cancel request.
        """
        return cls("cancel")
