import math
import uuid
from abc import ABC, abstractmethod
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from q_backend.backtesting.models import (
    Signal,
    SignalAction,
    Order,
    OrderAction,
    OrderType,
)


class FixedQuantityPositionSizing(BaseModel):
    type: Literal["fixed_quantity"] = "fixed_quantity"
    quantity: float = Field(default=1.0, gt=0)


class FixedSafetyMarginPositionSizing(BaseModel):
    type: Literal["fixed_safety_margin"] = "fixed_safety_margin"
    safety_margin_per_contract: float = Field(default=5000.0, gt=0)
    min_contracts: int = Field(default=1, ge=0)
    max_contracts: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def max_gte_min(self):
        if self.max_contracts is not None and self.max_contracts < self.min_contracts:
            raise ValueError("max_contracts must be >= min_contracts")
        return self


PositionSizingConfig = Annotated[
    Union[FixedQuantityPositionSizing, FixedSafetyMarginPositionSizing],
    Field(discriminator="type"),
]


class PositionSizer(ABC):
    """
    Abstract base class for position sizing logic.
    Responsible for converting a trading Signal into an executable Order.
    """

    @abstractmethod
    def size_signal(
        self, signal: Signal, current_price: float, current_capital: float
    ) -> Optional[Order]:
        """
        Calculates the quantity and creates an Order based on a Signal.

        Args:
            signal: The trading signal.
            current_price: The current market price of the asset.
            current_capital: The current available capital in the backtest.

        Returns:
            Order if the signal warrants trading, else None.
        """
        pass

    @abstractmethod
    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        """
        Maximum absolute position size (in units/contracts) this risk model
        permits to be open per symbol at any one time.

        The engine enforces this cap: it never lets open exposure for a symbol
        exceed this value, so repeated same-direction signals cannot pyramid
        past the configured limit. Each risk model decides how the cap is
        derived (e.g. Fixed Quantity returns its quantity; a margin-based model
        derives it from capital). Return ``None`` for an unbounded model.
        """
        pass


class FixedQuantitySizer(PositionSizer):
    """
    A basic position sizer that always trades a fixed quantity.
    """

    def __init__(self, quantity: float = 1.0):
        self.quantity = quantity

    def size_signal(
        self, signal: Signal, current_price: float, current_capital: float
    ) -> Optional[Order]:
        if signal.action == SignalAction.HOLD:
            return None

        if signal.action == SignalAction.CLOSE:
            # We don't generate an Order for CLOSE signals right now because
            # the engine handles CLOSE signals by closing existing open trades directly.
            # In a more advanced broker execution model, CLOSE could be an opposite market order.
            return None

        action = (
            OrderAction.BUY if signal.action == SignalAction.BUY else OrderAction.SELL
        )

        # We assume Market orders for immediate execution for now
        return Order(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            action=action,
            order_type=OrderType.MARKET,
            quantity=self.quantity,
        )

    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        # The configured quantity is the maximum position size, not a per-order
        # amount: the engine caps total open exposure at this value.
        return self.quantity


class FixedSafetyMarginSizer(PositionSizer):
    """
    Futures position sizer that sizes contracts from allocated capital and a per-contract safety margin.
    """

    def __init__(
        self,
        safety_margin_per_contract: float,
        max_contracts: int | None = None,
        min_contracts: int = 1,
    ):
        if safety_margin_per_contract <= 0:
            raise ValueError("safety_margin_per_contract must be greater than 0")
        if min_contracts < 0:
            raise ValueError("min_contracts must be greater than or equal to 0")
        if max_contracts is not None and max_contracts < min_contracts:
            raise ValueError(
                "max_contracts must be greater than or equal to min_contracts"
            )

        self.safety_margin_per_contract = safety_margin_per_contract
        self.max_contracts = max_contracts
        self.min_contracts = min_contracts

    def _target_contracts(self, current_capital: float) -> int:
        """Contracts this model would hold given available capital (0 if none)."""
        quantity = math.floor(current_capital / self.safety_margin_per_contract)
        if self.max_contracts is not None:
            quantity = min(quantity, self.max_contracts)

        if quantity < self.min_contracts:
            if self.min_contracts > 0 and quantity == 0:
                # Balance below one full margin; holding min contracts is still allowed
                quantity = self.min_contracts
            else:
                return 0

        return quantity if quantity > 0 else 0

    def size_signal(
        self, signal: Signal, current_price: float, current_capital: float
    ) -> Optional[Order]:
        if signal.action == SignalAction.HOLD:
            return None

        if signal.action == SignalAction.CLOSE:
            return None

        quantity = self._target_contracts(current_capital)
        if quantity <= 0:
            return None

        action = (
            OrderAction.BUY if signal.action == SignalAction.BUY else OrderAction.SELL
        )

        return Order(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            action=action,
            order_type=OrderType.MARKET,
            quantity=float(quantity),
        )

    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        # The full contract count this model would size to is also the cap on
        # simultaneous open exposure for the symbol.
        return float(self._target_contracts(current_capital))


def build_position_sizer(
    config: Optional[
        FixedQuantityPositionSizing | FixedSafetyMarginPositionSizing
    ] = None,
) -> PositionSizer:
    if config is None:
        return FixedQuantitySizer(quantity=1.0)

    if config.type == "fixed_quantity":
        return FixedQuantitySizer(quantity=config.quantity)

    return FixedSafetyMarginSizer(
        safety_margin_per_contract=config.safety_margin_per_contract,
        min_contracts=config.min_contracts,
        max_contracts=config.max_contracts,
    )
