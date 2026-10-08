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
    a sell's above it.
    """

    action: OrderActionLiteral
    stop_loss: float | None = None
    take_profit: float | None = None

    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset({"buy", "sell", "close"})

    def __post_init__(self) -> None:
        if self.action not in self._VALID_ACTIONS:
            raise ValueError(f"Invalid TradeOrder action {self.action!r}. Must be 'buy', 'sell', or 'close'.")
        _validate_level("stop_loss", self.stop_loss)
        _validate_level("take_profit", self.take_profit)
        if self.action == "close":
            if self.stop_loss is not None or self.take_profit is not None:
                raise ValueError("close orders carry no levels")
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
    def buy(cls, *, stop_loss: float | None = None, take_profit: float | None = None) -> TradeOrder:
        return cls("buy", stop_loss=stop_loss, take_profit=take_profit)

    @classmethod
    def sell(cls, *, stop_loss: float | None = None, take_profit: float | None = None) -> TradeOrder:
        return cls("sell", stop_loss=stop_loss, take_profit=take_profit)

    @classmethod
    def close(cls) -> TradeOrder:
        return cls("close")
