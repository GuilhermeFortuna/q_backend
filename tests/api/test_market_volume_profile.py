"""Volume profile API route tests."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from quote_ticks import BUY_FLAG, write_trade_ticks

from q_backend.api.dependencies import get_market_data_service
from q_backend.api.main import app

SYMBOL = "WDO$"
URL = f"/api/v1/market/volume-profile/{SYMBOL}"


class _Mds:
    def get_symbol_info(self, _symbol: str) -> dict:
        return {"trade_tick_size": 0.5}


@pytest.fixture()
def client() -> TestClient:
    app.dependency_overrides[get_market_data_service] = lambda: _Mds()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_empty_range_returns_reason(client, tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    response = client.post(
        URL,
        json={"start": "2026-09-01", "end": "2026-09-02", "segment": "session", "rowTicks": 1, "valueArea": 0.7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["segments"] == []
    assert body["unavailableReason"]


def test_validation_errors(client):
    bad_range = client.post(
        URL,
        json={"start": "2026-09-02", "end": "2026-09-01", "segment": "session"},
    )
    assert bad_range.status_code == 422
    bad_segment = client.post(
        URL,
        json={"start": "2026-09-01", "end": "2026-09-02", "segment": "bar"},
    )
    assert bad_segment.status_code == 422


def test_profile_matches_research_helper(client, tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    nine = datetime(2026, 9, 1, 9)
    write_trade_ticks(tmp_path, SYMBOL, [(nine, 100.0, 10.0, BUY_FLAG)])

    response = client.post(
        URL,
        json={"start": "2026-09-01", "end": "2026-09-02", "segment": "session", "rowTicks": 1, "valueArea": 0.7},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["segments"]) == 1
    assert body["segments"][0]["total"] == pytest.approx(10.0)
