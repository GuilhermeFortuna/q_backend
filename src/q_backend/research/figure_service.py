from __future__ import annotations

import base64
import json
import logging
import uuid
import xml.etree.ElementTree
from typing import Optional

from fastapi import HTTPException, Response
from sqlalchemy.orm import Session

from q_backend.api.schemas.common import BulkDeleteResponse
from q_backend.api.schemas.research_figures import (
    BulkDeleteResearchFiguresRequest,
    ResearchCollectionSummary,
    ResearchFigureCreateRequest,
    ResearchFigureListResponse,
    ResearchFigureMetadata,
    ResearchFigureProvenance,
)
from q_backend.storage.db.models import ResearchFigure
from q_backend.storage.db.repositories import (
    bulk_delete_research_figures,
    create_research_figure,
    delete_research_figure,
    get_research_figure,
    get_research_figure_by_collection_and_title,
    get_research_figure_collection_summaries,
    list_research_figures,
    update_research_figure,
)
from q_backend.storage.lake.artifacts import (
    delete_research_figure_content,
    read_research_figure_content,
    write_research_figure_content,
)
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _validate_figure_content(
    request: ResearchFigureCreateRequest,
) -> tuple[bytes, str, str, Optional[int], Optional[int]]:
    if not request.title.strip():
        raise HTTPException(status_code=422, detail="title must not be blank")
    if not request.collection.strip():
        raise HTTPException(status_code=422, detail="collection must not be blank")

    content = request.content
    if content.kind == "image":
        if content.width <= 0 or content.height <= 0:
            raise HTTPException(status_code=422, detail="width and height must be positive")
        try:
            raw_bytes = base64.b64decode(content.data, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid base64 encoded data: {exc}") from exc

        if content.media_type == "image/png":
            ext = "png"
            media_type = "image/png"
            if not raw_bytes.startswith(PNG_SIGNATURE):
                raise HTTPException(status_code=422, detail="Invalid PNG image: missing PNG signature")
        elif content.media_type == "image/svg+xml":
            ext = "svg"
            media_type = "image/svg+xml"
            try:
                root = xml.etree.ElementTree.fromstring(raw_bytes)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Invalid SVG image: not well-formed XML: {exc}") from exc
            tag = root.tag
            if tag != "svg" and not tag.endswith("}svg"):
                raise HTTPException(
                    status_code=422, detail=f"Invalid SVG image: root element must be <svg>, got <{tag}>"
                )
        else:
            raise HTTPException(status_code=422, detail=f"Unsupported image media type: {content.media_type}")

        width: Optional[int] = content.width
        height: Optional[int] = content.height
    elif content.kind == "plotly":
        fig = content.figure
        if not isinstance(fig, dict) or "data" not in fig or not isinstance(fig["data"], list):
            raise HTTPException(status_code=422, detail="Plotly figure must contain a 'data' array")
        raw_bytes = json.dumps(fig).encode("utf-8")
        ext = "json"
        media_type = "application/json"
        width = None
        height = None
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported figure kind: {content.kind}")

    max_bytes = get_settings().research_figure_max_bytes
    byte_size = len(raw_bytes)
    if byte_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Figure content size ({byte_size} bytes) exceeds limit of {max_bytes} bytes",
        )

    return raw_bytes, ext, media_type, width, height


def create_or_replace_figure(
    request: ResearchFigureCreateRequest,
    session: Session,
) -> tuple[str, bool]:
    raw_bytes, ext, media_type, width, height = _validate_figure_content(request)
    provenance_dict = request.provenance.model_dump() if request.provenance else None

    existing = None
    if request.replace:
        existing = get_research_figure_by_collection_and_title(
            session,
            collection=request.collection,
            title=request.title,
        )

    if existing is not None:
        fig_id = str(existing.id)
        write_research_figure_content(fig_id, ext, raw_bytes)
        update_research_figure(
            session,
            existing.id,
            kind=request.content.kind,
            media_type=media_type,
            byte_size=len(raw_bytes),
            notes=request.notes,
            width=width,
            height=height,
            provenance=provenance_dict,
        )
        session.flush()
        return fig_id, True

    fig_uuid = uuid.uuid4()
    fig_id = str(fig_uuid)
    try:
        write_research_figure_content(fig_id, ext, raw_bytes)
    except Exception:
        delete_research_figure_content(fig_id)
        raise

    try:
        create_research_figure(
            session,
            figure_id=fig_uuid,
            title=request.title,
            collection=request.collection,
            kind=request.content.kind,
            media_type=media_type,
            byte_size=len(raw_bytes),
            notes=request.notes,
            width=width,
            height=height,
            provenance=provenance_dict,
        )
        session.flush()
    except Exception:
        delete_research_figure_content(fig_id)
        raise

    return fig_id, False


def _to_metadata(figure: ResearchFigure) -> ResearchFigureMetadata:
    provenance = ResearchFigureProvenance(**figure.provenance) if figure.provenance else None
    return ResearchFigureMetadata(
        figure_id=str(figure.id),
        title=figure.title,
        collection=figure.collection,
        notes=figure.notes,
        kind=figure.kind,
        media_type=figure.media_type,
        width=figure.width,
        height=figure.height,
        byte_size=figure.byte_size,
        revision=figure.revision,
        created_at=figure.created_at.isoformat() if figure.created_at else "",
        updated_at=figure.updated_at.isoformat() if figure.updated_at else "",
        provenance=provenance,
    )


def get_figure_metadata(
    figure_id: str,
    session: Session,
) -> ResearchFigureMetadata:
    try:
        fig_uuid = uuid.UUID(figure_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail=f"Research figure '{figure_id}' not found")
    fig = get_research_figure(session, fig_uuid)
    if fig is None:
        raise HTTPException(status_code=404, detail=f"Research figure '{figure_id}' not found")
    return _to_metadata(fig)


def get_figure_content(
    figure_id: str,
    session: Session,
) -> Response:
    try:
        fig_uuid = uuid.UUID(figure_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail=f"Research figure '{figure_id}' not found")
    fig = get_research_figure(session, fig_uuid)
    if fig is None:
        raise HTTPException(status_code=404, detail=f"Research figure '{figure_id}' not found")

    try:
        content_bytes, _ = read_research_figure_content(figure_id)
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    headers = {
        "ETag": f'"{fig.revision}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    return Response(content=content_bytes, media_type=fig.media_type, headers=headers)


def delete_figure(
    figure_id: str,
    session: Session,
) -> None:
    try:
        fig_uuid = uuid.UUID(figure_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail=f"Research figure '{figure_id}' not found")
    deleted = delete_research_figure(session, fig_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Research figure '{figure_id}' not found")
    delete_research_figure_content(figure_id)
    session.flush()


def bulk_delete_figures(
    request: BulkDeleteResearchFiguresRequest,
    session: Session,
) -> BulkDeleteResponse:
    deleted_count, not_found, deleted_ids = bulk_delete_research_figures(
        session,
        figure_ids=request.figure_ids,
        collection=request.collection,
    )
    for fig_id in deleted_ids:
        delete_research_figure_content(fig_id)
    session.flush()
    return BulkDeleteResponse(deleted=deleted_count, not_found=not_found)


def list_figures(
    session: Session,
    collection: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> ResearchFigureListResponse:
    items, total = list_research_figures(
        session,
        collection=collection,
        limit=limit,
        offset=offset,
    )
    summary_rows = get_research_figure_collection_summaries(session)
    collections = [ResearchCollectionSummary(**row) for row in summary_rows]
    return ResearchFigureListResponse(
        items=[_to_metadata(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
        collections=collections,
    )
