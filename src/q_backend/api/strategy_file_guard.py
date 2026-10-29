"""Refuse strategy-file fields on workflows that only run registry strategies.

Strategy files run through ``POST /api/v1/backtest`` alone. Optimization, walk-forward, discovery,
ML-filter and execution requests embed their own backtest configuration, which ignores unknown
fields, so the guard inspects the raw body for ``strategy_file`` and ``strategy_version_id`` at any depth.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request

STRATEGY_FILE_FIELDS = frozenset({"strategy_file", "strategy_version_id"})


def _names_strategy_file(payload: Any) -> bool:
    if isinstance(payload, dict):
        return any(key in STRATEGY_FILE_FIELDS or _names_strategy_file(value) for key, value in payload.items())
    if isinstance(payload, list):
        return any(_names_strategy_file(item) for item in payload)
    return False


async def reject_strategy_file_fields(request: Request) -> None:
    """FastAPI dependency: 422 when the request body names a strategy file or version."""
    body = await request.body()
    if not body:
        return
    try:
        payload = json.loads(body)
    except ValueError:
        return  # FastAPI reports malformed JSON itself
    if _names_strategy_file(payload):
        raise HTTPException(
            status_code=422,
            detail=(
                "strategy_file and strategy_version_id are accepted only by POST /api/v1/backtest; "
                "this workflow runs registry strategies"
            ),
        )
