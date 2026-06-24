from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from q_backend.backtesting.ai_strategy_metadata import AiStrategyMetadata

from q_backend.backtesting.strategy_registry import (
    ExitRuleCatalogResponse,
    SignalManagerCatalogResponse,
    StrategiesResponse,
    list_registered_strategies,
    _STRATEGY_REGISTRY,
    _CUSTOM_STRATEGIES,
)
from q_backend.backtesting.exit_rules.registry import list_exit_rules, shared_exit_params
from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.signal_managers.registry import list_signal_managers
from q_backend.backtesting.custom_strategy_store import (
    load_custom_strategies,
    save_custom_strategies,
)

router = APIRouter(tags=["strategies"])


class CustomStrategySaveRequest(BaseModel):
    name: str
    base_strategy: str
    description: Optional[str] = ""
    parameters: dict[str, Any]
    ai_metadata: AiStrategyMetadata | None = None


@router.get("/api/v1/strategies", response_model=StrategiesResponse)
def list_strategies():
    """Return registered strategy metadata and parameter schemas."""
    return {"strategies": list_registered_strategies()}


@router.get("/api/v1/exit-rules", response_model=ExitRuleCatalogResponse)
def list_exit_rules_catalog():
    """Return exit-rule metadata, shared indicator params, and named presets."""
    return {
        "exit_rules": list_exit_rules(),
        "shared_exit_params": shared_exit_params(),
        "exit_presets": EXIT_PRESETS,
    }


@router.get("/api/v1/signal-managers", response_model=SignalManagerCatalogResponse)
def list_signal_managers_catalog():
    """Return signal-manager metadata and parameter schemas."""
    return {"managers": list_signal_managers()}


@router.get("/api/v1/strategies/custom")
def get_custom_strategies():
    """List all user-defined custom strategies."""
    return load_custom_strategies()


@router.post("/api/v1/strategies/custom")
def save_custom_strategy(req: CustomStrategySaveRequest):
    """Save or update a user-defined custom strategy."""
    # Ensure custom strategy name does not conflict with a built-in strategy
    if req.name in _STRATEGY_REGISTRY and req.name not in _CUSTOM_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot overwrite built-in strategy '{req.name}'.",
        )

    # Validate that the base strategy exists
    if req.base_strategy not in _STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Base strategy '{req.base_strategy}' not found.",
        )

    customs = load_custom_strategies()
    updated = False
    for item in customs:
        if item["name"] == req.name:
            item["base_strategy"] = req.base_strategy
            item["description"] = req.description
            item["parameters"] = req.parameters
            if req.ai_metadata is not None:
                item["ai_metadata"] = req.ai_metadata.model_dump(mode="json")
            updated = True
            break

    if not updated:
        record: dict[str, Any] = {
            "name": req.name,
            "base_strategy": req.base_strategy,
            "description": req.description,
            "parameters": req.parameters,
        }
        if req.ai_metadata is not None:
            record["ai_metadata"] = req.ai_metadata.model_dump(mode="json")
        customs.append(record)

    save_custom_strategies(customs)
    return {"status": "success", "message": f"Strategy '{req.name}' saved successfully."}


@router.delete("/api/v1/strategies/custom/{name}")
def delete_custom_strategy(name: str):
    """Delete a user-defined custom strategy."""
    customs = load_custom_strategies()
    filtered = [item for item in customs if item["name"] != name]
    if len(filtered) == len(customs):
        raise HTTPException(
            status_code=404,
            detail=f"Custom strategy '{name}' not found.",
        )

    save_custom_strategies(filtered)
    # Remove from active registry immediately
    _STRATEGY_REGISTRY.pop(name, None)
    _CUSTOM_STRATEGIES.discard(name)
    return {"status": "success", "message": f"Strategy '{name}' deleted successfully."}
