import base64
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.deps import get_session
from q_backend.api.main import app
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import ResearchFigure
from q_backend.storage.lake.artifacts import lake_root
from q_backend.storage.settings import get_settings

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><circle cx="50" cy="50" r="40"/></svg>'


@pytest.fixture
def test_lake(tmp_path, monkeypatch):
    lake_dir = tmp_path / "lake"
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(lake_dir))
    get_settings.cache_clear()
    yield lake_dir
    get_settings.cache_clear()


@pytest.fixture
def test_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_session():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield session_factory
    app.dependency_overrides.pop(get_session, None)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(test_db, test_lake):
    return TestClient(app)


def test_post_and_get_content_png(client, test_db, test_lake):
    b64_data = base64.b64encode(PNG_BYTES).decode("ascii")
    payload = {
        "title": "PNG Equity",
        "collection": "curves",
        "content": {
            "kind": "image",
            "media_type": "image/png",
            "data": b64_data,
            "width": 640,
            "height": 480,
        },
    }
    resp = client.post("/api/v1/research/figures", json=payload)
    assert resp.status_code == 201
    created = resp.json()
    fig_id = created["figure_id"]
    assert created["replaced"] is False

    # GET metadata
    meta_resp = client.get(f"/api/v1/research/figures/{fig_id}")
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta["figure_id"] == fig_id
    assert meta["title"] == "PNG Equity"
    assert meta["collection"] == "curves"
    assert meta["kind"] == "image"
    assert meta["media_type"] == "image/png"
    assert meta["byte_size"] == len(PNG_BYTES)
    assert meta["revision"] == 1

    # GET content
    content_resp = client.get(f"/api/v1/research/figures/{fig_id}/content")
    assert content_resp.status_code == 200
    assert content_resp.content == PNG_BYTES
    assert "image/png" in content_resp.headers["content-type"]
    assert content_resp.headers["etag"] == '"1"'
    assert content_resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in content_resp.headers["content-security-policy"]


def test_post_and_get_content_svg(client, test_db, test_lake):
    b64_data = base64.b64encode(SVG_BYTES).decode("ascii")
    payload = {
        "title": "SVG Chart",
        "collection": "curves",
        "content": {
            "kind": "image",
            "media_type": "image/svg+xml",
            "data": b64_data,
            "width": 100,
            "height": 100,
        },
    }
    resp = client.post("/api/v1/research/figures", json=payload)
    assert resp.status_code == 201
    fig_id = resp.json()["figure_id"]

    content_resp = client.get(f"/api/v1/research/figures/{fig_id}/content")
    assert content_resp.status_code == 200
    assert content_resp.content == SVG_BYTES
    assert "image/svg+xml" in content_resp.headers["content-type"]
    assert content_resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in content_resp.headers["content-security-policy"]


def test_post_and_get_content_plotly(client, test_db, test_lake):
    fig_dict = {
        "data": [{"type": "scatter", "x": [1, 2], "y": [10.5, 20.25]}],
        "layout": {"title": {"text": "Plotly Test"}},
    }
    payload = {
        "title": "Interactive Plotly",
        "collection": "interactive",
        "content": {
            "kind": "plotly",
            "figure": fig_dict,
        },
    }
    resp = client.post("/api/v1/research/figures", json=payload)
    assert resp.status_code == 201
    fig_id = resp.json()["figure_id"]

    meta_resp = client.get(f"/api/v1/research/figures/{fig_id}")
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta["kind"] == "plotly"
    assert meta["media_type"] == "application/json"
    assert meta["width"] is None
    assert meta["height"] is None

    content_resp = client.get(f"/api/v1/research/figures/{fig_id}/content")
    assert content_resp.status_code == 200
    assert "application/json" in content_resp.headers["content-type"]
    assert json.loads(content_resp.content) == fig_dict


def test_replace_in_place_and_replace_false(client, test_db, test_lake):
    b64_png = base64.b64encode(PNG_BYTES).decode("ascii")
    payload = {
        "title": "Regime Figure",
        "collection": "regimes",
        "replace": True,
        "content": {
            "kind": "image",
            "media_type": "image/png",
            "data": b64_png,
            "width": 640,
            "height": 480,
        },
    }
    # Initial publish
    r1 = client.post("/api/v1/research/figures", json=payload)
    assert r1.status_code == 201
    d1 = r1.json()
    fig_id = d1["figure_id"]
    assert d1["replaced"] is False

    # Second publish with replace=True (default) updates in place
    b64_svg = base64.b64encode(SVG_BYTES).decode("ascii")
    payload_v2 = {
        "title": "Regime Figure",
        "collection": "regimes",
        "replace": True,
        "content": {
            "kind": "image",
            "media_type": "image/svg+xml",
            "data": b64_svg,
            "width": 100,
            "height": 100,
        },
    }
    r2 = client.post("/api/v1/research/figures", json=payload_v2)
    assert r2.status_code == 201
    d2 = r2.json()
    assert d2["figure_id"] == fig_id
    assert d2["replaced"] is True

    meta_resp = client.get(f"/api/v1/research/figures/{fig_id}")
    meta = meta_resp.json()
    assert meta["revision"] == 2
    assert meta["media_type"] == "image/svg+xml"

    # Check content is now SVG
    c_resp = client.get(f"/api/v1/research/figures/{fig_id}/content")
    assert c_resp.content == SVG_BYTES
    assert c_resp.headers["etag"] == '"2"'

    # Third publish with replace=False creates a new figure
    payload_v3 = {
        "title": "Regime Figure",
        "collection": "regimes",
        "replace": False,
        "content": {
            "kind": "image",
            "media_type": "image/png",
            "data": b64_png,
            "width": 640,
            "height": 480,
        },
    }
    r3 = client.post("/api/v1/research/figures", json=payload_v3)
    assert r3.status_code == 201
    d3 = r3.json()
    assert d3["figure_id"] != fig_id
    assert d3["replaced"] is False


