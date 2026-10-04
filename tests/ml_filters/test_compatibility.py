"""Central ML-filter request and model compatibility rules (Q-087)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.ml_filters import service
from q_backend.ml_filters.compatibility import (
    MLFilterCompatibilityError,
    MLFilterModelUnavailableError,
    MLFilterRequestError,
    compatibility_fingerprint,
    source_equivalent_config,
    validate_filter_compatibility,
    validate_filter_request_shape,
)
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import create_ml_filter_model_version

MODEL_ID = "a" * 64
DATASET_ID = "b" * 64
TRAIN_END = "2024-01-31T00:00:00+00:00"
NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)
BASE = {
    "symbol": "WIN$",
    "timeframe": "M5",
    "start": "2024-02-01T00:00:00Z",
    "end": "2024-05-01T00:00:00Z",
    "point_value": 0.2,
    "strategy": "MACrossoverMLFilter",
    "strategy_params": {"short_period": 10, "long_period": 30, "stop_loss_atr": 2.0},
    "ml_filter": {"model_version_id": MODEL_ID, "threshold": 0.6},
}


def _config(**overrides) -> dict:
    return BacktestRequest.model_validate({**BASE, **overrides}).model_dump(mode="json")


def _manifest(config: dict | None = None, **overrides) -> dict:
    config = config or _config()
    return {
        "model_version_id": MODEL_ID,
        "dataset_id": DATASET_ID,
        "algorithm": "lightgbm",
        "train_end": TRAIN_END,
        "compatibility_fingerprint": compatibility_fingerprint(source_equivalent_config(config)),
        **overrides,
    }


def test_matching_configuration_is_accepted_and_filterless_requests_are_ignored():
    config = _config()
    validate_filter_compatibility(config, _manifest(config), now=NOW)
    validate_filter_request_shape(
        BacktestRequest.model_validate({"symbol": "WIN$", "strategy": "RSIMeanReversion"}).model_dump(mode="json")
    )
    # Original MACrossover with no filter is a normal, valid backtest.
    validate_filter_request_shape(BacktestRequest.model_validate({"symbol": "WIN$"}).model_dump(mode="json"))


def test_fingerprint_treats_the_variant_as_its_original_source_strategy():
    original = {**_config(strategy="MACrossover", ml_filter=None)}
    assert compatibility_fingerprint(source_equivalent_config(_config())) == compatibility_fingerprint(original)
    entries = [{"strategy": "MACrossoverMLFilter", "params": BASE["strategy_params"]}]
    config = _config(strategy="MACrossover", entries=entries, strategy_params={})
    source = {**config, "entries": [{**entries[0], "strategy": "MACrossover"}]}
    assert compatibility_fingerprint(source_equivalent_config(config)) == compatibility_fingerprint(source)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"strategy": "MACrossover"}, "cannot be combined"),
        ({"ml_filter": None}, "requires an ml_filter"),
        (
            {
                "strategy": "MACrossover",
                "strategy_params": {},
                "entries": [
                    {"strategy": "MACrossoverMLFilter", "params": {}},
                    {"strategy": "RSIMeanReversion", "params": {}},
                ],
            },
            "exactly one",
        ),
        ({"entry_manager": {"kind": "majority", "params": {"vote_threshold": 1}}}, "'or' entry manager"),
        ({"entry_manager": {"kind": "or", "params": {"x": 1}}}, "'or' entry manager"),
        ({"engine": "tick"}, "candle engine only"),
    ],
)
def test_unsupported_compositions_fail_before_a_model_is_resolved(overrides, message):
    with pytest.raises(MLFilterRequestError, match=message):
        validate_filter_request_shape(_config(**overrides))


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan")])
def test_threshold_must_be_finite_and_within_unit_interval(threshold):
    with pytest.raises(ValueError):
        BacktestRequest.model_validate({**BASE, "ml_filter": {"model_version_id": MODEL_ID, "threshold": threshold}})
    shaped = _config()
    shaped["ml_filter"]["threshold"] = threshold
    with pytest.raises(MLFilterRequestError, match="threshold"):
        validate_filter_request_shape(shaped)


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_params": {**BASE["strategy_params"], "short_period": 12}},
        {"strategy_params": {**BASE["strategy_params"], "stop_loss_atr": 3.0}},
        {"costs": {"commission_per_contract": 1.0}},
        {"position_sizing": {"type": "fixed_quantity", "quantity": 3}},
        {"day_trade": True},
        {"day_trade_close_time": "17:30"},
        {"point_value": 1.0},
        {"initial_capital": 50000.0},
        {"timeframe": "M15"},
        {"symbol": "WDO$"},
    ],
)
def test_settings_that_differ_from_the_training_source_conflict(overrides):
    trained = _manifest()
    with pytest.raises(MLFilterCompatibilityError, match="differ from the source"):
        validate_filter_compatibility(_config(**overrides), trained, now=NOW)


def test_manifest_for_a_different_version_is_rejected():
    with pytest.raises(MLFilterCompatibilityError, match="requested model version"):
        validate_filter_compatibility(_config(), _manifest(model_version_id="c" * 64), now=NOW)


def test_range_ending_inside_training_conflicts_but_warm_up_before_train_end_is_allowed():
    warm_up = _config(start="2023-06-01T00:00:00Z", end="2024-05-01T00:00:00Z")
    validate_filter_compatibility(warm_up, _manifest(warm_up), now=NOW)

    overlapping = _config(start="2023-06-01T00:00:00Z", end="2024-01-31T00:00:00Z")
    with pytest.raises(MLFilterCompatibilityError, match="train_end"):
        validate_filter_compatibility(overlapping, _manifest(overlapping), now=NOW)


def test_naive_range_is_interpreted_in_brasilia_time():
    naive = _config(start="2024-02-01T00:00:00", end="2024-01-31T01:00:00")
    # 01:00 Brasilia on 2024-01-31 is 04:00 UTC: after train_end 00:00 UTC.
    validate_filter_compatibility(naive, _manifest(naive), now=NOW)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)

    @contextmanager
    def scope():
        yield session
        session.commit()

    yield session, scope
    session.close()
    engine.dispose()


def _register_model(session, *, status: str = "ready") -> None:
    row = create_ml_filter_model_version(
        session,
        model_version_id=MODEL_ID,
        dataset_id=DATASET_ID,
        source_run_id="source",
        algorithm="lightgbm",
        artifact_path="models/path",
        manifest_path="models/path/manifest.json",
        summary={},
    )
    row.status = status
    session.commit()


def test_manifest_lookup_requires_a_ready_registered_model(db):
    session, scope = db
    with patch.object(service, "session_scope", scope):
        with pytest.raises(MLFilterModelUnavailableError, match="was not found"):
            service.read_filter_manifest(MODEL_ID)
        _register_model(session, status="deleted")
        with pytest.raises(MLFilterModelUnavailableError, match="was not found"):
            service.read_filter_manifest(MODEL_ID)


def test_manifest_lookup_merges_dataset_fields_and_reports_corrupt_artifacts(db):
    session, scope = db
    _register_model(session)
    manifest = {"model_version_id": MODEL_ID, "dataset_id": DATASET_ID}
    dataset = {"compatibility_fingerprint": "f" * 64, "train_end": TRAIN_END}
    with (
        patch.object(service, "session_scope", scope),
        patch.object(service, "read_model_manifest", return_value=manifest),
        patch.object(service, "read_dataset_manifest", return_value=dataset),
    ):
        merged = service.read_filter_manifest(MODEL_ID)
    assert merged["train_end"] == TRAIN_END and merged["compatibility_fingerprint"] == "f" * 64

    with (
        patch.object(service, "session_scope", scope),
        patch.object(
            service, "read_model_manifest", side_effect=ValueError("Published ML filter model checksum mismatch")
        ),
    ):
        with pytest.raises(MLFilterModelUnavailableError, match="checksum mismatch"):
            service.read_filter_manifest(MODEL_ID)


def test_resolve_loads_the_pipeline_only_after_compatibility_passes():
    config = _config()
    with (
        patch.object(
            service, "read_filter_manifest", return_value=_manifest(config, train_end="2030-01-01T00:00:00+00:00")
        ),
        patch.object(service, "load_model_version") as load,
    ):
        with pytest.raises(MLFilterCompatibilityError):
            service.resolve_filter_model(config)
    load.assert_not_called()

    with (
        patch.object(service, "read_filter_manifest", return_value=_manifest(config)),
        patch.object(
            service, "load_model_version", side_effect=ValueError("Published ML filter model checksum mismatch")
        ),
    ):
        with pytest.raises(MLFilterModelUnavailableError, match="corrupt"):
            service.resolve_filter_model(config)
