"""Versioned research profiles, curated hypothesis catalog, and candidate provider.

This module implements named economic hypotheses for systematic trading research
(swing & day trading) on CCM$, WIN$, and WDO$ futures.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Literal, Optional, Protocol, Set
from pydantic import BaseModel, Field

from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.search_space import derive_genome_search_space
from q_backend.backtesting.genome.exit_rule_policy import exit_policy_metadata_for_genome
from q_backend.optimization.models import SearchParam, SearchSpaceConfig
from q_backend.optimization.research_acceptance import ResearchAcceptanceConfig
from q_backend.optimization.strategy_search import (
    CandidateProvider,
    CandidateResult,
    SearchCandidate,
    StrategySearchConfig,
)


class HypothesisError(Exception):
    """Base exception for all hypothesis catalog errors."""
    pass


class IncompatibleProfileError(HypothesisError):
    """Raised when a hypothesis is incompatible with the requested instrument profile."""
    def __init__(self, hypothesis_id: str, symbol: str, timeframe: str, details: str):
        self.hypothesis_id = hypothesis_id
        self.symbol = symbol
        self.timeframe = timeframe
        super().__init__(
            f"Hypothesis '{hypothesis_id}' is incompatible with symbol={symbol}, "
            f"timeframe={timeframe}. {details}"
        )


class MissingFeatureEvidenceError(HypothesisError):
    """Raised when required features for a hypothesis are not admitted by the evidence gate."""
    def __init__(self, hypothesis_id: str, missing_features: list[str]):
        self.hypothesis_id = hypothesis_id
        self.missing_features = missing_features
        super().__init__(
            f"Hypothesis '{hypothesis_id}' is ineligible due to missing admitted "
            f"feature evidence for: {', '.join(missing_features)}"
        )


class SessionRules(BaseModel):
    """Session trading limits and flattening rules."""
    day_trade: bool = False
    day_trade_start_time: str = "09:00"
    day_trade_end_time: str = "17:00"
    day_trade_close_time: str = "18:00"


class InstrumentResearchProfile(BaseModel):
    """Versioned research constraints and thresholds for a specific symbol/timeframe."""
    version: int = 1
    profile_id: str
    symbol: str
    timeframe: str
    style: Literal["swing", "day_trade"]
    target_horizons: List[int]
    session_rules: SessionRules
    evidence_thresholds: Dict[str, Any] = Field(default_factory=dict)
    minimum_observation_counts: Dict[str, int] = Field(default_factory=dict)
    research_acceptance: ResearchAcceptanceConfig | None = None


class HypothesisDefinition(BaseModel):
    """Static catalog definition of a named economic hypothesis."""
    hypothesis_id: str
    profile_id: str
    version: int = 1
    rationale: str
    required_features: List[str]
    genome_template: Dict[str, Any]
    parameter_search_space: Dict[str, SearchParam] = Field(default_factory=dict)
    permitted_exit_families: List[str] = Field(default_factory=list)
    incompatibilities: List[str] = Field(default_factory=list)
    expected_holding_horizon: str


class HypothesisCandidateMetadata(BaseModel):
    """Identity tracking columns for candidates derived from the hypothesis catalog."""
    profile_version: int
    hypothesis_id: str
    rationale: str
    required_features: List[str]
    template_hash: str


class FeatureAdmissionResolver(Protocol):
    """Injectable boundary verifying feature evidence admissibility (WO161/WO162)."""
    def is_feature_admitted(self, feature_id: str, symbol: str, timeframe: str) -> bool:
        ...


class FailClosedFeatureAdmissionResolver:
    """Production default resolver that rejects all features prior to WO162 evidencing."""
    def is_feature_admitted(self, feature_id: str, symbol: str, timeframe: str) -> bool:
        return False


class AdmittedAllFeatureAdmissionResolver:
    """Test-only resolver that admits all features to exercise provider mechanics."""
    def is_feature_admitted(self, feature_id: str, symbol: str, timeframe: str) -> bool:
        return True


def compute_template_hash(template: dict[str, Any]) -> str:
    """Compute deterministic template hash ignoring metadata differences."""
    # We copy and remove metadata to ensure hash stability across documentation changes
    clean = copy_clean_template(template)
    serialized = json.dumps(clean, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def copy_clean_template(template: dict[str, Any]) -> dict[str, Any]:
    """Strip metadata field from template to isolate layout/structure."""
    import copy
    copied = copy.deepcopy(template)
    copied.pop("metadata", None)
    return copied


# ==========================================
# 1. Versioned Instrument Research Profiles
# ==========================================
RESEARCH_PROFILES: Dict[str, InstrumentResearchProfile] = {
    "ccm_h1_swing": InstrumentResearchProfile(
        version=1,
        profile_id="ccm_h1_swing",
        symbol="CCM$",
        timeframe="H1",
        style="swing",
        target_horizons=[6, 12, 24],
        session_rules=SessionRules(
            day_trade=False,
            day_trade_start_time="09:00",
            day_trade_end_time="17:55",
            day_trade_close_time="18:00",
        ),
        minimum_observation_counts={"min_trades": 30, "min_oos_windows": 6},
        research_acceptance=ResearchAcceptanceConfig(
            min_stitched_oos_trades=30,
            lockbox_min_trades=5,
        ),
        evidence_thresholds={
            "min_obs": 100,
            "min_folds": 3,
            "min_valid_folds": 2,
            "min_abs_rank_ic": 0.02,
            "min_sign_consistency": 0.67,
            "max_rank_ic_dispersion": 0.20,
            "min_deflated_score": 0.50,
        },
    ),
    "win_h1_swing": InstrumentResearchProfile(
        version=1,
        profile_id="win_h1_swing",
        symbol="WIN$",
        timeframe="H1",
        style="swing",
        target_horizons=[4, 8, 16],
        session_rules=SessionRules(
            day_trade=False,
            day_trade_start_time="09:00",
            day_trade_end_time="17:55",
            day_trade_close_time="18:00",
        ),
        minimum_observation_counts={"min_trades": 30, "min_oos_windows": 6},
        research_acceptance=ResearchAcceptanceConfig(
            min_stitched_oos_trades=30,
            lockbox_min_trades=5,
        ),
        evidence_thresholds={
            "min_obs": 100,
            "min_folds": 3,
            "min_valid_folds": 2,
            "min_abs_rank_ic": 0.02,
            "min_sign_consistency": 0.67,
            "max_rank_ic_dispersion": 0.20,
            "min_deflated_score": 0.50,
        },
    ),
    "wdo_m15_day": InstrumentResearchProfile(
        version=1,
        profile_id="wdo_m15_day",
        symbol="WDO$",
        timeframe="M15",
        style="day_trade",
        target_horizons=[2, 4, 8],
        session_rules=SessionRules(
            day_trade=True,
            day_trade_start_time="09:00",
            day_trade_end_time="17:55",
            day_trade_close_time="18:00",
        ),
        minimum_observation_counts={"min_trades": 100, "min_oos_windows": 6},
        research_acceptance=ResearchAcceptanceConfig(
            min_stitched_oos_trades=100,
            lockbox_min_trades=10,
            lockbox_max_drawdown_pct=0.25,
        ),
        evidence_thresholds={
            "min_obs": 100,
            "min_folds": 3,
            "min_valid_folds": 2,
            "min_abs_rank_ic": 0.02,
            "min_sign_consistency": 0.67,
            "max_rank_ic_dispersion": 0.20,
            "min_deflated_score": 0.50,
        },
    ),
}


# ==========================================
# 2. Curated Hypotheses Templates & Catalog
# ==========================================
CCM_H1_SWING_BREAKOUT = {
    "version": 1,
    "genome_id": "ccm_h1_swing_v1_breakout",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "n2", "kind": "ind.donchian", "params": {"period": {"param": "period"}}, "inputs": []},
        {"id": "n3", "kind": "cmp.cross_above", "params": {}, "inputs": ["n1", "n2:donchian_upper"]},
        {"id": "n4", "kind": "cmp.cross_below", "params": {}, "inputs": ["n1", "n2:donchian_lower"]},
        {"id": "n5", "kind": "feature.vol_regime", "params": {"window": {"param": "window"}, "regime_lookback": {"param": "regime_lookback"}}, "inputs": ["n1"]},
        {"id": "n6", "kind": "cmp.cross_above", "params": {"threshold": {"param": "threshold"}}, "inputs": ["n5"]},
        {"id": "n7", "kind": "logic.and", "params": {}, "inputs": ["n3", "n6"]},
        {"id": "n8", "kind": "logic.and", "params": {}, "inputs": ["n4", "n6"]},
    ],
    "entry_long": {"ref": "n7"},
    "entry_short": {"ref": "n8"},
    "exit_long": {"ref": "n4"},
    "exit_short": {"ref": "n3"},
    "metadata": {}
}

CCM_H1_SWING_PULLBACK = {
    "version": 1,
    "genome_id": "ccm_h1_swing_v1_pullback",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "n2", "kind": "ind.rsi", "params": {"period": {"param": "period"}}, "inputs": ["n1"]},
        {"id": "n3", "kind": "cmp.cross_above", "params": {"threshold": {"param": "oversold"}}, "inputs": ["n2"]},
        {"id": "n4", "kind": "cmp.cross_below", "params": {"threshold": {"param": "overbought"}}, "inputs": ["n2"]},
        {"id": "n5", "kind": "feature.d1_trend", "params": {}, "inputs": []},
        {"id": "n6", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["n5"]},
        {"id": "n7", "kind": "logic.not", "params": {}, "inputs": ["n6"]},
        {"id": "n8", "kind": "logic.and", "params": {}, "inputs": ["n3", "n6"]},
        {"id": "n9", "kind": "logic.and", "params": {}, "inputs": ["n4", "n7"]},
    ],
    "entry_long": {"ref": "n8"},
    "entry_short": {"ref": "n9"},
    "exit_long": {"ref": "n4"},
    "exit_short": {"ref": "n3"},
    "metadata": {}
}

WIN_H1_SWING_TREND = {
    "version": 1,
    "genome_id": "win_h1_swing_v1_trend",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "n2", "kind": "ind.rsi", "params": {"period": {"param": "period"}}, "inputs": ["n1"]},
        {"id": "n3", "kind": "cmp.cross_above", "params": {"threshold": {"param": "oversold"}}, "inputs": ["n2"]},
        {"id": "n4", "kind": "cmp.cross_below", "params": {"threshold": {"param": "overbought"}}, "inputs": ["n2"]},
        {"id": "n5", "kind": "feature.d1_trend", "params": {}, "inputs": []},
        {"id": "n6", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["n5"]},
        {"id": "n7", "kind": "logic.not", "params": {}, "inputs": ["n6"]},
        {"id": "n8", "kind": "logic.and", "params": {}, "inputs": ["n3", "n6"]},
        {"id": "n9", "kind": "logic.and", "params": {}, "inputs": ["n4", "n7"]},
    ],
    "entry_long": {"ref": "n8"},
    "entry_short": {"ref": "n9"},
    "exit_long": {"ref": "n4"},
    "exit_short": {"ref": "n3"},
    "metadata": {}
}

WDO_M15_DAY_OR_BREAKOUT = {
    "version": 1,
    "genome_id": "wdo_m15_day_v1_or_breakout",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "n2", "kind": "feature.opening_range_high", "params": {"session_open": "09:00", "range_minutes": {"param": "range_minutes"}}, "inputs": []},
        {"id": "n3", "kind": "feature.opening_range_low", "params": {"session_open": "09:00", "range_minutes": {"param": "range_minutes"}}, "inputs": []},
        {"id": "n4", "kind": "cmp.cross_above", "params": {}, "inputs": ["n1", "n2:out"]},
        {"id": "n5", "kind": "cmp.cross_below", "params": {}, "inputs": ["n1", "n3:out"]},
        {"id": "n6", "kind": "feature.minutes_from_open", "params": {"session_open": "09:00"}, "inputs": []},
        {"id": "n7", "kind": "cmp.cross_above", "params": {"threshold": 60.0}, "inputs": ["n6"]},
        {"id": "n8", "kind": "cmp.cross_below", "params": {"threshold": 300.0}, "inputs": ["n6"]},
        {"id": "n9", "kind": "logic.and", "params": {}, "inputs": ["n7", "n8"]},
        {"id": "n10", "kind": "logic.and", "params": {}, "inputs": ["n4", "n9"]},
        {"id": "n11", "kind": "logic.and", "params": {}, "inputs": ["n5", "n9"]},
    ],
    "entry_long": {"ref": "n10"},
    "entry_short": {"ref": "n11"},
    "exit_long": {"ref": "n5"},
    "exit_short": {"ref": "n4"},
    "metadata": {}
}


HYPOTHESIS_CATALOG: Dict[str, HypothesisDefinition] = {
    # CCM$ H1 swing hypotheses
    "ccm_h1_swing_v1_breakout": HypothesisDefinition(
        hypothesis_id="ccm_h1_swing_v1_breakout",
        profile_id="ccm_h1_swing",
        rationale="Volatility-normalized breakout strategy capturing medium-term trend shifts on CCM$ H1 swing.",
        required_features=["feature.vol_regime"],
        genome_template=CCM_H1_SWING_BREAKOUT,
        permitted_exit_families=["stop_loss", "target", "general"],
        expected_holding_horizon="6 to 24 bars",
    ),
    "ccm_h1_swing_v1_pullback": HypothesisDefinition(
        hypothesis_id="ccm_h1_swing_v1_pullback",
        profile_id="ccm_h1_swing",
        rationale="Swing pullback buys gated by a positive higher-timeframe trend filter on CCM$ H1 swing.",
        required_features=["feature.d1_trend"],
        genome_template=CCM_H1_SWING_PULLBACK,
        permitted_exit_families=["stop_loss", "trailing", "target"],
        expected_holding_horizon="6 to 24 bars",
    ),
    # WIN$ H1 swing hypotheses
    "win_h1_swing_v1_trend": HypothesisDefinition(
        hypothesis_id="win_h1_swing_v1_trend",
        profile_id="win_h1_swing",
        rationale="HTF trend continuation pulling back to daily support/resistance zones on WIN$ H1 swing.",
        required_features=["feature.d1_trend"],
        genome_template=WIN_H1_SWING_TREND,
        permitted_exit_families=["stop_loss", "trailing", "target"],
        expected_holding_horizon="4 to 16 bars",
    ),
    # WDO$ M15 day trade hypotheses
    "wdo_m15_day_v1_or_breakout": HypothesisDefinition(
        hypothesis_id="wdo_m15_day_v1_or_breakout",
        profile_id="wdo_m15_day",
        rationale="Opening-range breakout strategy after range definition completes, with intraday flatting on WDO$ M15.",
        required_features=[
            "feature.opening_range_high",
            "feature.opening_range_low",
            "feature.minutes_from_open",
        ],
        genome_template=WDO_M15_DAY_OR_BREAKOUT,
        permitted_exit_families=["stop_loss", "target", "time"],
        expected_holding_horizon="2 to 8 bars",
    ),
}


def get_hypotheses_for_profile(profile_id: str) -> List[HypothesisDefinition]:
    """Retrieve all hypotheses associated with the given profile."""
    return [h for h in HYPOTHESIS_CATALOG.values() if h.profile_id == profile_id]


def match_profile(symbol: str, timeframe: str) -> Optional[InstrumentResearchProfile]:
    """Find a research profile matching the given symbol and timeframe prefix."""
    s_upper = symbol.upper()
    tf_upper = timeframe.upper()
    for profile in RESEARCH_PROFILES.values():
        if profile.symbol.upper() in s_upper and profile.timeframe.upper() == tf_upper:
            return profile
    return None


def extract_hypothesis_metadata(genome: dict[str, Any] | Genome | None) -> dict[str, Any]:
    """Extract hypothesis identity and rationale fields from genome metadata."""
    if genome is None:
        return {}
    if isinstance(genome, dict):
        metadata = genome.get("metadata", {})
    else:
        metadata = genome.metadata or {}
    
    hyp_meta = metadata.get("hypothesis", {})
    if not isinstance(hyp_meta, dict):
        return {}
        
    return {
        "profile_version": hyp_meta.get("profile_version"),
        "hypothesis_id": hyp_meta.get("hypothesis_id"),
        "hypothesis_rationale": hyp_meta.get("rationale"),
        "hypothesis_required_features": hyp_meta.get("required_features"),
        "hypothesis_template_hash": hyp_meta.get("template_hash"),
    }


# ==========================================
# 3. Hypothesis Candidate Provider Seam
# ==========================================
class HypothesisCandidateProvider:
    """Emits candidates representing named, bounded economic hypotheses from the catalog."""

    def __init__(
        self,
        search_config: StrategySearchConfig,
        resolver: FeatureAdmissionResolver,
        catalog_version: int = 1,
        seed: int = 42,
    ) -> None:
        self._config = search_config
        self._resolver = resolver
        self._catalog_version = catalog_version
        self._seed = seed
        self._candidate_metadata: dict[str, dict[str, Any]] = {}

    def candidates(self) -> Iterable[SearchCandidate]:
        symbol = self._config.backtest.symbol
        timeframe = self._config.backtest.timeframe
        
        # 1. Determine profile
        profile = match_profile(symbol, timeframe)
        if profile is None:
            raise IncompatibleProfileError(
                "N/A", symbol, timeframe, "No matching instrument research profile found."
            )

        # 2. Get hypotheses
        hypotheses = get_hypotheses_for_profile(profile.profile_id)
        
        # Filter by requested strategies if specified
        requested = self._config.strategies
        if requested is not None:
            hypotheses_dict = {h.hypothesis_id: h for h in hypotheses}
            filtered_hyps = []
            for req_id in requested:
                if req_id not in hypotheses_dict:
                    if req_id in HYPOTHESIS_CATALOG:
                        other_hyp = HYPOTHESIS_CATALOG[req_id]
                        raise IncompatibleProfileError(
                            req_id, symbol, timeframe,
                            f"Hypothesis '{req_id}' belongs to profile '{other_hyp.profile_id}' "
                            f"but active profile is '{profile.profile_id}'."
                        )
                    else:
                        raise ValueError(f"Hypothesis ID '{req_id}' not found in the catalog.")
                filtered_hyps.append(hypotheses_dict[req_id])
            hypotheses = filtered_hyps

        # 3. For each hypothesis, check features and yield SearchCandidate
        for hyp in hypotheses:
            # Check required features admission
            missing = [
                feat for feat in hyp.required_features
                if not self._resolver.is_feature_admitted(feat, symbol, timeframe)
            ]
            if missing:
                if requested is not None and hyp.hypothesis_id in requested:
                    raise MissingFeatureEvidenceError(hyp.hypothesis_id, missing)
                continue

            # Check incompatibilities
            incompatible = False
            for inc in hyp.incompatibilities:
                if inc == timeframe or inc == symbol:
                    incompatible = True
                    break
            if incompatible:
                if requested is not None and hyp.hypothesis_id in requested:
                    raise IncompatibleProfileError(
                        hyp.hypothesis_id, symbol, timeframe, f"Hypothesis is incompatible with {inc}."
                    )
                continue

            # Build template
            template = dict(hyp.genome_template)
            template["genome_id"] = hyp.hypothesis_id
            template["version"] = 1
            
            # Embed hypothesis identity into genome metadata
            tpl_hash = compute_template_hash(hyp.genome_template)
            if "metadata" not in template:
                template["metadata"] = {}
            template["metadata"]["hypothesis"] = {
                "profile_version": profile.version,
                "hypothesis_id": hyp.hypothesis_id,
                "rationale": hyp.rationale,
                "required_features": list(hyp.required_features),
                "template_hash": tpl_hash,
            }

            genome = Genome.model_validate(template)

            # Derive search space
            search_space, fixed_params = derive_genome_search_space(genome)
            # Override strategy params with custom bounds
            for key, spec in hyp.parameter_search_space.items():
                if key in search_space.strategy_params:
                    search_space.strategy_params[key] = spec

            # Combine risk search space if configured
            if self._config.include_risk_search:
                from q_backend.optimization.auto_search_space import default_risk_search_space
                risk_space = default_risk_search_space()
                search_space = SearchSpaceConfig(
                    strategy_params=search_space.strategy_params,
                    risk_params=risk_space.risk_params,
                    manager_params=search_space.manager_params,
                )

            # Populate metadata
            self._candidate_metadata[hyp.hypothesis_id] = {
                "profile_version": profile.version,
                "hypothesis_id": hyp.hypothesis_id,
                "hypothesis_rationale": hyp.rationale,
                "hypothesis_required_features": list(hyp.required_features),
                "hypothesis_template_hash": tpl_hash,
                "genome": genome.model_dump(),
                "genome_node_count": len(genome.nodes),
            }

            policy_meta = exit_policy_metadata_for_genome(genome)
            if policy_meta:
                self._candidate_metadata[hyp.hypothesis_id].update(policy_meta)

            yield SearchCandidate(
                candidate_id=hyp.hypothesis_id,
                strategy="CompositeStrategy",
                search_space=search_space,
                fixed_params={
                    "genome": genome.model_dump(),
                    "timeframe": timeframe,
                    **fixed_params,
                }
            )

    def report(self, results: list[CandidateResult]) -> None:
        pass

    def candidate_metadata(self) -> dict[str, dict[str, Any]]:
        return dict(self._candidate_metadata)


def resolve_candidate_provider(
    request: StrategySearchConfig,
    resolver: FeatureAdmissionResolver | None = None,
) -> CandidateProvider:
    """Build the correct CandidateProvider (Registry, Genetic or Hypothesis) for the request."""
    if request.genetic is not None:
        from q_backend.optimization.genetic_search import create_genetic_candidate_provider
        return create_genetic_candidate_provider(
            request.genetic,
            request,
            latents_enabled=request.latents_enabled,
            resolver=resolver,
        )
    
    resolver = resolver or FailClosedFeatureAdmissionResolver()
    
    use_hypothesis = False
    if request.strategies:
        if any(strat in HYPOTHESIS_CATALOG for strat in request.strategies):
            use_hypothesis = True
    else:
        profile = match_profile(request.backtest.symbol, request.backtest.timeframe)
        if profile is not None:
            use_hypothesis = True

    if use_hypothesis:
        return HypothesisCandidateProvider(request, resolver)
    
    from q_backend.optimization.strategy_search import RegistryCandidateProvider
    return RegistryCandidateProvider(request)
