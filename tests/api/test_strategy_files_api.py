"""Strategy-file listing, version lookup, and the refusal of strategy files on other workflows."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.api.schemas.strategy_files import StrategyVersionResponse
from q_backend.api.routers import strategy_files as strategy_files_router
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.storage.settings import get_settings
from test_strategy_file_backtests import (  # noqa: F401 - fixtures and helpers
    SIMPLE_MA,
    _row,
    _start,
    _write,
    strategy_env,
)

GUARDED_ROUTES = [
    "/api/v1/optimize",
    "/api/v1/walkforward",
    "/api/v1/strategy-search",
    "/api/v1/experiments/discovery-ab",
    "/api/v1/experiments/encoder-ablation",
    "/api/v1/experiments/alpha-research",
    "/api/v1/ml-filters/training",
    "/api/v1/ml-filters/comparisons",
    "/api/v1/ml-filters/evaluations",
    "/api/v1/execution/deployments",
]


@pytest.fixture(autouse=True)
def _fresh_validation_cache():
    strategy_files_router._validation_cache.clear()
    yield
    strategy_files_router._validation_cache.clear()


def test_listing_shows_valid_files_with_bool_and_time_specs_and_invalid_files_with_their_errors(strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    _write(
        strategy_env,
        "two.py",
        "from q_backend.research import ResearchStrategy\n"
        "class A(ResearchStrategy):\n    parameters = {}\n    def __init__(self):\n        pass\n"
        "    def entry_strategy(self, frame, positions=()):\n        return None\n"
        "class B(A):\n    pass\n",
    )

    payload = TestClient(app).get("/api/v1/strategy-files").json()

    files = {entry["id"]: entry for entry in payload["strategy_files"]}
    valid = files["simple_ma"]
    assert valid["valid"] is True and valid["error"] is None
    assert valid["label"] == "Simple MA"
    assert valid["description"].startswith("Buys closes above a moving average")
    kinds = {spec["name"]: spec["type"] for spec in valid["parameters"]}
    assert kinds == {"length": "int", "use_filter": "bool", "entry_after": "time"}
    entry_after = next(spec for spec in valid["parameters"] if spec["name"] == "entry_after")
    assert entry_after["default"] == "09:00"
    assert files["two"]["valid"] is False
    assert "2 ResearchStrategy subclasses" in files["two"]["error"]
    assert files["two"]["parameters"] == []


def test_listing_is_empty_when_the_directory_does_not_exist(strategy_env, monkeypatch, tmp_path):
    monkeypatch.setenv("Q_STRATEGY_DIR", str(tmp_path / "absent"))
    get_settings.cache_clear()

    assert strategy_files_router.list_strategy_files_endpoint() == {"strategy_files": []}


def test_a_listing_of_unchanged_files_is_served_from_the_cache(strategy_env, monkeypatch):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    strategy_files_router.list_strategy_files_endpoint()

    def no_validation(*_args, **_kwargs):
        raise AssertionError("an unchanged file must not be validated again")

    monkeypatch.setattr("q_backend.research.strategy_files._run_check", no_validation)

    payload = strategy_files_router.list_strategy_files_endpoint()

    assert [entry["valid"] for entry in payload["strategy_files"]] == [True]


def test_a_frozen_version_is_served_with_its_source_and_parameters(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    run_id = _start()
    version_id = str(_row(run_jobs_sync, run_id).strategy_version_id)

    with run_jobs_sync.harness_session_factory() as session:
        payload = StrategyVersionResponse.model_validate(
            strategy_files_router.get_strategy_version(version_id, session=session)
        ).model_dump(mode="json")

    assert payload["version_id"] == version_id
    assert payload["strategy_id"] == "simple_ma"
    assert payload["class_name"] == "SimpleMa"
    assert payload["source_files"][0]["path"] == "simple_ma.py"
    assert payload["source_files"][0]["content"] == SIMPLE_MA
    assert {spec["name"] for spec in payload["parameters"]} == {"length", "use_filter", "entry_after"}


def test_an_unknown_version_is_not_found(run_jobs_sync, strategy_env):
    with run_jobs_sync.harness_session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            strategy_files_router.get_strategy_version(str(uuid.uuid4()), session=session)
        assert caught.value.status_code == 404
        with pytest.raises(HTTPException) as malformed:
            strategy_files_router.get_strategy_version("not-a-uuid", session=session)
        assert malformed.value.status_code == 404


@pytest.mark.parametrize("path", GUARDED_ROUTES)
def test_workflows_that_run_registry_strategies_refuse_strategy_files(path, monkeypatch):
    client = TestClient(app)
    body = {"backtest": {"symbol": "WIN$N", "timeframe": "D1", "strategy_file": "simple_ma"}}

    response = client.post(path, json=body)

    assert response.status_code == 422
    assert "POST /api/v1/backtest" in response.json()["detail"]


def test_a_strategy_file_request_cannot_also_name_a_version_strategy_or_entries():
    with pytest.raises(ValueError, match="mutually exclusive"):
        BacktestJobRequest(symbol="WIN$N", strategy_file="simple_ma", strategy_version_id=str(uuid.uuid4()))
    with pytest.raises(ValueError, match="mutually exclusive with strategy"):
        BacktestJobRequest.model_validate({"symbol": "WIN$N", "strategy_file": "simple_ma", "strategy": "MACrossover"})
    with pytest.raises(ValueError, match="mutually exclusive with entries"):
        BacktestJobRequest.model_validate({"symbol": "WIN$N", "strategy_file": "simple_ma", "entries": []})