def test_list_figures_sorting_filtering_paging_and_collection_summaries(client, test_db, test_lake):
    b64_png = base64.b64encode(PNG_BYTES).decode("ascii")
    for i in range(3):
        client.post(
            "/api/v1/research/figures",
            json={
                "title": f"A{i}",
                "collection": "col_A",
                "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
            },
        )
    for i in range(2):
        client.post(
            "/api/v1/research/figures",
            json={
                "title": f"B{i}",
                "collection": "col_B",
                "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
            },
        )

    # Full list
    resp = client.get("/api/v1/research/figures?limit=2&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0

    collections = {c["name"]: c["count"] for c in data["collections"]}
    assert collections == {"col_A": 3, "col_B": 2}

    # Filter by collection
    resp_filtered = client.get("/api/v1/research/figures?collection=col_A")
    assert resp_filtered.status_code == 200
    f_data = resp_filtered.json()
    assert f_data["total"] == 3
    assert len(f_data["items"]) == 3
    # collections field remains unfiltered
    f_collections = {c["name"]: c["count"] for c in f_data["collections"]}
    assert f_collections == {"col_A": 3, "col_B": 2}


def test_validation_errors_leave_no_row_and_no_file(client, test_db, test_lake, monkeypatch):
    session = test_db()

    def assert_empty():
        assert session.query(ResearchFigure).count() == 0
        fig_lake = lake_root() / "research_figures"
        if fig_lake.exists():
            assert list(fig_lake.iterdir()) == []

    # 1. Blank title
    b64_png = base64.b64encode(PNG_BYTES).decode("ascii")
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "   ",
            "collection": "c1",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 2. Blank collection
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "  ",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 3. Notes too long
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "notes": "x" * 4001,
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 4. Non-positive dimensions
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 0, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 5. Invalid base64
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "content": {
                "kind": "image",
                "media_type": "image/png",
                "data": "not-valid-base64!!!",
                "width": 10,
                "height": 10,
            },
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 6. PNG missing signature
    bad_png_b64 = base64.b64encode(b"NOT_A_PNG_HEADER_TEST").decode("ascii")
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "content": {"kind": "image", "media_type": "image/png", "data": bad_png_b64, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 7. SVG not well-formed XML
    bad_svg_b64 = base64.b64encode(b"<svg><unclosed>").decode("ascii")
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "content": {"kind": "image", "media_type": "image/svg+xml", "data": bad_svg_b64, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 8. SVG without <svg> root
    not_svg_b64 = base64.b64encode(b"<html><body>test</body></html>").decode("ascii")
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "content": {"kind": "image", "media_type": "image/svg+xml", "data": not_svg_b64, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 422
    assert_empty()

    # 9. Plotly without data array
    r = client.post(
        "/api/v1/research/figures",
        json={"title": "t1", "collection": "c1", "content": {"kind": "plotly", "figure": {"layout": {}}}},
    )
    assert r.status_code == 422
    assert_empty()

    # 10. Payload too large (413)
    monkeypatch.setenv("Q_RESEARCH_FIGURE_MAX_BYTES", "10")
    get_settings.cache_clear()
    r = client.post(
        "/api/v1/research/figures",
        json={
            "title": "t1",
            "collection": "c1",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    assert r.status_code == 413
    assert_empty()


def test_deletion_and_bulk_delete(client, test_db, test_lake):
    b64_png = base64.b64encode(PNG_BYTES).decode("ascii")
    r1 = client.post(
        "/api/v1/research/figures",
        json={
            "title": "F1",
            "collection": "del_col",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    id1 = r1.json()["figure_id"]

    r2 = client.post(
        "/api/v1/research/figures",
        json={
            "title": "F2",
            "collection": "del_col",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    id2 = r2.json()["figure_id"]

    # Delete single figure
    del_resp = client.delete(f"/api/v1/research/figures/{id1}")
    assert del_resp.status_code == 204
    assert not (lake_root() / "research_figures" / id1).exists()

    # 404 on subsequent get
    assert client.get(f"/api/v1/research/figures/{id1}").status_code == 404
    assert client.get(f"/api/v1/research/figures/{id1}/content").status_code == 404
    assert client.delete(f"/api/v1/research/figures/{id1}").status_code == 404

    # Bulk delete by ids
    r_bulk_ids = client.post(
        "/api/v1/research/figures/bulk-delete",
        json={"figure_ids": [id2, "nonexistent-id"]},
    )
    assert r_bulk_ids.status_code == 200
    assert r_bulk_ids.json() == {"deleted": 1, "not_found": ["nonexistent-id"]}
    assert not (lake_root() / "research_figures" / id2).exists()

    # Create figure for bulk delete by collection
    r3 = client.post(
        "/api/v1/research/figures",
        json={
            "title": "F3",
            "collection": "del_by_col",
            "content": {"kind": "image", "media_type": "image/png", "data": b64_png, "width": 10, "height": 10},
        },
    )
    id3 = r3.json()["figure_id"]
    r_bulk_col = client.post(
        "/api/v1/research/figures/bulk-delete",
        json={"collection": "del_by_col"},
    )
    assert r_bulk_col.status_code == 200
    assert r_bulk_col.json() == {"deleted": 1, "not_found": []}
    assert not (lake_root() / "research_figures" / id3).exists()

    # Bulk delete with both or neither rejected with 422
    assert client.post("/api/v1/research/figures/bulk-delete", json={}).status_code == 422
    assert (
        client.post(
            "/api/v1/research/figures/bulk-delete",
            json={"figure_ids": ["a"], "collection": "b"},
        ).status_code
        == 422
    )
