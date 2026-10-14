from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.schemas.research_studies import (
    ResearchStudyCreateRequest,
    ResearchStudyCreateResponse,
    ResearchStudyListResponse,
    ResearchStudyMetadata,
    ResearchStudyRenameRequest,
    ResearchStudySeriesResponse,
)
from q_backend.research.study_service import (
    create_or_replace_study,
    delete_study,
    get_study_series,
    list_studies,
    rename_study,
)

router = APIRouter(prefix="/api/v1/research/studies", tags=["research-studies"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ResearchStudyCreateResponse)
def create_study(
    request: ResearchStudyCreateRequest,
    session: Session = Depends(get_session),
) -> ResearchStudyCreateResponse:
    study_id, replaced = create_or_replace_study(request, session)
    return ResearchStudyCreateResponse(studyId=study_id, replaced=replaced)


@router.get("", response_model=ResearchStudyListResponse, response_model_exclude_none=True)
def get_studies(
    symbol: str = Query(min_length=1),
    timeframe: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> ResearchStudyListResponse:
    return list_studies(session, symbol=symbol, timeframe=timeframe)


@router.get("/{study_id}/series", response_model=ResearchStudySeriesResponse)
def get_series(
    study_id: str,
    start: str = Query(),
    end: str = Query(),
    session: Session = Depends(get_session),
) -> ResearchStudySeriesResponse:
    return get_study_series(study_id, start=start, end=end, session=session)


@router.patch("/{study_id}", response_model=ResearchStudyMetadata, response_model_exclude_none=True)
def rename(
    study_id: str,
    request: ResearchStudyRenameRequest,
    session: Session = Depends(get_session),
) -> ResearchStudyMetadata:
    return rename_study(study_id, name=request.name, session=session)


@router.delete("/{study_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_single_study(
    study_id: str,
    session: Session = Depends(get_session),
) -> None:
    delete_study(study_id, session)
