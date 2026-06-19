from fastapi import APIRouter

from q_backend.backtesting.strategy_registry import StrategiesResponse, list_registered_strategies

router = APIRouter(tags=["strategies"])


@router.get("/api/v1/strategies", response_model=StrategiesResponse)
def list_strategies():
    """Return registered strategy metadata and parameter schemas."""
    return {"strategies": list_registered_strategies()}
