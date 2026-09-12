"""Feature-score-biased GA seeding weights (WO152).

Run selection precedence (read-only, no evaluation triggers):

1. Latest ``COMPLETED`` classical ``EvaluationRun`` on
   ``(symbol, timeframe, target_name=fwd_return, target_horizon=5, start, end)``
   when ``start``/``end`` are supplied (typically the discovery backtest window).
2. Otherwise ``find_latest_latent_evaluation_run`` for the instrument's production
   model so latent biasing works when the classical run used a different target
   or window.

Empty weights ``{}`` mean uniform seeding (WO151 behavior).
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from sqlalchemy.orm import Session

from q_backend.features.registry import FEATURE_SPECS
from q_backend.neural.gate import (
    _find_completed_run,
    _load_score_rows,
    find_latest_latent_evaluation_run,
)
from q_backend.storage.db.models import FeatureScoreRow
from q_backend.storage.db.repositories import get_neural_model_version

DEFAULT_TARGET_NAME = "fwd_return"
DEFAULT_TARGET_HORIZON = 5
ALPHA = 2.0

_LATENT_FEATURE_PATTERN = re.compile(r"^latent_(\d+)@")

FEATURE_NAME_TO_KIND: dict[str, str] = {
    name: spec.node_kind
    for name, spec in FEATURE_SPECS.items()
    if spec.source == "classical" and spec.node_kind is not None
}


def _latent_index_from_name(feature_name: str) -> int | None:
    match = _LATENT_FEATURE_PATTERN.match(feature_name)
    if match is None:
        return None
    return int(match.group(1)) - 1


def feature_name_to_kind(feature_name: str, *, n_latents: int) -> str | None:
    """Map a Feature Store name to a genome indicator kind, or ``None`` if unmapped."""
    kind = FEATURE_NAME_TO_KIND.get(feature_name)
    if kind is not None:
        return kind

    latent_index = _latent_index_from_name(feature_name)
    if latent_index is None:
        return None
    if latent_index < 0 or latent_index >= n_latents:
        return None
    return "ind.latent"


def _aggregate_max_abs_ic(rows: list[FeatureScoreRow], *, n_latents: int) -> dict[str, float]:
    max_by_kind: dict[str, float] = {}
    for row in rows:
        if row.ic is None or (isinstance(row.ic, float) and math.isnan(row.ic)):
            continue
        kind = feature_name_to_kind(row.feature_name, n_latents=n_latents)
        if kind is None:
            continue
        abs_ic = abs(float(row.ic))
        max_by_kind[kind] = max(max_by_kind.get(kind, 0.0), abs_ic)
    return max_by_kind


def _normalize_weights(max_by_kind: dict[str, float]) -> dict[str, float]:
    if not max_by_kind:
        return {}
    peak = max(max_by_kind.values())
    if peak <= 0.0:
        return {}
    return {kind: 1.0 + ALPHA * (abs_ic / peak) for kind, abs_ic in max_by_kind.items()}


def build_kind_weights(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    n_latents: int,
    start: datetime | None = None,
    end: datetime | None = None,
    latent_model_hash: str | None = None,
) -> dict[str, float]:
    """Latest feature scores → indicator-kind weights (|IC|-based); ``{}`` when none."""
    run = None
    if start is not None and end is not None:
        run = _find_completed_run(
            session,
            symbol=symbol,
            timeframe=timeframe,
            target_name=DEFAULT_TARGET_NAME,
            horizon=DEFAULT_TARGET_HORIZON,
            start=start,
            end=end,
        )

    if run is None and latent_model_hash is not None:
        version = get_neural_model_version(session, latent_model_hash)
        if version is not None:
            run = find_latest_latent_evaluation_run(session, version)

    if run is None:
        return {}

    rows = _load_score_rows(session, run.id)
    max_by_kind = _aggregate_max_abs_ic(rows, n_latents=n_latents)
    return _normalize_weights(max_by_kind)
