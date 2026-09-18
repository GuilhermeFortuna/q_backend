"""Validate fake edge responses against vendored schemas."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tests.execution.fake_edge import fake_edge_server


def _schema(name: str) -> dict:
    root = Path(__file__).resolve().parents[2] / "contracts" / "schema" / "edge"
    return json.loads((root / name).read_text())


@pytest.mark.parametrize(
    "path,schema_file",
    [
        ("/v1/health", "common/health.schema.json"),
        ("/v1/quote?symbol=WIN$", "execution/quote-response.schema.json"),
        ("/v1/account", "execution/account-response.schema.json"),
    ],
)
def test_fake_edge_get_responses_match_schema(path, schema_file):
    schema = _schema(schema_file)
    with fake_edge_server() as (base_url, state):
        import httpx

        response = httpx.get(f"{base_url}{path}", headers={"X-Schema-Major": "1"})
        assert response.status_code == 200
        jsonschema.validate(response.json(), schema)


def test_fake_edge_submit_and_lookup_match_schemas():
    submit_schema = _schema("execution/submit-outcome.schema.json")
    lookup_schema = _schema("execution/lookup-outcome.schema.json")
    with fake_edge_server() as (base_url, _state):
        import httpx

        submit = httpx.post(
            f"{base_url}/v1/submit",
            json={
                "intent_id": "00000000-0000-0000-0000-000000000001",
                "order": {"symbol": "WIN$", "volume": 1.0, "side": "buy"},
            },
            headers={"X-Schema-Major": "1"},
        )
        assert submit.status_code == 200
        jsonschema.validate(submit.json(), submit_schema)

        lookup = httpx.post(
            f"{base_url}/v1/lookup",
            json={
                "intent_id": "00000000-0000-0000-0000-000000000001",
                "window_start": "2024-01-01T00:00:00Z",
                "window_end": "2024-06-01T00:00:00Z",
            },
            headers={"X-Schema-Major": "1"},
        )
        assert lookup.status_code == 200
        jsonschema.validate(lookup.json(), lookup_schema)
