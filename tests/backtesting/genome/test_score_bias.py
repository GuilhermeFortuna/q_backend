"""Tests for feature-score-biased GA seeding weights (WO152)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

import pytest

from q_backend.backtesting.genome.operators import (
    INDICATOR_KINDS,
    _choose_seeding,
    build_initial_population,
)
from q_backend.backtesting.genome.score_bias import (
    ALPHA,
    build_kind_weights,
    feature_name_to_kind,
)
from q_backend.storage.db.models import EvaluationRun, FeatureScoreRow, RunStatus


def test_feature_name_to_kind_maps_classical_and_latent() -> None:
    assert feature_name_to_kind("rsi", n_latents=0) == "ind.rsi"
    assert feature_name_to_kind("macd_signal", n_latents=0) == "ind.macd"
    assert feature_name_to_kind("bollinger_upper", n_latents=0) == "ind.bollinger"
    assert feature_name_to_kind("donchian_lower", n_latents=0) == "ind.donchian"
    assert feature_name_to_kind("latent_004@abc12345", n_latents=4) == "ind.latent"
    assert feature_name_to_kind("latent_004@abc12345", n_latents=3) is None
    assert feature_name_to_kind("unknown_feature", n_latents=3) is None


def test_build_kind_weights_aggregates_max_abs_ic(db_session) -> None:
    run = EvaluationRun(
        id=uuid.uuid4(),
        symbol="SYN152",
        timeframe="H1",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 20, tzinfo=timezone.utc),
        target_name="fwd_return",
        target_horizon=5,
        matrix_id="matrix-test",
        status=RunStatus.COMPLETED.value,
        feature_count=4,
        finished_at=datetime(2024, 1, 21, tzinfo=timezone.utc),
    )
    db_session.add(run)
    db_session.flush()

    rows = [
        ("rsi", 0.05),
        ("macd", 0.12),
        ("macd_signal", 0.08),
        ("bollinger_upper", 0.03),
        ("unknown", 0.99),
        ("latent_002@deadbeef", 0.17),
        ("latent_005@deadbeef", 0.20),
    ]
    for feature_name, ic in rows:
        db_session.add(
            FeatureScoreRow(
                run_id=run.id,
                feature_id=f"id-{feature_name}",
                feature_name=feature_name,
                ic=ic,
                rank_ic=ic,
                mutual_info=ic,
                stability=0.9,
                global_score=ic,
                cluster_id=0,
                is_representative=True,
                leakage_status="clean",
                regime_ics={},
            )
        )
    db_session.flush()

    weights = build_kind_weights(
        db_session,
        symbol="SYN152",
        timeframe="H1",
        n_latents=4,
        start=run.start,
        end=run.end,
    )

    assert weights["ind.macd"] == pytest.approx(1.0 + ALPHA * (0.12 / 0.17))
    assert weights["ind.rsi"] == pytest.approx(1.0 + ALPHA * (0.05 / 0.17))
    assert weights["ind.latent"] == pytest.approx(1.0 + ALPHA)
    assert "unknown" not in weights
    assert weights["ind.bollinger"] == pytest.approx(1.0 + ALPHA * (0.03 / 0.17))


def test_build_kind_weights_without_completed_run_returns_empty(db_session) -> None:
    weights = build_kind_weights(
        db_session,
        symbol="MISSING",
        timeframe="H1",
        n_latents=0,
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 20, tzinfo=timezone.utc),
    )
    assert weights == {}


def test_empty_weights_reproduce_uniform_seeding_draws() -> None:
    choices = ("a", "b", "c")
    rng_uniform = random.Random(12345)
    rng_helper = random.Random(12345)
    uniform = [rng_uniform.choice(choices) for _ in range(200)]
    helper = [_choose_seeding(rng_helper, choices, {}) for _ in range(200)]
    assert uniform == helper


def test_build_initial_population_byte_identical_with_empty_kind_weights() -> None:
    kwargs = dict(
        population_size=12,
        max_nodes=24,
        max_depth=12,
        indicator_kinds=tuple(INDICATOR_KINDS),
        n_latents=0,
    )
    baseline_rng = random.Random(5150)
    baseline = build_initial_population(baseline_rng, **kwargs)

    explicit_rng = random.Random(5150)
    explicit = build_initial_population(explicit_rng, kind_weights={}, **kwargs)

    assert [genome.model_dump() for genome in baseline] == [genome.model_dump() for genome in explicit]
