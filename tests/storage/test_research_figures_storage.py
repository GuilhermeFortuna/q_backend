from datetime import datetime, timezone
import pytest
from pathlib import Path

from q_backend.storage.settings import get_settings
from q_backend.storage.lake.artifacts import (
    lake_root,
    write_research_figure_content,
    read_research_figure_content,
    delete_research_figure_content,
)
from q_backend.storage.db.models import ResearchFigure
from q_backend.storage.db.repositories import (
    create_research_figure,
    get_research_figure,
    get_research_figure_by_collection_and_title,
    update_research_figure,
    list_research_figures,
    get_research_figure_collection_summaries,
    delete_research_figure,
    bulk_delete_research_figures,
)


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_research_figure_max_bytes_setting(monkeypatch):
    get_settings.cache_clear()
    assert get_settings().research_figure_max_bytes == 32 * 1024 * 1024

    monkeypatch.setenv("Q_RESEARCH_FIGURE_MAX_BYTES", "1048576")
    get_settings.cache_clear()
    assert get_settings().research_figure_max_bytes == 1048576
    get_settings.cache_clear()


def test_lake_atomic_write_read_and_extension_change(lake_root_path):
    fig_id = "test-fig-1"
    png_data = b"\x89PNG\r\n\x1a\nfake_png_data"
    svg_data = b"<svg>fake_svg_data</svg>"

    # 1. Write PNG
    png_path = write_research_figure_content(fig_id, "png", png_data)
    assert png_path.is_file()
    assert png_path.name == "content.png"

    # 2. Read PNG
    data, ext = read_research_figure_content(fig_id)
    assert data == png_data
    assert ext == "png"

    # 3. Replace with SVG (extension change)
    svg_path = write_research_figure_content(fig_id, "svg", svg_data)
    assert svg_path.is_file()
    assert svg_path.name == "content.svg"
    assert not png_path.exists()  # Old extension removed

    data, ext = read_research_figure_content(fig_id)
    assert data == svg_data
    assert ext == "svg"

    # 4. Delete
    deleted = delete_research_figure_content(fig_id)
    assert deleted is True
    assert not (lake_root() / "research_figures" / fig_id).exists()
    assert delete_research_figure_content(fig_id) is False


def test_db_create_and_get(db_session):
    fig = create_research_figure(
        db_session,
        title="Sharpe by regime",
        collection="macro",
        notes="H1 notes",
        kind="image",
        media_type="image/png",
        width=800,
        height=600,
        byte_size=1024,
        provenance={"script": "macro.py", "git_revision": "abc", "git_dirty": False},
    )
    assert fig.id is not None
    assert fig.revision == 1
    assert fig.title == "Sharpe by regime"
    assert fig.collection == "macro"
    assert fig.created_at is not None
    assert fig.updated_at is not None

    fetched = get_research_figure(db_session, fig.id)
    assert fetched is not None
    assert fetched.id == fig.id
    assert fetched.notes == "H1 notes"


def test_db_replace_updates_metadata_and_increments_revision(db_session):
    fig = create_research_figure(
        db_session,
        title="Sharpe by regime",
        collection="macro",
        kind="image",
        media_type="image/png",
        width=800,
        height=600,
        byte_size=1024,
    )
    original_id = fig.id
    original_created_at = fig.created_at

    by_col_title = get_research_figure_by_collection_and_title(db_session, collection="macro", title="Sharpe by regime")
    assert by_col_title is not None
    assert by_col_title.id == original_id

    updated = update_research_figure(
        db_session,
        fig.id,
        notes="Updated notes",
        kind="plotly",
        media_type="application/json",
        width=None,
        height=None,
        byte_size=2048,
        provenance={"script": "macro_v2.py"},
    )
    assert updated.id == original_id
    assert updated.created_at == original_created_at
    assert updated.revision == 2
    assert updated.kind == "plotly"
    assert updated.notes == "Updated notes"
    assert updated.byte_size == 2048


def test_db_list_with_pagination_and_collection_counts(db_session):
    # Add figures in collection A and B
    f1 = create_research_figure(
        db_session, title="Fig A1", collection="col_a", kind="image", media_type="image/png", byte_size=100
    )
    f2 = create_research_figure(
        db_session, title="Fig A2", collection="col_a", kind="image", media_type="image/png", byte_size=200
    )
    f3 = create_research_figure(
        db_session, title="Fig B1", collection="col_b", kind="image", media_type="image/png", byte_size=300
    )

    items, total = list_research_figures(db_session, limit=2, offset=0)
    assert total == 3
    assert len(items) == 2

    # Filter by collection
    items_a, total_a = list_research_figures(db_session, collection="col_a")
    assert total_a == 2
    assert len(items_a) == 2
    assert all(it.collection == "col_a" for it in items_a)

    # Collection summaries should be unfiltered
    summaries = get_research_figure_collection_summaries(db_session)
    sum_map = {s["name"]: s["count"] for s in summaries}
    assert sum_map == {"col_a": 2, "col_b": 1}


def test_db_delete_and_bulk_delete(db_session):
    f1 = create_research_figure(
        db_session, title="Fig 1", collection="col_del", kind="image", media_type="image/png", byte_size=100
    )
    f2 = create_research_figure(
        db_session, title="Fig 2", collection="col_del", kind="image", media_type="image/png", byte_size=200
    )
    f3 = create_research_figure(
        db_session, title="Fig 3", collection="col_other", kind="image", media_type="image/png", byte_size=300
    )

    # Single delete
    assert delete_research_figure(db_session, f3.id) is True
    assert get_research_figure(db_session, f3.id) is None
    assert delete_research_figure(db_session, f3.id) is False

    # Bulk delete by ids
    deleted, not_found, deleted_ids = bulk_delete_research_figures(
        db_session, figure_ids=[str(f1.id), "non-existent-id"]
    )
    assert deleted == 1
    assert not_found == ["non-existent-id"]
    assert deleted_ids == [str(f1.id)]

    # Bulk delete by collection
    deleted, not_found, deleted_ids = bulk_delete_research_figures(db_session, collection="col_del")
    assert deleted == 1
    assert not_found == []
    assert deleted_ids == [str(f2.id)]
