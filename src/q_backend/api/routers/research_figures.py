from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.schemas.common import BulkDeleteResponse
from q_backend.api.schemas.research_figures import (
    BulkDeleteResearchFiguresRequest,
    ResearchFigureCreateRequest,
    ResearchFigureCreateResponse,
    ResearchFigureListResponse,
    ResearchFigureMetadata,
)
from q_backend.research.figure_service import (
    bulk_delete_figures,
    create_or_replace_figure,
    delete_figure,
    get_figure_content,
    get_figure_metadata,
    list_figures,
)

router = APIRouter(prefix="/api/v1/research/figures", tags=["research-figures"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ResearchFigureCreateResponse)
def create_figure(
    request: ResearchFigureCreateRequest,
    session: Session = Depends(get_session),
) -> ResearchFigureCreateResponse:
    figure_id, replaced = create_or_replace_figure(request, session)
    return ResearchFigureCreateResponse(figure_id=figure_id, replaced=replaced)


@router.get("", response_model=ResearchFigureListResponse)
def get_figures(
    collection: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> ResearchFigureListResponse:
    return list_figures(session, collection=collection, limit=limit, offset=offset)


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete(
    request: BulkDeleteResearchFiguresRequest,
    session: Session = Depends(get_session),
) -> BulkDeleteResponse:
    return bulk_delete_figures(request, session)


@router.get("/{figure_id}", response_model=ResearchFigureMetadata)
def get_single_figure(
    figure_id: str,
    session: Session = Depends(get_session),
) -> ResearchFigureMetadata:
    return get_figure_metadata(figure_id, session)


@router.get("/{figure_id}/content")
def get_single_figure_content(
    figure_id: str,
    session: Session = Depends(get_session),
) -> Response:
    return get_figure_content(figure_id, session)


@router.delete("/{figure_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_single_figure(
    figure_id: str,
    session: Session = Depends(get_session),
) -> None:
    delete_figure(figure_id, session)
