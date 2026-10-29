"""Strategy files in the Research stack's strategy directory, and their frozen versions."""

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.schemas.strategy_files import (
    StrategyFileListResponse,
    StrategyVersionResponse,
)
from q_backend.backtesting.strategy_registry import StrategyParamSpec
from q_backend.research.params import to_wire_spec
from q_backend.research.strategy_files import (
    StrategyFileInfo,
    default_bundle_root,
    list_strategy_files,
    strategy_dir,
)
from q_backend.storage.db.models import ResearchStrategyVersion

router = APIRouter(tags=["strategies"])

# Validation results by bundle hash. A hash changes with every edit to the file or its helpers, so
# an entry never goes stale; the cache only spares repeated child-process checks of unchanged files.
_validation_cache: Dict[str, StrategyFileInfo] = {}


def _parameter_specs(params: Any) -> list[StrategyParamSpec]:
    return [StrategyParamSpec.model_validate(to_wire_spec(param)) for param in params]


@router.get("/api/v1/strategy-files", response_model=StrategyFileListResponse)
def list_strategy_files_endpoint():
    """List every strategy file in ``Q_STRATEGY_DIR`` with its validation result and parameters."""
    infos = list_strategy_files(strategy_dir(), bundle_root=default_bundle_root(), cache=_validation_cache)
    return {
        "strategy_files": [
            {
                "id": info.id,
                "label": info.label,
                "description": info.description,
                "class_name": info.class_name,
                "bundle_hash": info.bundle_hash,
                "valid": info.valid,
                "error": info.error,
                "parameters": _parameter_specs(info.parameters),
                "uses_tick_hooks": info.uses_tick_hooks,
            }
            for info in infos
        ]
    }


@router.get(
    "/api/v1/strategy-versions/{version_id}",
    response_model=StrategyVersionResponse,
    responses={404: {"description": "Strategy version not found"}},
)
def get_strategy_version(version_id: str, session: Session = Depends(get_session)):
    """Return one frozen strategy version, including the source it runs."""
    version = _version(session, version_id)
    return {
        "version_id": str(version.id),
        "strategy_id": version.strategy_id,
        "class_name": version.class_name,
        "bundle_hash": version.bundle_hash,
        "created_at": version.created_at,
        "parameters": _parameter_specs_from_wire(version.param_specs),
        "source_files": [{"path": entry["path"], "content": entry["content"]} for entry in version.source_files],
    }


def _parameter_specs_from_wire(specs: list[Dict[str, Any]]) -> list[StrategyParamSpec]:
    return [StrategyParamSpec.model_validate(spec) for spec in specs]


def _version(session: Session, version_id: str) -> Optional[ResearchStrategyVersion]:
    try:
        parsed = uuid.UUID(version_id)
    except ValueError:
        parsed = None
    version = session.get(ResearchStrategyVersion, parsed) if parsed is not None else None
    if version is None:
        raise HTTPException(status_code=404, detail=f"Strategy version '{version_id}' not found.")
    return version
