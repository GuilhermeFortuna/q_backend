"""Tests for WO162 profile-scoped feature evidence and admission."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from q_backend.features.evidence import (
    FeatureEvidence,
    FeatureEvidenceKey,
    decide_admission,
    deflated_ic_score,
    evaluate_feature_evidence,
    merge_evidence_thresholds,
    permutation_null_floor,
)
from q_backend.features.evidence_service import load_feature_evidence, persist_feature_evidence
from q_backend.features.matrix import FeatureMatrix
from q_backend.features.registry import feature_id, get_feature_spec
from q_backend.features.scoring import cluster_redundant
from q_backend.features.split_manifest import (
    assert_segments_disjoint,
    build_split_manifest,
    slice_segment,
)
from q_backend.features.targets import TargetSpec, compute_target
from q_backend.optimization.feature_admission import ProfileFeatureAdmissionResolver
from q_backend.optimization.hypothesis import (
    FailClosedFeatureAdmissionResolver,
    HypothesisCandidateProvider,
    RESEARCH_PROFILES,
)
from q_backend.optimization.models import (
    BacktestConfig,
    ObjectiveConfig,
    ObjectiveMode,
    StudyConfig,
)
from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.models import FeatureEvidenceRow
from q_backend.storage.lake.artifacts import read_feature_evidence


def _search_config(symbol: str = "CCM$", timeframe: str = "H1", strategies: list[str] | None = None):
    return StrategySearchConfig(
        backtest=BacktestConfig(
            symbol=symbol,
            timeframe=timeframe,
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 15),
            initial_capital=100_000.0,
            strategy="CompositeStrategy",
        ),
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, anchored=True),
        study=StudyConfig(name="test_evidence_study", n_trials=5, seed=42),
        strategies=strategies,
    )


def _synthetic_bars(n: int = 500, *, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.15, size=n)
    close = 100.0 + np.cumsum(steps)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": np.full(n, 1000.0),
        }
    )


def _manifest_and_segment(n: int = 500):
    bars = _synthetic_bars(n)
    manifest = build_split_manifest(bars, symbol="CCM$", timeframe="H1")
    assert_segments_disjoint(manifest)
    evidence_bars = slice_segment(bars, manifest.evidence)
    return bars, manifest, evidence_bars


def _evaluate_with_series(
    feature: pd.Series,
    target: pd.Series,
    *,
    manifest,
    evidence_bars,
    profile_id: str = "ccm_h1_swing",
    horizon: int = 6,
    attempted: int = 4,
    leakage_status: str = "clean",
    thresholds: dict | None = None,
):
    profile = RESEARCH_PROFILES[profile_id]
    spec = get_feature_spec("momentum")
    fid = feature_id(spec, {"lookback_bars": 8})
    key = FeatureEvidenceKey(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        feature_id=fid,
        feature_version=spec.version,
        target="fwd_return",
        horizon=horizon,
        split_manifest_hash=manifest.manifest_hash,
    )
    merged = merge_evidence_thresholds(profile.evidence_thresholds)
    if thresholds:
        merged.update(thresholds)
    return evaluate_feature_evidence(
        key=key,
        feature_name="momentum",
        feature=feature,
        target=target,
        close=pd.Series(evidence_bars["close"].to_numpy(), index=target.index),
        manifest=manifest,
        evidence_bars=evidence_bars,
        thresholds=merged,
        attempted_feature_count=attempted,
        leakage_status=leakage_status,
        node_kind="ind.momentum",
    )


def test_ohlcv_to_bars_normalizes_volume_for_compute():
    """Regression: OHLCV records carry tick_volume, not volume; the evidence loader
    must emit a canonical `volume` column so features.compute does not reject the frame
    with 'bars missing required columns: [volume]' (production alpha-research failure)."""
    from q_backend.features.compute import _validate_bars
    from q_backend.features.evidence_service import _ohlcv_to_bars
    from q_backend.market_data.models import OHLCV

    records = [
        OHLCV(
            time=datetime(2024, 1, 1, hour) if hour < 24 else datetime(2024, 1, 2),
            open=100.0 + hour,
            high=101.0 + hour,
            low=99.0 + hour,
            close=100.5 + hour,
            tick_volume=1000 + hour,
            real_volume=None,
        )
        for hour in range(5)
    ]
    frame = _ohlcv_to_bars(records)
    assert "volume" in frame.columns
    assert frame["volume"].tolist() == [float(1000 + h) for h in range(5)]
    # Must satisfy the compute-path validator (raises ValueError if volume is absent).
    _validate_bars(frame)


def test_split_segments_are_disjoint():
    bars, manifest, _ = _manifest_and_segment()
    assert manifest.evidence.bar_count > 0
    assert manifest.walkforward.bar_count > 0
    assert manifest.lockbox.bar_count > 0
    assert manifest.evidence.end < manifest.walkforward.start
    assert manifest.walkforward.end < manifest.lockbox.start
    assert len(bars) == (
        manifest.evidence.bar_count
        + manifest.walkforward.bar_count
        + manifest.lockbox.bar_count
    )


def test_planted_causal_feature_can_be_admitted():
    _, manifest, evidence_bars = _manifest_and_segment(600)
    times = pd.to_datetime(evidence_bars["time"], utc=True)
    close = evidence_bars["close"].to_numpy(dtype=float)
    feature = pd.Series(close, index=times, name="planted")
    target = compute_target(evidence_bars, TargetSpec("fwd_return", 6, "regression"))
    target.index = times

    evidence = _evaluate_with_series(
        feature,
        target,
        manifest=manifest,
        evidence_bars=evidence_bars,
        thresholds={"min_abs_rank_ic": 0.01, "min_deflated_score": 0.01, "min_obs": 80},
    )
    assert evidence.n_obs >= 80
    assert evidence.decision == "admitted"


def test_lookahead_feature_rejected_for_leakage():
    _, manifest, evidence_bars = _manifest_and_segment(600)
    times = pd.to_datetime(evidence_bars["time"], utc=True)
    close = evidence_bars["close"].to_numpy(dtype=float)
    lookahead = pd.Series(np.roll(close, -6), index=times, name="lookahead")
    target = compute_target(evidence_bars, TargetSpec("fwd_return", 6, "regression"))
    target.index = times

    evidence = _evaluate_with_series(
        lookahead,
        target,
        manifest=manifest,
        evidence_bars=evidence_bars,
        leakage_status="suspect",
    )
    assert evidence.decision == "rejected"
    assert "leakage_suspect" in evidence.rejection_reasons


def test_unstable_sign_rejected_despite_high_aggregate_ic():
    class _Fold:
        def __init__(self, fold_index: int, rank_ic: float):
            self.fold_index = fold_index
            self.ic = rank_ic
            self.rank_ic = rank_ic
            self.n_obs = 50

    decision, reasons = decide_admission(
        n_obs=200,
        fold_diagnostics=[
            _Fold(0, 0.20),
            _Fold(1, 0.18),
            _Fold(2, -0.17),
            _Fold(3, -0.16),
        ],
        rank_ic=0.15,
        sign_consistency=0.5,
        effect_dispersion=0.05,
        deflated_score=0.9,
        leakage_status="clean",
        is_representative=True,
        thresholds=merge_evidence_thresholds({}),
    )
    assert decision == "rejected"
    assert any("unstable_sign" in reason for reason in reasons)


def test_too_short_range_is_inconclusive():
    _, manifest, evidence_bars = _manifest_and_segment(80)
    times = pd.to_datetime(evidence_bars["time"], utc=True)
    feature = pd.Series(evidence_bars["close"].to_numpy(), index=times)
    target = compute_target(evidence_bars, TargetSpec("fwd_return", 6, "regression"))
    target.index = times

    evidence = _evaluate_with_series(
        feature,
        target,
        manifest=manifest,
        evidence_bars=evidence_bars,
    )
    assert evidence.decision == "inconclusive"
    assert any(reason.startswith("insufficient_") for reason in evidence.rejection_reasons)


def test_deflated_score_decreases_as_search_budget_grows():
    score_small = deflated_ic_score(
        0.12, num_trials=5, n_obs=300, null_floor=0.01
    )
    score_large = deflated_ic_score(
        0.12, num_trials=500, n_obs=300, null_floor=0.01
    )
    assert score_large < score_small


def test_permutation_null_floor_rises_with_attempted_feature_count():
    _, manifest, evidence_bars = _manifest_and_segment(400)
    times = pd.to_datetime(evidence_bars["time"], utc=True)
    feature = pd.Series(evidence_bars["close"].to_numpy(), index=times)
    target = compute_target(evidence_bars, TargetSpec("fwd_return", 6, "regression"))
    target.index = times

    floor_small, _ = permutation_null_floor(
        feature, target, num_trials=5, n_permutations=20, block_size=30, seed=1
    )
    floor_large, _ = permutation_null_floor(
        feature, target, num_trials=500, n_permutations=20, block_size=30, seed=1
    )
    assert floor_large > floor_small


def test_redundant_feature_rejected():
    _, manifest, evidence_bars = _manifest_and_segment(400)
    times = pd.to_datetime(evidence_bars["time"], utc=True)
    base = evidence_bars["close"].to_numpy(dtype=float)
    frame = pd.DataFrame(
        {
            "feat_a.v1.abc": base,
            "feat_b.v1.def": base + 1e-9,
        },
        index=times,
    )
    matrix = FeatureMatrix(
        matrix_id="test-matrix",
        frame=frame,
        manifest={"valid_from": None, "features": []},
    )
    clusters = cluster_redundant(matrix, threshold=0.9)
    assert len(clusters) == 1
    assert len(clusters[0].feature_ids) == 2

    class _Fold:
        def __init__(self, fold_index: int):
            self.fold_index = fold_index
            self.ic = 0.1
            self.rank_ic = 0.1
            self.n_obs = 50

    decision, reasons = decide_admission(
        n_obs=200,
        fold_diagnostics=[_Fold(0), _Fold(1), _Fold(2)],
        rank_ic=0.1,
        sign_consistency=1.0,
        effect_dispersion=0.01,
        deflated_score=0.9,
        leakage_status="clean",
        is_representative=False,
        thresholds=merge_evidence_thresholds({}),
    )
    assert decision == "rejected"
    assert "redundant_non_representative" in reasons


def test_persistence_round_trip(db_session, lake_root_path):
    profile = RESEARCH_PROFILES["ccm_h1_swing"]
    spec = get_feature_spec("vol_regime")
    fid = feature_id(spec, spec.default_params)
    key = FeatureEvidenceKey(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        feature_id=fid,
        feature_version=spec.version,
        target="fwd_return",
        horizon=6,
        split_manifest_hash="abc123",
    )
    evidence = FeatureEvidence(
        key=key,
        feature_name="vol_regime",
        node_kind="feature.vol_regime",
        ic=0.05,
        rank_ic=0.06,
        mutual_info=0.01,
        sign_consistency=0.8,
        median_effect=0.05,
        effect_dispersion=0.01,
        n_obs=200,
        fold_diagnostics=[],
        regime_ics={"all": 0.05},
        permutation_null_floor=0.02,
        deflated_score=0.7,
        decision="admitted",
        rejection_reasons=[],
        leakage_status="clean",
        is_representative=True,
        cluster_id=0,
        data_fingerprint="fingerprint123",
        attempted_feature_count=4,
    )
    persist_feature_evidence(db_session, evidence)
    db_session.commit()

    row = db_session.query(FeatureEvidenceRow).one()
    assert row.profile_id == profile.profile_id
    assert row.feature_name == "vol_regime"
    assert row.decision == "admitted"
    assert row.diagnostics_artifact_id

    loaded = load_feature_evidence(db_session, key)
    assert loaded is not None
    assert loaded.decision == "admitted"
    assert loaded.rank_ic == pytest.approx(0.06)
    diagnostics = read_feature_evidence(row.diagnostics_artifact_id)
    assert diagnostics["feature_name"] == "vol_regime"
    assert diagnostics["fold_diagnostics"] == []


def test_hypothesis_eligibility_with_admitted_evidence(db_session, lake_root_path):
    profile = RESEARCH_PROFILES["ccm_h1_swing"]
    spec = get_feature_spec("vol_regime")
    fid = feature_id(spec, spec.default_params)
    key = FeatureEvidenceKey(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        feature_id=fid,
        feature_version=spec.version,
        target="fwd_return",
        horizon=6,
        split_manifest_hash="manifest-1",
    )
    evidence = FeatureEvidence(
        key=key,
        feature_name="vol_regime",
        node_kind="feature.vol_regime",
        ic=0.05,
        rank_ic=0.06,
        mutual_info=0.01,
        sign_consistency=0.8,
        median_effect=0.05,
        effect_dispersion=0.01,
        n_obs=200,
        fold_diagnostics=[],
        regime_ics={},
        permutation_null_floor=0.02,
        deflated_score=0.7,
        decision="admitted",
        data_fingerprint="fp-1",
    )
    persist_feature_evidence(db_session, evidence)
    db_session.commit()

    resolver = ProfileFeatureAdmissionResolver(
        db_session,
        profile=profile,
        split_manifest_hash="manifest-1",
        data_fingerprint="fp-1",
    )
    assert resolver.is_feature_admitted("feature.vol_regime", "CCM$", "H1")

    config = _search_config(
        symbol="CCM$",
        timeframe="H1",
        strategies=["ccm_h1_swing_v1_breakout"],
    )
    provider = HypothesisCandidateProvider(config, resolver)
    candidates = list(provider.candidates())
    assert len(candidates) == 1
    assert candidates[0].candidate_id == "ccm_h1_swing_v1_breakout"


def test_rejected_or_mismatched_evidence_keeps_hypothesis_ineligible(db_session, lake_root_path):
    profile = RESEARCH_PROFILES["ccm_h1_swing"]
    spec = get_feature_spec("vol_regime")
    fid = feature_id(spec, spec.default_params)
    key = FeatureEvidenceKey(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        feature_id=fid,
        feature_version=spec.version,
        target="fwd_return",
        horizon=6,
        split_manifest_hash="manifest-1",
    )
    evidence = FeatureEvidence(
        key=key,
        feature_name="vol_regime",
        node_kind="feature.vol_regime",
        ic=0.01,
        rank_ic=0.01,
        mutual_info=0.0,
        sign_consistency=0.5,
        median_effect=0.01,
        effect_dispersion=0.2,
        n_obs=200,
        fold_diagnostics=[],
        regime_ics={},
        permutation_null_floor=0.05,
        deflated_score=0.1,
        decision="rejected",
        rejection_reasons=["weak_rank_ic"],
        data_fingerprint="fp-1",
    )
    persist_feature_evidence(db_session, evidence)
    db_session.commit()

    resolver = ProfileFeatureAdmissionResolver(
        db_session,
        profile=profile,
        split_manifest_hash="manifest-1",
        data_fingerprint="fp-1",
    )
    assert not resolver.is_feature_admitted("feature.vol_regime", "CCM$", "H1")

    mismatch_resolver = ProfileFeatureAdmissionResolver(
        db_session,
        profile=profile,
        split_manifest_hash="other-manifest",
        data_fingerprint="fp-1",
    )
    assert not mismatch_resolver.is_feature_admitted("feature.vol_regime", "CCM$", "H1")

    fail_closed = FailClosedFeatureAdmissionResolver()
    assert not fail_closed.is_feature_admitted("feature.vol_regime", "CCM$", "H1")
