from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
    BacktestRunner,
    DefaultBacktestRunner,
)
from q_backend.optimization.tick_backtest_runner import TickBacktestRunner
from q_backend.optimization.config_loader import load_optimization_config
from q_backend.optimization.exporter import (
    ExportPaths,
    export_results,
    serialize_trial,
)
from q_backend.optimization.models import OptimizationConfig, ObjectiveMode
from q_backend.optimization.runner import OptimizationResult, OptimizationRunner
from q_backend.optimization.feature_admission import (
    ProfileFeatureAdmissionResolver,
    create_profile_admission_resolver,
)
from q_backend.optimization.hypothesis import (
    AdmittedAllFeatureAdmissionResolver,
    FailClosedFeatureAdmissionResolver,
    FeatureAdmissionResolver,
    HypothesisCandidateProvider,
    HypothesisDefinition,
    HypothesisError,
    IncompatibleProfileError,
    InstrumentResearchProfile,
    MissingFeatureEvidenceError,
    SessionRules,
    get_hypotheses_for_profile,
    match_profile,
    resolve_candidate_provider,
)

from q_backend.optimization.research_acceptance import (
    AcceptanceCriterion,
    AttemptCountInputs,
    PlateauNeighborResult,
    PlateauResult,
    ResearchAcceptanceConfig,
    ResearchAcceptanceResult,
    SeedRunRecord,
    compute_champion_hash,
    compute_effective_attempt_count,
    evaluate_plateau_result,
    evaluate_research_acceptance_from_evidence,
    generate_parameter_neighbors,
    research_acceptance_config_for_profile,
    resolve_verdict,
)
from q_backend.optimization.lockbox_consumption import (
    HoldoutConsumedError,
    check_lockbox_consumption,
    record_lockbox_consumption,
)
from q_backend.optimization.research_acceptance_service import run_research_acceptance

__all__ = [
    "BacktestRunConfig",
    "BacktestRunResult",
    "BacktestRunner",
    "DefaultBacktestRunner",
    "ExportPaths",
    "ObjectiveMode",
    "OptimizationConfig",
    "OptimizationResult",
    "OptimizationRunner",
    "TickBacktestRunner",
    "export_results",
    "load_optimization_config",
    "serialize_trial",
    "ProfileFeatureAdmissionResolver",
    "create_profile_admission_resolver",
    "AdmittedAllFeatureAdmissionResolver",
    "FailClosedFeatureAdmissionResolver",
    "FeatureAdmissionResolver",
    "HypothesisCandidateProvider",
    "HypothesisDefinition",
    "HypothesisError",
    "IncompatibleProfileError",
    "InstrumentResearchProfile",
    "MissingFeatureEvidenceError",
    "SessionRules",
    "get_hypotheses_for_profile",
    "match_profile",
    "resolve_candidate_provider",
    "AcceptanceCriterion",
    "AttemptCountInputs",
    "HoldoutConsumedError",
    "PlateauNeighborResult",
    "PlateauResult",
    "ResearchAcceptanceConfig",
    "ResearchAcceptanceResult",
    "SeedRunRecord",
    "check_lockbox_consumption",
    "compute_champion_hash",
    "compute_effective_attempt_count",
    "evaluate_plateau_result",
    "evaluate_research_acceptance_from_evidence",
    "generate_parameter_neighbors",
    "record_lockbox_consumption",
    "research_acceptance_config_for_profile",
    "resolve_verdict",
    "run_research_acceptance",
]
