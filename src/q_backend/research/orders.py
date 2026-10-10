"""Research TradeOrder model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import ClassVar, Literal

OrderActionLiteral = Literal["buy", "sell", "close"]


def _validate_level(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive price, got {value!r}")


@dataclass(frozen=True, slots=True)
class TradeOrder:
    """An immutable research decision request.

    ``stop_loss`` and ``take_profit`` are price levels fixed at entry. They are
    only meaningful on ``buy`` and ``sell``; a buy's stop lies below its target,
    a sell's above it. When ``price`` is set, it is the entry fill level for
    same-bar execution; a buy requires ``stop_loss < price < take_profit`` and a
    sell ``stop_loss > price > take_profit``.
    """

    action: OrderActionLiteral
    stop_loss: float | None = None
    take_profit: float | None = None
    price: float | None = None

    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset({"buy", "sell", "close"})

    def __post_init__(self) -> None:
        if self.action not in self._VALID_ACTIONS:
            raise ValueError(f"Invalid TradeOrder action {self.action!r}. Must be 'buy', 'sell', or 'close'.")
        _validate_level("stop_loss", self.stop_loss)
        _validate_level("take_profit", self.take_profit)
        _validate_level("price", self.price)
        if self.action == "close":
            if self.stop_loss is not None or self.take_profit is not None:
                raise ValueError("close orders carry no levels")
            if self.price is not None:
                raise ValueError("close orders carry no price")
            return
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

    @classmethod
    def buy(
        cls,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        price: float | None = None,
    ) -> TradeOrder:
        return cls("buy", stop_loss=stop_loss, take_profit=take_profit, price=price)

    @classmethod
    def sell(
        cls,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        price: float | None = None,
    ) -> TradeOrder:
        return cls("sell", stop_loss=stop_loss, take_profit=take_profit, price=price)

    @classmethod
    def close(cls) -> TradeOrder:
        return cls("close")
