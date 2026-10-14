"""Tests for the research studies routes: validation, limits, range reads, identity and deletion."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.deps import get_session
from q_backend.api.main import app
from q_backend.storage.db.base import Base
from q_backend.storage.lake.artifacts import study_lake_dir
from q_backend.storage.settings import get_settings

TIMESTAMPS = [
    "2026-01-05T13:00:00+00:00",
    "2026-01-05T13:10:00+00:00",
    "2026-01-05T13:20:00+00:00",
    "2026-01-05T13:30:00+00:00",
]


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


def _request(**overrides) -> dict:
    body = {
        "name": "Keltner 20/2",
        "symbol": "WDO$N",
        "timeframe": "M10",
        "notes": "band over M10 bars",
        "timestamps": list(TIMESTAMPS),
        "series": [
            {
                "key": "kc_upper",
                "label": "Upper",
                "pane": "price",
                "color": "#6eb5ff",
                "lineStyle": "dashed",
                "lineWidth": 1.5,
                "values": [3.0, None, 3.2, 3.3],
            },
            {
                "key": "kc_width",
                "label": "Width",
                "pane": "oscillator",
                "values": [1.0, 1.1, 1.2, 1.3],
            },
        ],
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict):
    return client.post("/api/v1/research/studies", json=body)


def test_post_then_range_series_returns_only_the_requested_rows(client):
    response = _post(client, _request())
    assert response.status_code == 201
    study_id = response.json()["studyId"]
    assert response.json()["replaced"] is False

    series = client.get(
        f"/api/v1/research/studies/{study_id}/series",
        params={"start": "2026-01-05T13:10:00+00:00", "end": "2026-01-05T13:20:00+00:00"},
    )
    assert series.status_code == 200
    body = series.json()
    assert body["timestamps"] == ["2026-01-05T13:10:00+00:00", "2026-01-05T13:20:00+00:00"]
    values = {item["key"]: item["values"] for item in body["series"]}
    assert values["kc_upper"] == [None, 3.2]
    assert values["kc_width"] == [1.1, 1.2]


def test_metadata_lists_only_the_symbol_and_orders_newest_first(client):
    first = _post(client, _request(name="Band A")).json()["studyId"]
    _post(client, _request(name="Band B", symbol="WDOV26"))
    second = _post(client, _request(name="Band C")).json()["studyId"]

    listing = client.get("/api/v1/research/studies", params={"symbol": "wdo$n"})
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["name"] for item in items] == ["Band C", "Band A"]
    assert {item["studyId"] for item in items} == {first, second}
    assert all(item["symbol"] == "WDO$N" for item in items)

    detail = items[0]
    assert detail["pointCount"] == 4
    assert detail["revision"] == 1
    assert detail["start"] == "2026-01-05T13:00:00+00:00"
    assert detail["end"] == "2026-01-05T13:30:00+00:00"
    assert detail["series"][0] == {
        "key": "kc_upper",
        "label": "Upper",
        "pane": "price",
        "color": "#6eb5ff",
        "lineStyle": "dashed",
        "lineWidth": 1.5,
    }


def test_replacement_keeps_id_and_created_at_and_increments_revision(client):
    first = _post(client, _request()).json()["studyId"]
    created = client.get("/api/v1/research/studies", params={"symbol": "WDO$N"}).json()["items"][0]

    response = _post(
        client,
        _request(timestamps=TIMESTAMPS[:2], series=[{**_request()["series"][1], "values": [1.0, 1.1]}]),
    )
    assert response.status_code == 201
    assert response.json() == {"studyId": first, "replaced": True}

    replaced = client.get("/api/v1/research/studies", params={"symbol": "WDO$N"}).json()["items"][0]
    assert replaced["studyId"] == first
    assert replaced["createdAt"] == created["createdAt"]
    assert replaced["revision"] == 2
    assert replaced["pointCount"] == 2


def test_replace_false_on_existing_identity_is_a_conflict(client):
    _post(client, _request())
    response = _post(client, _request(replace=False))
    assert response.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "   "},
        {"name": "x" * 121},
        {"symbol": "  "},
        {"timeframe": "M7"},
        {"timestamps": ["2026-01-05T13:10:00+00:00", "2026-01-05T13:00:00+00:00"]},
        {"timestamps": [TIMESTAMPS[0], TIMESTAMPS[0]]},
        {"timestamps": ["2026-01-05T13:00:00"]},
        {"series": [{"key": "a", "label": "a", "pane": "price", "values": [1.0]}]},
        {
            "series": [
                {"key": "a", "label": "a", "pane": "price", "values": [1.0, 2.0, 3.0, 4.0]},
                {"key": "a", "label": "b", "pane": "price", "values": [1.0, 2.0, 3.0, 4.0]},
            ]
        },
        {"series": [{"key": "a", "label": "a", "pane": "sidebar", "values": [1.0, 2.0, 3.0, 4.0]}]},
        {"series": [{"key": "a", "label": "a", "pane": "price", "lineStyle": "wavy", "values": [1.0, 2.0, 3.0, 4.0]}]},
        {"series": [{"key": "a", "label": "a", "pane": "price", "lineWidth": 0, "values": [1.0, 2.0, 3.0, 4.0]}]},
        {"series": [{"key": "a", "label": "a", "pane": "price", "values": [None, None, None, None]}]},
        {"series": [{"key": "time", "label": "t", "pane": "price", "values": [1.0, 2.0, 3.0, 4.0]}]},
        {"series": []},
    ],
)
def test_documented_validation_failures_return_422(client, overrides):
    response = _post(client, _request(**overrides))
    assert response.status_code == 422, response.text


def test_value_limit_returns_413(client, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_STUDY_MAX_VALUES", "7")
    get_settings.cache_clear()
    response = _post(client, _request())
    assert response.status_code == 413
    get_settings.cache_clear()


def test_rename_onto_existing_identity_returns_409(client):
    first = _post(client, _request(name="Band A")).json()["studyId"]
    _post(client, _request(name="Band B"))

    clash = client.patch(f"/api/v1/research/studies/{first}", json={"name": "Band B"})
    assert clash.status_code == 409

    renamed = client.patch(f"/api/v1/research/studies/{first}", json={"name": "Band renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Band renamed"
    assert renamed.json()["studyId"] == first


def test_delete_removes_row_and_parquet_file(client, test_db, test_lake):
    study_id = _post(client, _request()).json()["studyId"]
    assert (study_lake_dir(study_id) / "values.parquet").is_file()

    response = client.delete(f"/api/v1/research/studies/{study_id}")
    assert response.status_code == 204
    assert not study_lake_dir(study_id).exists()

    assert (
        client.get(
            f"/api/v1/research/studies/{study_id}/series", params={"start": TIMESTAMPS[0], "end": TIMESTAMPS[-1]}
        ).status_code
        == 404
    )
    listing = client.get("/api/v1/research/studies", params={"symbol": "WDO$N"}).json()
    assert listing["items"] == []


def test_unknown_study_id_is_404_on_every_route(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.get(
            f"/api/v1/research/studies/{missing}/series", params={"start": TIMESTAMPS[0], "end": TIMESTAMPS[-1]}
        ).status_code
        == 404
    )
    assert client.patch(f"/api/v1/research/studies/{missing}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/v1/research/studies/{missing}").status_code == 404
    assert (
        client.get(
            f"/api/v1/research/studies/not-a-uuid/series", params={"start": TIMESTAMPS[0], "end": TIMESTAMPS[-1]}
        ).status_code
        == 404
    )


def test_failed_write_leaves_neither_row_nor_file(client, test_db, monkeypatch):
    import q_backend.research.study_service as study_service

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(study_service, "create_research_study", boom)
    with pytest.raises(OSError):
        _post(client, _request())

    session = test_db()
    try:
        from q_backend.storage.db.models import ResearchStudy

        assert session.query(ResearchStudy).count() == 0
    finally:
        session.close()


def test_publish_study_library_round_trips_through_the_api(client):
    import pandas as pd

    from q_backend.research import ChartIndicator, publish_study

    index = pd.DatetimeIndex(["2026-01-05 10:00", "2026-01-05 10:10", "2026-01-05 10:20"])
    frame = pd.DataFrame({"kc_upper": [3.0, None, 3.2], "kc_width": [1.0, 1.1, 1.2]}, index=index)

    study_id = publish_study(
        frame,
        symbol="wdo$n",
        name="Keltner 20/2",
        series=[
            ChartIndicator("kc_upper", color="#6eb5ff"),
            ChartIndicator("kc_width", pane="oscillator", label="Width"),
        ],
        api_url="http://testserver",
        client=client,
    )

    listing = client.get("/api/v1/research/studies", params={"symbol": "WDO$N"}).json()["items"]
    assert [item["studyId"] for item in listing] == [study_id]
    assert listing[0]["timeframe"] == "M10"

    series = client.get(
        f"/api/v1/research/studies/{study_id}/series",
        params={"start": "2026-01-05T13:00:00+00:00", "end": "2026-01-05T13:20:00+00:00"},
    ).json()
    assert series["timestamps"] == [
        "2026-01-05T13:00:00+00:00",
        "2026-01-05T13:10:00+00:00",
        "2026-01-05T13:20:00+00:00",
    ]
    values = {item["key"]: item["values"] for item in series["series"]}
    assert values == {"kc_upper": [3.0, None, 3.2], "kc_width": [1.0, 1.1, 1.2]}
