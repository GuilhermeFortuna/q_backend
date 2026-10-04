from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.ml_filters.adapters import create_classifier
from q_backend.ml_filters.artifacts import (
    load_dataset_snapshot,
    load_model_version,
    save_dataset_artifacts,
    save_model_artifact,
)
from q_backend.ml_filters.config import EntrySample, MLFilterDataset


def test_dataset_snapshot_is_atomic_and_checksums_are_enforced(tmp_path, monkeypatch):
    from q_backend.ml_filters import artifacts

    monkeypatch.setattr(artifacts, "lake_root", lambda: tmp_path)
    dataset_id = "a" * 64
    dataset = MLFilterDataset(
        dataset_id=dataset_id,
        source_run_id="run",
        bars=pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2026-01-01T00:00:00Z"])),
        samples=(
            EntrySample(
                "train",
                0,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
                1,
                1,
                10.0,
            ),
        ),
        selected_features=("close", "side"),
        train_end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        validation_end=datetime(2026, 1, 3, tzinfo=timezone.utc),
        rejections={"train": {}, "validation": {}, "lockbox": {}},
        source_config={"engine": "legacy"},
        source_config_revision="legacy-unversioned",
        compatibility_fingerprint="b" * 64,
        bars_checksum="c" * 64,
        trades_checksum="d" * 64,
    )

    paths = save_dataset_artifacts(dataset)
    _manifest, bars, samples, _source_config = load_dataset_snapshot(dataset_id)
    assert len(bars) == len(samples) == 1
    assert save_dataset_artifacts(dataset) == paths
    (tmp_path / paths["bars"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_dataset_snapshot(dataset_id)


def test_model_artifact_round_trip_checks_checksum_and_runtime(tmp_path, monkeypatch):
    from q_backend.ml_filters import artifacts

    monkeypatch.setattr(artifacts, "lake_root", lambda: tmp_path)
    X = pd.DataFrame({"close": [0.0, 0.2, 1.0, 1.2], "side": [1, -1, 1, -1]})
    model = create_classifier("random_forest", {"n_estimators": 4}).fit(X, [0, 0, 1, 1])
    record = save_model_artifact(
        "dataset",
        model,
        selected_features=("close", "side"),
        training_label_availability_cutoff="2026-01-01T00:00:00Z",
    )

    loaded = load_model_version(record["model_version_id"])
    np.testing.assert_allclose(model.predict_good_entry_probability(X), loaded.predict_good_entry_probability(X))
    model_file = tmp_path / record["artifact_path"] / "model.joblib"
    model_file.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_model_version(record["model_version_id"])


def test_model_artifact_identity_changes_with_feature_order(tmp_path, monkeypatch):
    from q_backend.ml_filters import artifacts

    monkeypatch.setattr(artifacts, "lake_root", lambda: tmp_path)
    X = pd.DataFrame({"close": [0.0, 0.2, 1.0, 1.2], "side": [1, -1, 1, -1]})
    model_a = create_classifier("random_forest", {"n_estimators": 3}).fit(X, [0, 0, 1, 1])
    model_b = create_classifier("random_forest", {"n_estimators": 3}).fit(X[["side", "close"]], [0, 0, 1, 1])
    a = save_model_artifact(
        "dataset", model_a, selected_features=("close", "side"), training_label_availability_cutoff="cutoff"
    )
    b = save_model_artifact(
        "dataset", model_b, selected_features=("side", "close"), training_label_availability_cutoff="cutoff"
    )

    assert a["model_version_id"] != b["model_version_id"]
