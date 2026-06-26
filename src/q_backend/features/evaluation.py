"""Feature evaluation metrics — IC, Rank IC, MI, stability, regime robustness (WO133).

Pure compute service: no DB, HTTP, or file I/O. Alignment and embargo helpers come
from ``targets.py``; leakage is flagged via manifest + optional ``assert_causal``.

Mutual information uses ``sklearn.feature_selection.mutual_info_regression`` with
``random_state=0`` (scikit-learn is a project dependency). If sklearn were absent,
the fallback would be a deterministic binned-histogram estimator — see
``_mutual_info``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.feature_selection import mutual_info_regression

from q_backend.backtesting.technical_indicators import compute_realized_vol
from q_backend.features.compute import compute_feature
from q_backend.features.leakage import LeakageError, assert_causal
from q_backend.features.matrix import FeatureMatrix
from q_backend.features.registry import get_feature_spec
from q_backend.features.targets import (
    DEFAULT_VOL_WINDOW,
    align_feature_target,
)

MIN_OBS = 100
_EPS = 1e-12
_MI_RANDOM_STATE = 0

_REGIME_LABELS: dict[int, list[str]] = {
    1: ["all"],
    2: ["low", "high"],
    3: ["low", "mid", "high"],
}


@dataclass(frozen=True)
class FeatureEvaluation:
    feature_id: str
    target: str
    ic: float
    rank_ic: float
    mutual_info: float
    stability: float
    regime_ics: dict[str, float]
    n_obs: int
    leakage_status: str


def _regime_labels(n_regimes: int) -> list[str]:
    if n_regimes in _REGIME_LABELS:
        return _REGIME_LABELS[n_regimes]
    return [f"q{i}" for i in range(n_regimes)]


def _nan_evaluation(
    feature_id: str,
    target: str,
    n_obs: int,
    leakage_status: str,
    *,
    regimes: int,
) -> FeatureEvaluation:
    regime_ics = {label: float("nan") for label in _regime_labels(regimes)}
    return FeatureEvaluation(
        feature_id=feature_id,
        target=target,
        ic=float("nan"),
        rank_ic=float("nan"),
        mutual_info=float("nan"),
        stability=float("nan"),
        regime_ics=regime_ics,
        n_obs=n_obs,
        leakage_status=leakage_status,
    )


def _pearson_ic(feature: pd.Series, target: pd.Series) -> float:
    if len(feature) < 2:
        return float("nan")
    if feature.std() == 0 or target.std() == 0:
        return float("nan")
    return float(feature.corr(target, method="pearson"))


def _rank_ic(feature: pd.Series, target: pd.Series) -> float:
    if len(feature) < 2:
        return float("nan")
    if feature.std() == 0 or target.std() == 0:
        return float("nan")
    return float(feature.corr(target, method="spearman"))


def _mutual_info(feature: pd.Series, target: pd.Series) -> float:
    x = feature.to_numpy(dtype=float).reshape(-1, 1)
    y = target.to_numpy(dtype=float)
    if len(y) < 2:
        return float("nan")
    mi = mutual_info_regression(x, y, random_state=_MI_RANDOM_STATE)
    return float(mi[0])


def _binned_mutual_info(feature: pd.Series, target: pd.Series, *, bins: int = 16) -> float:
    """Deterministic histogram MI fallback when sklearn is unavailable."""
    x = feature.to_numpy(dtype=float)
    y = target.to_numpy(dtype=float)
    if len(x) < 2:
        return float("nan")
    x_edges = np.histogram_bin_edges(x, bins=bins)
    y_edges = np.histogram_bin_edges(y, bins=bins)
    joint, _, _ = np.histogram2d(x, y, bins=[x_edges, y_edges])
    pxy = joint / joint.sum()
    px = pxy.sum(axis=1)
    py = pxy.sum(axis=0)
    nz = pxy > 0
    mi = np.sum(pxy[nz] * np.log(pxy[nz] / (px[:, None] * py[None, :])[nz]))
    return float(mi)


def _time_windows(index: pd.Index, n_windows: int) -> list[pd.Index]:
    n = len(index)
    if n == 0 or n_windows <= 0:
        return []
    edges = np.linspace(0, n, n_windows + 1, dtype=int)
    return [
        index[edges[i] : edges[i + 1]]
        for i in range(n_windows)
        if edges[i + 1] > edges[i]
    ]


def _stability_score(window_rank_ics: list[float]) -> float:
    valid = [value for value in window_rank_ics if not math.isnan(value)]
    if len(valid) < 2:
        return float("nan")
    mean = float(np.mean(valid))
    std = float(np.std(valid))
    score = 1.0 - (std / (abs(mean) + _EPS))
    return float(np.clip(score, 0.0, 1.0))


def assign_regime_buckets(vol: pd.Series, n_regimes: int) -> pd.Series:
    """Assign causal expanding-quantile regime labels (vol at t uses vol[:t+1] only)."""
    labels = _regime_labels(n_regimes)
    buckets = pd.Series(index=vol.index, dtype=object)
    values = vol.to_numpy(dtype=float)
    for i in range(len(vol)):
        prefix = values[: i + 1]
        valid = prefix[~np.isnan(prefix)]
        if len(valid) < n_regimes:
            buckets.iloc[i] = np.nan
            continue
        current = values[i]
        if np.isnan(current):
            buckets.iloc[i] = np.nan
            continue
        quantiles = np.quantile(valid, np.linspace(0.0, 1.0, n_regimes + 1))
        inner = quantiles[1:-1]
        bucket_idx = int(np.searchsorted(inner, current, side="right"))
        bucket_idx = min(bucket_idx, len(labels) - 1)
        buckets.iloc[i] = labels[bucket_idx]
    return buckets


def _compute_regime_ics(
    feature: pd.Series,
    target: pd.Series,
    close: pd.Series,
    *,
    regimes: int,
    vol_window: int = DEFAULT_VOL_WINDOW,
) -> dict[str, float]:
    labels = _regime_labels(regimes)
    close_aligned = close.reindex(feature.index)
    vol = compute_realized_vol(close_aligned.sort_index(), vol_window)
    vol = vol.reindex(feature.index)
    buckets = assign_regime_buckets(vol, regimes)
    frame = pd.DataFrame({"feature": feature, "target": target, "bucket": buckets}).dropna(
        subset=["feature", "target", "bucket"]
    )
    result: dict[str, float] = {}
    for label in labels:
        subset = frame.loc[frame["bucket"] == label]
        if len(subset) < 2:
            result[label] = float("nan")
        else:
            result[label] = _rank_ic(subset["feature"], subset["target"])
    return result


def evaluate_feature(
    feature: pd.Series,
    target: pd.Series,
    *,
    n_windows: int = 6,
    regimes: int = 3,
    close: pd.Series | None = None,
) -> FeatureEvaluation:
    """Score one aligned feature column against a target series."""
    aligned_feature, aligned_target = align_feature_target(feature, target)
    n_obs = len(aligned_feature)
    target_name = str(target.name or "target")
    feature_id = str(feature.name or "feature")
    leakage_status = "unverified"

    if n_obs < MIN_OBS:
        return _nan_evaluation(
            feature_id,
            target_name,
            n_obs,
            leakage_status,
            regimes=regimes,
        )

    ic = _pearson_ic(aligned_feature, aligned_target)
    rank_ic = _rank_ic(aligned_feature, aligned_target)
    mutual_info = _mutual_info(aligned_feature, aligned_target)

    windows = _time_windows(aligned_feature.index, n_windows)
    window_rank_ics = [
        _rank_ic(aligned_feature.loc[window], aligned_target.loc[window])
        for window in windows
        if len(window) >= 2
    ]
    stability = _stability_score(window_rank_ics)

    if close is not None:
        regime_ics = _compute_regime_ics(
            aligned_feature, aligned_target, close, regimes=regimes
        )
    else:
        regime_ics = {label: float("nan") for label in _regime_labels(regimes)}

    return FeatureEvaluation(
        feature_id=feature_id,
        target=target_name,
        ic=ic,
        rank_ic=rank_ic,
        mutual_info=mutual_info,
        stability=stability,
        regime_ics=regime_ics,
        n_obs=n_obs,
        leakage_status=leakage_status,
    )


def _manifest_entry_for_feature(
    manifest: dict[str, Any], feature_id: str
) -> dict[str, Any] | None:
    for entry in manifest.get("features", []):
        if entry.get("feature_id") == feature_id:
            return entry
    return None


def _resolve_leakage_status(
    manifest_entry: dict[str, Any] | None,
    bars: pd.DataFrame | None,
) -> str:
    if manifest_entry is None:
        status = "unverified"
    else:
        status = str(manifest_entry.get("leakage_status", "unverified"))

    if bars is None or manifest_entry is None:
        return status

    spec = get_feature_spec(
        manifest_entry["name"], version=manifest_entry.get("version")
    )
    params = manifest_entry.get("params", {})
    n = len(bars)
    if n == 0:
        return status

    sample_indices = sorted(
        {min(50, n - 1), min(120, n - 1), min(200, n - 1), n - 1}
    )
    sample_indices = [idx for idx in sample_indices if idx >= 0]

    def _compute(frame: pd.DataFrame):
        return compute_feature(frame, spec, params)

    try:
        assert_causal(_compute, bars, sample_indices=sample_indices)
    except LeakageError:
        return "suspect"
    return status


def evaluate_matrix(
    matrix: FeatureMatrix,
    target: pd.Series,
    *,
    n_windows: int = 6,
    regimes: int = 3,
    close: pd.Series | None = None,
    bars: pd.DataFrame | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[FeatureEvaluation]:
    """Evaluate every feature column in a matrix against one target.

    ``progress_callback(done, total)`` is invoked after each feature is scored so
    callers can surface live progress (the per-feature loop is ~all the runtime)."""
    results: list[FeatureEvaluation] = []
    target_name = str(target.name or "target")
    feature_ids = sorted(matrix.frame.columns)
    total = len(feature_ids)

    for index, feature_id in enumerate(feature_ids, start=1):
        series = matrix.frame[feature_id]
        series = series.rename(feature_id)
        manifest_entry = _manifest_entry_for_feature(matrix.manifest, feature_id)
        leakage_status = _resolve_leakage_status(manifest_entry, bars)

        evaluation = evaluate_feature(
            series,
            target,
            n_windows=n_windows,
            regimes=regimes,
            close=close,
        )
        results.append(
            FeatureEvaluation(
                feature_id=evaluation.feature_id,
                target=target_name,
                ic=evaluation.ic,
                rank_ic=evaluation.rank_ic,
                mutual_info=evaluation.mutual_info,
                stability=evaluation.stability,
                regime_ics=evaluation.regime_ics,
                n_obs=evaluation.n_obs,
                leakage_status=leakage_status,
            )
        )
        if progress_callback is not None:
            progress_callback(index, total)
    return results
