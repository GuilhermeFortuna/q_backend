"""Regression tests for the shared train pipeline window builder (WO147)."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import q_backend.neural.training_pipeline as tp
from q_backend.features.matrix import FeatureMatrix


def test_build_training_feature_window_renames_feature_ids_to_names(monkeypatch) -> None:
    """The encoder matches on bare feature names, so the window the pipeline
    hands it must use names — not the ``feature_id`` columns ``build_feature_matrix``
    emits (e.g. ``rsi.v1.<hash>``). Without the rename, every requested feature
    reads as 'missing from window'."""
    frame = pd.DataFrame(
        {
            "rsi.v1.abc123": [1.0, 2.0, 3.0],
            "macd.v1.def456": [4.0, 5.0, 6.0],
        }
    )
    manifest = {
        "features": [
            {"feature_id": "rsi.v1.abc123", "name": "rsi"},
            {"feature_id": "macd.v1.def456", "name": "macd"},
        ]
    }

    def _fake_build_feature_matrix(symbol, timeframe, start, end, requests, use_cache=True):
        return FeatureMatrix(matrix_id="m1", frame=frame, manifest=manifest)

    monkeypatch.setattr(tp, "build_feature_matrix", _fake_build_feature_matrix)

    window = tp.build_training_feature_window(
        "SYN",
        "H1",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        ("rsi", "macd"),
    )

    assert sorted(window.columns) == ["macd", "rsi"]
