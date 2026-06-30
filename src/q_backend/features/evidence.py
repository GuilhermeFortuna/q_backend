"""Profile-scoped feature evidence evaluation and admission (WO162)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import numpy as np
import pandas as pd

from q_backend.features.evaluation import (
    MIN_OBS,
    _pearson_ic,
    _rank_ic,
    _time_windows,
    evaluate_feature,
)
from q_backend.features.scoring import RedundancyCluster
from q_backend.features.split_manifest import SplitManifest, slice_segment
from q_backend.features.targets import TargetSpec, align_feature_target, purge_embargo

# NB: `q_backend.optimization.dsr` is imported lazily inside the functions below.
# A module-level import here forms a cycle (features.evidence -> optimization.dsr ->
# optimization/__init__ -> feature_admission -> features.evidence) that makes
# `import q_backend.features.evidence` fail when it is the first module imported.

EvidenceDecision = Literal["admitted", "rejected", "inconclusive"]

DEFAULT_EVIDENCE_THRESHOLDS: dict[str, float | int] = {
    "min_obs": MIN_OBS,
    "min_folds": 3,
    "min_valid_folds": 2,
    "min_abs_rank_ic": 0.02,
    "min_sign_consistency": 0.67,
    "max_rank_ic_dispersion": 0.20,
    "min_deflated_score": 0.50,
    "redundancy_correlation_threshold": 0.90,
    "n_permutations": 25,
    "permutation_block_multiplier": 5,
}


@dataclass(frozen=True)
class FoldDiagnostic:
    fold_index: int
    ic: float
    rank_ic: float
    n_obs: int


@dataclass(frozen=True)
class FeatureEvidenceKey:
    profile_id: str
    profile_version: int
    feature_id: str
    feature_version: int
    target: str
    horizon: int
    split_manifest_hash: str

    def composite_key(self) -> str:
        payload = json.dumps(
            {
                "profile_id": self.profile_id,
                "profile_version": self.profile_version,
                "feature_id": self.feature_id,
                "feature_version": self.feature_version,
                "target": self.target,
                "horizon": self.horizon,
                "split_manifest_hash": self.split_manifest_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class FeatureEvidence:
    key: FeatureEvidenceKey
    feature_name: str
    node_kind: str | None
    ic: float
    rank_ic: float
    mutual_info: float
    sign_consistency: float
    median_effect: float
    effect_dispersion: float
    n_obs: int
    fold_diagnostics: list[FoldDiagnostic]
    regime_ics: dict[str, float]
    permutation_null_floor: float
    deflated_score: float
    decision: EvidenceDecision
    rejection_reasons: list[str] = field(default_factory=list)
    leakage_status: str = "unverified"
    is_representative: bool = True
    cluster_id: int = 0
    data_fingerprint: str = ""
    attempted_feature_count: int = 1
    diagnostics_artifact_id: str | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "composite_key": self.key.composite_key(),
            "profile_id": self.key.profile_id,
            "profile_version": self.key.profile_version,
            "feature_id": self.key.feature_id,
            "feature_version": self.key.feature_version,
            "feature_name": self.feature_name,
            "node_kind": self.node_kind,
            "target": self.key.target,
            "horizon": self.key.horizon,
            "split_manifest_hash": self.key.split_manifest_hash,
            "data_fingerprint": self.data_fingerprint,
            "ic": self.ic,
            "rank_ic": self.rank_ic,
            "mutual_info": self.mutual_info,
            "sign_consistency": self.sign_consistency,
            "median_effect": self.median_effect,
            "effect_dispersion": self.effect_dispersion,
            "n_obs": self.n_obs,
            "permutation_null_floor": self.permutation_null_floor,
            "deflated_score": self.deflated_score,
            "decision": self.decision,
            "rejection_reasons": list(self.rejection_reasons),
            "leakage_status": self.leakage_status,
            "is_representative": self.is_representative,
            "cluster_id": self.cluster_id,
            "attempted_feature_count": self.attempted_feature_count,
            "diagnostics_artifact_id": self.diagnostics_artifact_id,
        }

    def to_diagnostics_dict(self) -> dict[str, Any]:
        return {
            **self.to_summary_dict(),
            "fold_diagnostics": [
                {
                    "fold_index": item.fold_index,
                    "ic": item.ic,
                    "rank_ic": item.rank_ic,
                    "n_obs": item.n_obs,
                }
                for item in self.fold_diagnostics
            ],
            "regime_ics": dict(self.regime_ics),
        }


def normalize_node_kind(feature_key: str) -> str:
    """Map catalog / genome keys to a canonical node kind."""
    if feature_key.startswith("feature."):
        return feature_key
    return f"feature.{feature_key}"


def normalize_feature_name(feature_key: str) -> str:
    if feature_key.startswith("feature."):
        return feature_key[len("feature.") :]
    return feature_key


def merge_evidence_thresholds(profile_thresholds: dict[str, Any] | None) -> dict[str, float | int]:
    merged: dict[str, float | int] = dict(DEFAULT_EVIDENCE_THRESHOLDS)
    if profile_thresholds:
        merged.update(profile_thresholds)
    return merged


def block_permute_series(
    series: pd.Series,
    *,
    block_size: int,
    rng: np.random.Generator,
) -> pd.Series:
    """Permute contiguous blocks to preserve local autocorrelation."""
    values = series.to_numpy(dtype=float)
    n = len(values)
    block_size = max(2, int(block_size))
    if n < block_size * 2:
        return series.copy()
    n_blocks = n // block_size
    blocks = [values[i * block_size : (i + 1) * block_size] for i in range(n_blocks)]
    remainder = values[n_blocks * block_size :]
    permuted_blocks = [blocks[i] for i in rng.permutation(n_blocks)]
    out = np.concatenate(permuted_blocks + ([remainder] if len(remainder) else []))
    return pd.Series(out[:n], index=series.index)


def _sign_consistency(fold_rank_ics: list[float]) -> float:
    valid = [value for value in fold_rank_ics if not math.isnan(value) and value != 0.0]
    if len(valid) < 2:
        return 0.0
    positive = sum(1 for value in valid if value > 0)
    negative = len(valid) - positive
    return max(positive, negative) / len(valid)


def _median_and_dispersion(values: list[float]) -> tuple[float, float]:
    valid = [value for value in values if not math.isnan(value)]
    if not valid:
        return float("nan"), float("nan")
    median = float(np.median(valid))
    dispersion = float(np.std(valid))
    return median, dispersion


def _purged_fold_metrics(
    feature: pd.Series,
    target: pd.Series,
    *,
    n_folds: int,
    embargo: int,
) -> list[FoldDiagnostic]:
    aligned_feature, aligned_target = align_feature_target(feature, target)
    index = aligned_feature.index
    windows = _time_windows(index, n_folds)
    metrics: list[FoldDiagnostic] = []
    split_points = np.linspace(0, len(index), n_folds + 1, dtype=int)

    for fold_index, window in enumerate(windows):
        if len(window) < 2:
            continue
        window_pos = index.get_indexer(window)
        split_point = int(split_points[fold_index + 1])
        train_idx, test_idx = purge_embargo(index, split_point, embargo)
        test_window = index.intersection(window)
        if len(test_window) < 2:
            continue
        fold_feature = aligned_feature.loc[test_window]
        fold_target = aligned_target.loc[test_window]
        metrics.append(
            FoldDiagnostic(
                fold_index=fold_index,
                ic=_pearson_ic(fold_feature, fold_target),
                rank_ic=_rank_ic(fold_feature, fold_target),
                n_obs=len(fold_feature),
            )
        )
    return metrics


def permutation_null_floor(
    feature: pd.Series,
    target: pd.Series,
    *,
    num_trials: int,
    n_permutations: int,
    block_size: int,
    seed: int = 0,
) -> tuple[float, list[float]]:
    """Block-permutation null for |rank IC| plus search-budget floor."""
    aligned_feature, aligned_target = align_feature_target(feature, target)
    if len(aligned_feature) < 4:
        return float("nan"), []

    rng = np.random.default_rng(seed)
    null_values: list[float] = []
    for _ in range(max(1, n_permutations)):
        permuted = block_permute_series(
            aligned_feature, block_size=block_size, rng=rng
        )
        null_values.append(abs(_rank_ic(permuted, aligned_target)))

    null_mean = float(np.median(null_values))
    null_std = float(np.std(null_values))
    if math.isnan(null_std) or null_std <= 0.0:
        null_std = 1.0 / math.sqrt(max(len(aligned_feature), 2))

    from q_backend.optimization.dsr import expected_max_sharpe

    floor = expected_max_sharpe(
        max(1, num_trials),
        mu=null_mean,
        sigma=null_std,
    )
    return floor, null_values


def deflated_ic_score(
    observed_rank_ic: float,
    *,
    num_trials: int,
    n_obs: int,
    null_floor: float,
) -> float:
    from q_backend.optimization.dsr import _norm_cdf, expected_max_sharpe

    if n_obs < 2 or math.isnan(observed_rank_ic):
        return 0.0
    abs_obs = abs(observed_rank_ic)
    trial_floor = expected_max_sharpe(
        max(1, num_trials),
        mu=null_floor if not math.isnan(null_floor) else 0.0,
        sigma=1.0 / math.sqrt(max(n_obs, 2)),
    )
    effective = abs_obs - trial_floor
    if effective <= 0.0:
        return 0.0
    z = effective * math.sqrt(n_obs - 1)
    return float(_norm_cdf(z))


def decide_admission(
    *,
    n_obs: int,
    fold_diagnostics: list[FoldDiagnostic],
    rank_ic: float,
    sign_consistency: float,
    effect_dispersion: float,
    deflated_score: float,
    leakage_status: str,
    is_representative: bool,
    thresholds: dict[str, float | int],
) -> tuple[EvidenceDecision, list[str]]:
    reasons: list[str] = []
    min_obs = int(thresholds["min_obs"])
    min_folds = int(thresholds["min_folds"])
    min_valid_folds = int(thresholds["min_valid_folds"])

    if n_obs < min_obs:
        return "inconclusive", [f"insufficient_observations:{n_obs}<{min_obs}"]
    if len(fold_diagnostics) < min_folds:
        return "inconclusive", [f"insufficient_folds:{len(fold_diagnostics)}<{min_folds}"]

    valid_folds = [
        item for item in fold_diagnostics if not math.isnan(item.rank_ic) and item.n_obs >= 2
    ]
    if len(valid_folds) < min_valid_folds:
        return "inconclusive", [
            f"insufficient_valid_folds:{len(valid_folds)}<{min_valid_folds}"
        ]

    if leakage_status == "suspect":
        return "rejected", ["leakage_suspect"]

    if not is_representative:
        return "rejected", ["redundant_non_representative"]

    min_abs_rank_ic = float(thresholds["min_abs_rank_ic"])
    if math.isnan(rank_ic) or abs(rank_ic) < min_abs_rank_ic:
        reasons.append(f"weak_rank_ic:{rank_ic}")

    min_sign = float(thresholds["min_sign_consistency"])
    if sign_consistency < min_sign:
        reasons.append(f"unstable_sign:{sign_consistency:.3f}<{min_sign}")

    max_disp = float(thresholds["max_rank_ic_dispersion"])
    if not math.isnan(effect_dispersion) and effect_dispersion > max_disp:
        reasons.append(f"high_dispersion:{effect_dispersion:.3f}>{max_disp}")

    min_deflated = float(thresholds["min_deflated_score"])
    if deflated_score < min_deflated:
        reasons.append(f"low_deflated_score:{deflated_score:.3f}<{min_deflated}")

    if reasons:
        return "rejected", reasons
    return "admitted", []


def evaluate_feature_evidence(
    *,
    key: FeatureEvidenceKey,
    feature_name: str,
    feature: pd.Series,
    target: pd.Series,
    close: pd.Series | None,
    manifest: SplitManifest,
    evidence_bars: pd.DataFrame,
    thresholds: dict[str, float | int],
    attempted_feature_count: int,
    leakage_status: str = "clean",
    is_representative: bool = True,
    cluster_id: int = 0,
    node_kind: str | None = None,
    n_folds: int = 6,
    seed: int = 0,
) -> FeatureEvidence:
    """Evaluate one feature on the evidence segment only."""
    segment_bars = slice_segment(evidence_bars, manifest.evidence)
    if segment_bars.empty:
        return FeatureEvidence(
            key=key,
            feature_name=feature_name,
            node_kind=node_kind,
            ic=float("nan"),
            rank_ic=float("nan"),
            mutual_info=float("nan"),
            sign_consistency=0.0,
            median_effect=float("nan"),
            effect_dispersion=float("nan"),
            n_obs=0,
            fold_diagnostics=[],
            regime_ics={},
            permutation_null_floor=float("nan"),
            deflated_score=0.0,
            decision="inconclusive",
            rejection_reasons=["empty_evidence_segment"],
            leakage_status=leakage_status,
            is_representative=is_representative,
            cluster_id=cluster_id,
            data_fingerprint=manifest.data_fingerprint,
            attempted_feature_count=attempted_feature_count,
        )

    embargo = max(key.horizon, 1)
    segment_times = pd.to_datetime(segment_bars["time"], utc=True)
    mask = feature.index.isin(segment_times)
    segment_feature = feature.loc[mask]
    segment_target = target.reindex(segment_feature.index)
    if close is not None:
        segment_close = close.reindex(segment_feature.index)
    else:
        segment_close = None

    base_eval = evaluate_feature(
        segment_feature,
        segment_target,
        n_windows=n_folds,
        regimes=3,
        close=segment_close,
    )
    fold_diagnostics = _purged_fold_metrics(
        segment_feature,
        segment_target,
        n_folds=n_folds,
        embargo=embargo,
    )
    fold_rank_ics = [item.rank_ic for item in fold_diagnostics]
    sign_consistency = _sign_consistency(fold_rank_ics)
    median_effect, effect_dispersion = _median_and_dispersion(fold_rank_ics)

    block_size = max(embargo * int(thresholds["permutation_block_multiplier"]), 2)
    null_floor, _nulls = permutation_null_floor(
        segment_feature,
        segment_target,
        num_trials=attempted_feature_count,
        n_permutations=int(thresholds["n_permutations"]),
        block_size=block_size,
        seed=seed,
    )
    deflated_score = deflated_ic_score(
        base_eval.rank_ic,
        num_trials=attempted_feature_count,
        n_obs=base_eval.n_obs,
        null_floor=null_floor,
    )

    decision, reasons = decide_admission(
        n_obs=base_eval.n_obs,
        fold_diagnostics=fold_diagnostics,
        rank_ic=base_eval.rank_ic,
        sign_consistency=sign_consistency,
        effect_dispersion=effect_dispersion,
        deflated_score=deflated_score,
        leakage_status=leakage_status,
        is_representative=is_representative,
        thresholds=thresholds,
    )

    return FeatureEvidence(
        key=key,
        feature_name=feature_name,
        node_kind=node_kind,
        ic=base_eval.ic,
        rank_ic=base_eval.rank_ic,
        mutual_info=base_eval.mutual_info,
        sign_consistency=sign_consistency,
        median_effect=median_effect,
        effect_dispersion=effect_dispersion,
        n_obs=base_eval.n_obs,
        fold_diagnostics=fold_diagnostics,
        regime_ics=dict(base_eval.regime_ics),
        permutation_null_floor=null_floor,
        deflated_score=deflated_score,
        decision=decision,
        rejection_reasons=reasons,
        leakage_status=leakage_status,
        is_representative=is_representative,
        cluster_id=cluster_id,
        data_fingerprint=manifest.data_fingerprint,
        attempted_feature_count=attempted_feature_count,
    )


def profile_target_specs(horizons: list[int]) -> list[TargetSpec]:
    """Profile-aware forward-return targets for each configured horizon."""
    return [
        TargetSpec(name="fwd_return", horizon=horizon, kind="regression")
        for horizon in sorted(horizons)
        if horizon > 0
    ]


def apply_redundancy_decisions(
    evidences: list[FeatureEvidence],
    clusters: list[RedundancyCluster],
    scores: list,
) -> list[FeatureEvidence]:
    """Mark non-representatives rejected when a stronger peer exists."""
    representative_by_cluster = {
        cluster.cluster_id: cluster.representative for cluster in clusters
    }
    score_by_feature = {item.feature_id: item.global_score for item in scores}
    updated: list[FeatureEvidence] = []

    for evidence in evidences:
        representative = representative_by_cluster.get(
            evidence.cluster_id, evidence.key.feature_id
        )
        is_representative = evidence.key.feature_id == representative
        if evidence.decision != "admitted":
            updated.append(replace(evidence, is_representative=is_representative))
            continue
        if not is_representative:
            peer_score = score_by_feature.get(representative, 0.0)
            own_score = score_by_feature.get(evidence.key.feature_id, 0.0)
            if peer_score >= own_score:
                updated.append(
                    replace(
                        evidence,
                        is_representative=False,
                        decision="rejected",
                        rejection_reasons=["redundant_non_representative"],
                    )
                )
                continue
        updated.append(replace(evidence, is_representative=is_representative))
    return updated
