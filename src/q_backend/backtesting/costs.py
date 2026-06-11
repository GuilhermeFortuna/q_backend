from typing import Optional

from pydantic import BaseModel, Field


class TransactionCostConfig(BaseModel):
    cost_per_contract: float = Field(0.0, ge=0)
    cost_bps: float = Field(0.0, ge=0)


def side_cost(
    costs: Optional[TransactionCostConfig],
    price: float,
    quantity: float,
    point_value: float,
) -> float:
    if costs is None:
        return 0.0
    return quantity * costs.cost_per_contract + (
        costs.cost_bps / 10_000
    ) * price * quantity * point_value
