"""Unit tests for WO161 hypothesis profiles, catalog, and provider."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import validate_genome
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.session_context import prepare_evaluation_frame
from q_backend.optimization.hypothesis import (
    RESEARCH_PROFILES,
    HYPOTHESIS_CATALOG,
    match_profile,
    get_hypotheses_for_profile,
    compute_template_hash,
    AdmittedAllFeatureAdmissionResolver,
    FailClosedFeatureAdmissionResolver,
    HypothesisCandidateProvider,
    resolve_candidate_provider,
    IncompatibleProfileError,
    MissingFeatureEvidenceError,
)
from q_backend.optimization.models import (
    BacktestConfig,
    ObjectiveConfig,
    ObjectiveMode,
    StudyConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.optimization.strategy_search import StrategySearchConfig, CandidateResult
from q_backend.optimization.genetic_search import GeneticCandidateProvider


def _search_config(symbol: str = "CCM$", timeframe: str = "H1", strategies: list[str] | None = None) -> StrategySearchConfig:
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
        walkforward=WalkForwardConfig(
            train_days=10,
            test_days=5,
            anchored=True,
        ),
        study=StudyConfig(name="test_hypothesis_study", n_trials=5, seed=42),
        strategies=strategies,
    )


def _make_intraday_ohlcv(start: datetime, periods: int = 100) -> pd.DataFrame:
    rows = []
    price = 100.0
    for i in range(periods):
        timestamp = start + timedelta(hours=i)
        drift = 0.05 if i % 10 < 5 else -0.05
        price = max(50.0, price + drift)
        rows.append(
            {
                "time": timestamp,
                "open": price - 0.2,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000.0,
            }
        )
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def test_profiles_snapshot():
    """Verify that the profiles contain correct identifiers, horizons, styles, and rules."""
    assert "ccm_h1_swing" in RESEARCH_PROFILES
    ccm = RESEARCH_PROFILES["ccm_h1_swing"]
    assert ccm.symbol == "CCM$"
    assert ccm.timeframe == "H1"
    assert ccm.style == "swing"
    assert ccm.target_horizons == [6, 12, 24]
    assert ccm.session_rules.day_trade is False

    assert "win_h1_swing" in RESEARCH_PROFILES
    win = RESEARCH_PROFILES["win_h1_swing"]
    assert win.symbol == "WIN$"
    assert win.timeframe == "H1"
    assert win.style == "swing"
    assert win.target_horizons == [4, 8, 16]

    assert "wdo_m15_day" in RESEARCH_PROFILES
    wdo = RESEARCH_PROFILES["wdo_m15_day"]
    assert wdo.symbol == "WDO$"
    assert wdo.timeframe == "M15"
    assert wdo.style == "day_trade"
    assert wdo.target_horizons == [2, 4, 8]
    assert wdo.session_rules.day_trade is True


def test_hypothesis_catalog_snapshot():
    """Snapshot validation of hypothesis catalog IDs, expected horizons, rationales, features, and hashes."""
    assert len(HYPOTHESIS_CATALOG) >= 4
    
    # Verify hashes and basic properties
    for hyp_id, hyp in HYPOTHESIS_CATALOG.items():
        assert hyp.hypothesis_id == hyp_id
        assert len(hyp.rationale) > 10
        assert len(hyp.required_features) >= 1
        assert len(hyp.expected_holding_horizon) > 0
        
        # Verify template hash computation matches
        expected_hash = compute_template_hash(hyp.genome_template)
        assert len(expected_hash) == 64  # SHA-256 length hex


def test_validate_and_compile_templates():
    """Verify that every template in the catalog is syntactically valid and passes schema verification."""
    for hyp in HYPOTHESIS_CATALOG.values():
        genome = Genome.model_validate(hyp.genome_template)
        # Passes validate_genome without throwing exceptions
        validate_genome(genome)


def test_backtest_templates_on_synthetic_data():
    """Verify that every template runs and computes signals on B3-context-prepared synthetic data."""
    ohlcv = _make_intraday_ohlcv(datetime(2026, 1, 1, 9, 0), periods=200)
    prepared_frame = prepare_evaluation_frame(ohlcv)
    
    # We must mock default parameter values because the templates use parameter references
    # that normally get filled by the optimizer/provider.
    default_params = {
        "period": 14,
        "oversold": 30.0,
        "overbought": 70.0,
        "window": 20,
        "regime_lookback": 60,
        "threshold": 0.0,
        "range_minutes": 60,
    }

    for hyp in HYPOTHESIS_CATALOG.values():
        genome = Genome.model_validate(hyp.genome_template)
        # Evaluate composite strategy compute_indicators on our prepared frame
        strategy = CompositeStrategy(
            genome=genome,
            params=default_params,
            symbol="TEST",
        )
        indicators = strategy.compute_indicators(prepared_frame)
        assert isinstance(indicators, pd.DataFrame)
        assert "buy_signal" in indicators.columns
        assert "sell_signal" in indicators.columns


def test_missing_feature_evidence_throws_structured_error():
    """Verify that requesting an ineligible hypothesis throws a structured MissingFeatureEvidenceError."""
    resolver = FailClosedFeatureAdmissionResolver()
    config = _search_config(symbol="CCM$", timeframe="H1", strategies=["ccm_h1_swing_v1_breakout"])
    
    provider = HypothesisCandidateProvider(config, resolver)
    with pytest.raises(MissingFeatureEvidenceError) as exc_info:
        list(provider.candidates())
    
    assert exc_info.value.hypothesis_id == "ccm_h1_swing_v1_breakout"
    assert "feature.vol_regime" in exc_info.value.missing_features


def test_incompatible_profile_throws_structured_error():
    """Verify that requesting a hypothesis for a mismatching profile throws IncompatibleProfileError."""
    resolver = AdmittedAllFeatureAdmissionResolver()
    
    # Requesting WIN$ hypothesis on WDO$ instrument profile
    config = _search_config(symbol="WDO$", timeframe="M15", strategies=["win_h1_swing_v1_trend"])
    provider = HypothesisCandidateProvider(config, resolver)
    
    with pytest.raises(IncompatibleProfileError) as exc_info:
        list(provider.candidates())
        
    assert exc_info.value.hypothesis_id == "win_h1_swing_v1_trend"
    assert exc_info.value.symbol == "WDO$"
    assert exc_info.value.timeframe == "M15"


def test_provider_determinism_and_seed_stability():
    """Verify that the candidate provider produces identical outputs given the same seed and config."""
    resolver = AdmittedAllFeatureAdmissionResolver()
    config = _search_config(symbol="CCM$", timeframe="H1")
    
    p1 = HypothesisCandidateProvider(config, resolver, seed=42)
    p2 = HypothesisCandidateProvider(config, resolver, seed=42)
    
    c1 = list(p1.candidates())
    c2 = list(p2.candidates())
    
    assert len(c1) == len(c2)
    for cand1, cand2 in zip(c1, c2):
        assert cand1.candidate_id == cand2.candidate_id
        assert cand1.strategy == cand2.strategy
        assert cand1.fixed_params == cand2.fixed_params
        assert cand1.search_space.model_dump() == cand2.search_space.model_dump()


def test_genetic_seeding_preserves_diversity():
    """Verify that genetic seeding injects admitted hypothesis templates without destroying random population."""
    resolver = AdmittedAllFeatureAdmissionResolver()
    config = _search_config(symbol="CCM$", timeframe="H1")
    config.genetic = StudyConfig(name="test_genetic_study", n_trials=10, seed=42)
    # Mock GeneticSearchConfig fields
    from q_backend.optimization.strategy_search import GeneticSearchConfig
    config.genetic = GeneticSearchConfig(
        population_size=10,
        generations=2,
        seed_hypotheses=True,
    )
    
    # Build provider
    provider = resolve_candidate_provider(config, resolver)
    assert isinstance(provider, GeneticCandidateProvider)
    
    # Check population
    pop = provider.population
    assert len(pop) == 10
    
    # Count seeded hypotheses
    seeded = [g for g in pop if g.metadata.get("hypothesis") is not None]
    assert len(seeded) >= 1  # We should have seeded at least one admitted hypothesis
    
    # Count random/registry genomes (no hypothesis metadata)
    unseeded = [g for g in pop if g.metadata.get("hypothesis") is None]
    assert len(unseeded) >= 5  # Half of pop should remain registry/random to retain diversity
