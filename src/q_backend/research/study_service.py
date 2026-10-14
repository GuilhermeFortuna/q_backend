from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from q_backend.api.schemas.research_figures import ResearchFigureProvenance
from q_backend.api.schemas.research_studies import (
    ResearchStudyCreateRequest,
    ResearchStudyListResponse,
    ResearchStudyMetadata,
    ResearchStudySeriesResponse,
    ResearchStudySeriesValues,
    parse_utc_timestamps,
    study_series_metadata,
)
from q_backend.storage.db.models import ResearchStudy
from q_backend.storage.db.repositories import (
    create_research_study,
    delete_research_study,
    get_research_study,
    get_research_study_by_identity,
    list_research_studies,
    rename_research_study,
    update_research_study,
)
from q_backend.storage.lake.artifacts import (
    delete_research_study_values,
    read_research_study_values,
    write_research_study_values,
)
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)


def create_or_replace_study(request: ResearchStudyCreateRequest, session: Session) -> tuple[str, bool]:
    settings = get_settings()
    timestamps = parse_utc_timestamps(request.timestamps)
    if len(timestamps) > settings.research_study_max_timestamps:
        raise HTTPException(
            status_code=413,
            detail=f"Study has {len(timestamps)} timestamps; the limit is {settings.research_study_max_timestamps}",
        )
    value_count = sum(value is not None for item in request.series for value in item.values)
    if value_count > settings.research_study_max_values:
        raise HTTPException(
            status_code=413,
            detail=f"Study has {value_count} non-null values; the limit is {settings.research_study_max_values}",
        )

    existing = get_research_study_by_identity(
        session,
        symbol=request.symbol,
        timeframe=request.timeframe,
        name=request.name,
    )
    if existing is not None and not request.replace:
        raise HTTPException(
            status_code=409,
            detail=f"Study '{request.name}' already exists for {request.symbol} {request.timeframe}",
        )

    values = pd.DataFrame({"time": pd.DatetimeIndex(timestamps)})
    for item in request.series:
        values[item.key] = pd.Series(item.values, dtype="float64").to_numpy()
    series_metadata = [item.model_dump(by_alias=True, exclude={"values"}, exclude_none=True) for item in request.series]
    provenance = request.provenance.model_dump() if request.provenance else None
    start, end = timestamps[0], timestamps[-1]

    if existing is not None:
        study_id = str(existing.id)
        write_research_study_values(study_id, values)
        update_research_study(
            session,
            existing,
            series=series_metadata,
            start=start,
            end=end,
            point_count=len(timestamps),
            notes=request.notes,
            provenance=provenance,
        )
        session.flush()
        return study_id, True

    study_id = str(uuid.uuid4())
    try:
        write_research_study_values(study_id, values)
        create_research_study(
            session,
            study_id=study_id,
            name=request.name,
            symbol=request.symbol,
            timeframe=request.timeframe,
            series=series_metadata,
            start=start,
            end=end,
            point_count=len(timestamps),
            notes=request.notes,
            provenance=provenance,
        )
        session.flush()
    except Exception:
        delete_research_study_values(study_id)
        raise
    return study_id, False


def list_studies(session: Session, *, symbol: str, timeframe: Optional[str] = None) -> ResearchStudyListResponse:
    studies = list_research_studies(
        session,
        symbol=symbol.strip().upper(),
        timeframe=timeframe.strip().upper() if timeframe else None,
    )
    return ResearchStudyListResponse(items=[_to_metadata(study) for study in studies])


def get_study_series(study_id: str, *, start: str, end: str, session: Session) -> ResearchStudySeriesResponse:
    study = _get_or_404(session, study_id)
    start_ts = _parse_bound(start, "start")
    end_ts = _parse_bound(end, "end")
    if end_ts < start_ts:
        raise HTTPException(status_code=422, detail="end must not be before start")

    try:
        frame = read_research_study_values(str(study.id), pd.Timestamp(start_ts), pd.Timestamp(end_ts))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Research study '{study_id}' values not found") from exc

    series = [
        ResearchStudySeriesValues(
            key=item["key"],
            values=[None if np.isnan(value) else float(value) for value in frame[item["key"]].to_numpy()],
        )
        for item in study.series
    ]
    return ResearchStudySeriesResponse(
        timestamps=[_utc_iso(moment) for moment in frame["time"]],
        series=series,
    )


def rename_study(study_id: str, *, name: str, session: Session) -> ResearchStudyMetadata:
    study = _get_or_404(session, study_id)
    clash = get_research_study_by_identity(
        session,
        symbol=study.symbol,
        timeframe=study.timeframe,
        name=name,
    )
    if clash is not None and clash.id != study.id:
        raise HTTPException(
            status_code=409,
            detail=f"Study '{name}' already exists for {study.symbol} {study.timeframe}",
        )
    rename_research_study(session, study, name=name)
    return _to_metadata(study)


def delete_study(study_id: str, session: Session) -> None:
    if not delete_research_study(session, study_id):
        raise HTTPException(status_code=404, detail=f"Research study '{study_id}' not found")
    delete_research_study_values(study_id)
    session.flush()


def _get_or_404(session: Session, study_id: str) -> ResearchStudy:
    study = get_research_study(session, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"Research study '{study_id}' not found")
    return study


def _parse_bound(raw: str, label: str) -> datetime:
    try:
        return parse_utc_timestamps([raw])[0]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label}: {exc}") from exc


def _utc_iso(moment: datetime | pd.Timestamp) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _to_metadata(study: ResearchStudy) -> ResearchStudyMetadata:
    return ResearchStudyMetadata(
        studyId=str(study.id),
        name=study.name,
        symbol=study.symbol,
        timeframe=study.timeframe,
        notes=study.notes,
        series=study_series_metadata(study.series),
        start=_utc_iso(study.start),
        end=_utc_iso(study.end),
        pointCount=study.point_count,
        revision=study.revision,
        createdAt=_utc_iso(study.created_at),
        updatedAt=_utc_iso(study.updated_at),
        provenance=ResearchFigureProvenance(**study.provenance) if study.provenance else None,
    )
