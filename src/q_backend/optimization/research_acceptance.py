"""Research acceptance layer: repeated-seed robustness, plateau, DSR, and verdict (WO163).

This module is separate from Discovery ``passed_gates`` ranking semantics.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from q_backend.optimization.dsr import deflated_sharpe_ratio
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    LogFloatParam,
    ObjectiveMode,
    SearchParam,
    SearchSpaceConfig,
)
from q_backend.optimization.objectives import resolve_objective
from q_backend.optimization.walkforward import WalkForwardWindowResult

AcceptanceVerdict = Literal["ready_for_paper", "inconclusive", "rejected"]
CriterionStatus = Literal["passed", "failed", "unavailable"]
SeedRunStatus = Literal["completed", "failed", "missing"]


class ResearchAcceptanceConfig(BaseModel):
    """Profile-aware thresholds for paper-candidate acceptance."""

    min_completed_oos_windows: int = Field(default=6, ge=1)
    min_stitched_oos_trades: int = Field(default=30, ge=0)
    require_positive_aggregate_oos_return: bool = True
    require_positive_median_window_return: bool = True
    optimization_seeds: int = Field(default=5, ge=1)
    min_positive_seed_outcomes: int = Field(default=4, ge=0)
    min_dsr: float = Field(default=0.95, ge=0.0, le=1.0)
    min_plateau_profitable_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    min_plateau_score_retention: float = Field(default=0.7, ge=0.0)
    lockbox_min_trades: int = Field(default=5, ge=0)
    lockbox_max_drawdown_pct: float | None = None
    require_positive_lockbox_return: bool = True
    require_positive_lockbox_sharpe: bool = True
    worst_window_return_gate: float | None = None
    max_neighbors: int = Field(default=24, ge=1)


def research_acceptance_config_for_profile(profile: Any) -> ResearchAcceptanceConfig:
    """Build acceptance defaults from an ``InstrumentResearchProfile``."""
    counts = getattr(profile, "minimum_observation_counts", {}) or {}
    overrides = getattr(profile, "research_acceptance", None)
    if overrides is not None:
        if isinstance(overrides, ResearchAcceptanceConfig):
            return overrides
        if isinstance(overrides, dict):
            return ResearchAcceptanceConfig(**overrides)
    return ResearchAcceptanceConfig(
        min_completed_oos_windows=int(counts.get("min_oos_windows", 6)),
        min_stitched_oos_trades=int(counts.get("min_trades", 30)),
    )


@dataclass
class AcceptanceCriterion:
    name: str
    status: CriterionStatus
    observed: Any
    threshold: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "observed": self.observed,
            "threshold": self.threshold,
            "reason": self.reason,
        }


@dataclass
class SeedRunRecord:
    seed: int
    status: SeedRunStatus
    study_config: dict[str, Any] | None = None
    best_params: dict[str, Any] | None = None
    oos_metrics: dict[str, Any] | None = None
    window_returns: list[float] = field(default_factory=list)
    window_count: int = 0
    completed_windows: int = 0
    objective_value: float | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "status": self.status,
            "study_config": self.study_config,
            "best_params": self.best_params,
            "oos_metrics": self.oos_metrics,
            "window_returns": self.window_returns,
            "window_count": self.window_count,
            "completed_windows": self.completed_windows,
            "objective_value": self.objective_value,
            "failure_reason": self.failure_reason,
        }


@dataclass
class PlateauNeighborResult:
    params: dict[str, Any]
    objective_value: float | None
    profitable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "objective_value": self.objective_value,
            "profitable": self.profitable,
        }


@dataclass
class PlateauResult:
    neighbors_evaluated: int
    profitable_fraction: float | None
    score_retention: float | None
    champion_objective: float | None
    neighbors: list[PlateauNeighborResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "neighbors_evaluated": self.neighbors_evaluated,
            "profitable_fraction": self.profitable_fraction,
            "score_retention": self.score_retention,
            "champion_objective": self.champion_objective,
            "neighbors": [neighbor.to_dict() for neighbor in self.neighbors],
        }


@dataclass
class TailDiagnostics:
    worst_window_return: float | None = None
    p25_window_return: float | None = None
    max_losing_window_streak: int | None = None
    trade_count_concentration: float | None = None
    regime_contribution: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "worst_window_return": self.worst_window_return,
            "p25_window_return": self.p25_window_return,
            "max_losing_window_streak": self.max_losing_window_streak,
            "trade_count_concentration": self.trade_count_concentration,
            "regime_contribution": self.regime_contribution,
        }


@dataclass
class AttemptCountInputs:
    """Conservative multiplicity inputs for DSR (WO163).

    Feature-search multiplicity is handled by WO162's permutation null floor when
    ``ProfileFeatureAdmissionResolver`` is active. Do not add ``feature_attempts``
    here unless feature evidence was skipped, or the same burden is double-counted.
    """

    hypothesis_count: int = 1
    genomes_evaluated: int = 0
    optuna_trials: int = 0
    optimization_seeds: int = 0


def compute_effective_attempt_count(inputs: AttemptCountInputs) -> int:
    """Conservative effective trial count for DSR deflation."""
    total = (
        max(inputs.hypothesis_count, 0)
        + max(inputs.genomes_evaluated, 0)
        + max(inputs.optuna_trials, 0)
        + max(inputs.optimization_seeds, 0)
    )
    return max(total, 1)


def compute_champion_hash(
    *,
    candidate_id: str,
    champion_params: dict[str, Any],
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "champion_params": champion_params,
    }
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def window_returns_from_results(
    windows: list[WalkForwardWindowResult],
    *,
    mode: ObjectiveMode = ObjectiveMode.MAXIMIZE_NET_PROFIT,
) -> list[float]:
    returns: list[float] = []
    for window in windows:
        if window.status != "completed" or not window.oos_metrics:
            continue
        returns.append(float(window.oos_metrics.get("total_return_pct", 0.0)))
    return returns


def compute_tail_diagnostics(
    *,
    window_returns: list[float],
    window_trade_counts: list[int] | None = None,
    regime_trade_pnl: dict[str, float] | None = None,
) -> TailDiagnostics:
    if not window_returns:
        return TailDiagnostics()

    series = pd.Series(window_returns)
    worst = float(series.min())
    p25 = float(series.quantile(0.25))

    losing_streak = 0
    max_streak = 0
    for value in window_returns:
        if value <= 0.0:
            losing_streak += 1
            max_streak = max(max_streak, losing_streak)
        else:
            losing_streak = 0

    concentration: float | None = None
    if window_trade_counts and sum(window_trade_counts) > 0:
        total = float(sum(window_trade_counts))
        concentration = float(max(window_trade_counts) / total)

    regime_contribution: dict[str, float] | None = None
    if regime_trade_pnl:
        total_abs = sum(abs(value) for value in regime_trade_pnl.values())
        if total_abs > 0:
            regime_contribution = {
                key: float(value / total_abs) for key, value in regime_trade_pnl.items()
            }

    return TailDiagnostics(
        worst_window_return=worst,
        p25_window_return=p25,
        max_losing_window_streak=max_streak,
        trade_count_concentration=concentration,
        regime_contribution=regime_contribution,
    )


def _neighbor_values(current: Any, spec: SearchParam) -> list[Any]:
    if isinstance(spec, IntParam):
        values: list[Any] = []
        for delta in (-spec.step, spec.step):
            candidate = int(current) + delta
            if spec.low <= candidate <= spec.high and candidate != int(current):
                values.append(candidate)
        return values
    if isinstance(spec, FloatParam):
        step = spec.step
        if step is None:
            span = spec.high - spec.low
            step = span / 20.0 if span > 0 else 0.1
        values = []
        for delta in (-step, step):
            candidate = float(current) + delta
            if spec.low <= candidate <= spec.high and not math.isclose(
                candidate, float(current)
            ):
                values.append(candidate)
        return values
    if isinstance(spec, LogFloatParam):
        current_f = float(current)
        if current_f <= 0:
            return []
        values = []
        for factor in (0.9, 1.1):
            candidate = current_f * factor
            if spec.low <= candidate <= spec.high and not math.isclose(
                candidate, current_f
            ):
                values.append(candidate)
        return values
    if isinstance(spec, CategoricalParam):
        return [choice for choice in spec.choices if choice != current]
    return []


def generate_parameter_neighbors(
    champion_params: dict[str, Any],
    search_space: SearchSpaceConfig,
    *,
    max_neighbors: int = 24,
) -> list[dict[str, Any]]:
    """Generate valid adjacent parameter configurations from declared search space."""
    neighbors: list[dict[str, Any]] = []
    strategy_params = dict(champion_params.get("strategy_params", {}))
    risk_params = dict(champion_params.get("risk_params", {}))
    manager_params = dict(champion_params.get("manager_params", {}))

    def _append_neighbor(
        section: str,
        key: str,
        value: Any,
    ) -> None:
        neighbor = copy.deepcopy(champion_params)
        bucket = dict(neighbor.get(section, {}))
        bucket[key] = value
        neighbor[section] = bucket
        neighbors.append(neighbor)

    for key, spec in search_space.strategy_params.items():
        if key not in strategy_params:
            continue
        for value in _neighbor_values(strategy_params[key], spec):
            _append_neighbor("strategy_params", key, value)
            if len(neighbors) >= max_neighbors:
                return neighbors

    for key, spec in search_space.risk_params.items():
        if key not in risk_params:
            continue
        for value in _neighbor_values(risk_params[key], spec):
            _append_neighbor("risk_params", key, value)
            if len(neighbors) >= max_neighbors:
                return neighbors

    for key, spec in search_space.manager_params.items():
        if key not in manager_params:
            continue
        for value in _neighbor_values(manager_params[key], spec):
            _append_neighbor("manager_params", key, value)
            if len(neighbors) >= max_neighbors:
                return neighbors

    return neighbors


def evaluate_plateau_result(
    *,
    champion_objective: float | None,
    neighbors: list[PlateauNeighborResult],
) -> PlateauResult:
    evaluated = [neighbor for neighbor in neighbors if neighbor.objective_value is not None]
    if not evaluated:
        return PlateauResult(
            neighbors_evaluated=0,
            profitable_fraction=None,
            score_retention=None,
            champion_objective=champion_objective,
            neighbors=neighbors,
        )

    profitable = [neighbor for neighbor in evaluated if neighbor.profitable]
    profitable_fraction = len(profitable) / len(evaluated)
    neighbor_objectives = [float(neighbor.objective_value) for neighbor in evaluated]
    median_neighbor = float(np.median(neighbor_objectives))
    retention: float | None = None
    if champion_objective is not None and champion_objective > 0:
        retention = median_neighbor / champion_objective

    return PlateauResult(
        neighbors_evaluated=len(evaluated),
        profitable_fraction=profitable_fraction,
        score_retention=retention,
        champion_objective=champion_objective,
        neighbors=neighbors,
    )


def compute_dsr_value(
    *,
    oos_metrics: dict[str, Any] | None,
    oos_equity_curve: pd.Series | None,
    attempt_count: int,
) -> float | None:
    if not oos_metrics:
        return None
    sr_observed = float(oos_metrics.get("sharpe_ratio", 0.0))
    returns: list[float] = []
    if oos_equity_curve is not None and len(oos_equity_curve) >= 2:
        returns = oos_equity_curve.pct_change().dropna().tolist()
    num_obs = max(len(returns), 2)
    skewness = 0.0
    kurtosis = 3.0
    if len(returns) >= 2:
        series = pd.Series(returns)
        skewness = float(series.skew())
        kurtosis = float(series.kurtosis())
    return deflated_sharpe_ratio(
        sr_observed=sr_observed,
        num_trials=attempt_count,
        num_observations=num_obs,
        skewness=skewness,
        kurtosis=kurtosis,
    )


def select_champion_seed(seeds: list[SeedRunRecord]) -> SeedRunRecord | None:
    completed = [seed for seed in seeds if seed.status == "completed"]
    if not completed:
        return None
    completed.sort(key=lambda seed: seed.objective_value or float("-inf"), reverse=True)
    return completed[0]


def build_acceptance_criteria(
    *,
    config: ResearchAcceptanceConfig,
    champion: SeedRunRecord | None,
    seeds: list[SeedRunRecord],
    plateau: PlateauResult | None,
    dsr_value: float | None,
    lockbox_metrics: dict[str, Any] | None,
    lockbox_evaluated: bool,
    lockbox_blocked: bool,
    tail: TailDiagnostics,
) -> list[AcceptanceCriterion]:
    criteria: list[AcceptanceCriterion] = []

    if champion is None or champion.status != "completed" or not champion.oos_metrics:
        criteria.append(
            AcceptanceCriterion(
                name="champion_seed",
                status="unavailable",
                observed=None,
                threshold="completed champion",
                reason="No completed optimization seed produced a champion.",
            )
        )
    else:
        metrics = champion.oos_metrics
        completed_windows = champion.completed_windows
        criteria.append(
            AcceptanceCriterion(
                name="oos_windows",
                status="passed"
                if completed_windows >= config.min_completed_oos_windows
                else "failed",
                observed=completed_windows,
                threshold=config.min_completed_oos_windows,
                reason="Completed walk-forward OOS windows.",
            )
        )
        oos_trades = int(metrics.get("total_trades", 0))
        criteria.append(
            AcceptanceCriterion(
                name="oos_trades",
                status="passed"
                if oos_trades >= config.min_stitched_oos_trades
                else "failed",
                observed=oos_trades,
                threshold=config.min_stitched_oos_trades,
                reason="Stitched OOS trade count.",
            )
        )
        aggregate_return = float(metrics.get("total_return_pct", 0.0))
        if config.require_positive_aggregate_oos_return:
            criteria.append(
                AcceptanceCriterion(
                    name="aggregate_oos_return",
                    status="passed" if aggregate_return > 0 else "failed",
                    observed=aggregate_return,
                    threshold="> 0",
                    reason="Aggregate stitched OOS return must be positive.",
                )
            )
        if champion.window_returns and config.require_positive_median_window_return:
            median_return = float(np.median(champion.window_returns))
            criteria.append(
                AcceptanceCriterion(
                    name="median_window_return",
                    status="passed" if median_return > 0 else "failed",
                    observed=median_return,
                    threshold="> 0",
                    reason="Median OOS-window return must be positive.",
                )
            )
        if config.worst_window_return_gate is not None and champion.window_returns:
            worst = min(champion.window_returns)
            criteria.append(
                AcceptanceCriterion(
                    name="worst_window_return",
                    status="passed"
                    if worst >= config.worst_window_return_gate
                    else "failed",
                    observed=worst,
                    threshold=config.worst_window_return_gate,
                    reason="Worst OOS-window return hard gate.",
                )
            )

    expected = config.optimization_seeds
    if len(seeds) < expected:
        criteria.append(
            AcceptanceCriterion(
                name="seed_robustness",
                status="unavailable",
                observed=len(seeds),
                threshold=expected,
                reason="Fewer seed runs than required.",
            )
        )
    else:
        evaluated = seeds[:expected]
        incomplete = [seed for seed in evaluated if seed.status != "completed"]
        if incomplete:
            criteria.append(
                AcceptanceCriterion(
                    name="seed_robustness",
                    status="unavailable",
                    observed={
                        "completed": sum(
                            1 for seed in evaluated if seed.status == "completed"
                        ),
                        "failed_or_missing": len(incomplete),
                    },
                    threshold={
                        "seeds": expected,
                        "min_positive": config.min_positive_seed_outcomes,
                    },
                    reason="Missing or failed seeds cannot be converted to zero outcomes.",
                )
            )
        else:
            positive = sum(
                1
                for seed in evaluated
                if float((seed.oos_metrics or {}).get("total_return_pct", 0.0)) > 0.0
            )
            criteria.append(
                AcceptanceCriterion(
                    name="seed_robustness",
                    status="passed"
                    if positive >= config.min_positive_seed_outcomes
                    else "failed",
                    observed=positive,
                    threshold=config.min_positive_seed_outcomes,
                    reason="Independent optimization seeds with positive OOS return.",
                )
            )

    if dsr_value is None:
        criteria.append(
            AcceptanceCriterion(
                name="dsr",
                status="unavailable",
                observed=None,
                threshold=config.min_dsr,
                reason="DSR could not be computed from champion evidence.",
            )
        )
    else:
        criteria.append(
            AcceptanceCriterion(
                name="dsr",
                status="passed" if dsr_value >= config.min_dsr else "failed",
                observed=dsr_value,
                threshold=config.min_dsr,
                reason="Deflated Sharpe Ratio after conservative attempt count.",
            )
        )

    if plateau is None or plateau.neighbors_evaluated == 0:
        criteria.append(
            AcceptanceCriterion(
                name="parameter_plateau",
                status="unavailable",
                observed=None,
                threshold={
                    "min_profitable_fraction": config.min_plateau_profitable_fraction,
                    "min_score_retention": config.min_plateau_score_retention,
                },
                reason="Parameter neighborhood could not be evaluated.",
            )
        )
    else:
        profitable_ok = (
            plateau.profitable_fraction is not None
            and plateau.profitable_fraction >= config.min_plateau_profitable_fraction
        )
        retention_ok = (
            plateau.score_retention is None
            or plateau.score_retention >= config.min_plateau_score_retention
        )
        passed = profitable_ok and retention_ok
        criteria.append(
            AcceptanceCriterion(
                name="parameter_plateau",
                status="passed" if passed else "failed",
                observed={
                    "profitable_fraction": plateau.profitable_fraction,
                    "score_retention": plateau.score_retention,
                    "neighbors_evaluated": plateau.neighbors_evaluated,
                },
                threshold={
                    "min_profitable_fraction": config.min_plateau_profitable_fraction,
                    "min_score_retention": config.min_plateau_score_retention,
                },
                reason="Champion must sit on a profitable parameter plateau.",
            )
        )

    if lockbox_blocked:
        criteria.append(
            AcceptanceCriterion(
                name="lockbox_holdout",
                status="unavailable",
                observed=None,
                threshold="single untouched evaluation",
                reason="Lock-box holdout already consumed for this split manifest.",
            )
        )
    elif not lockbox_evaluated:
        criteria.append(
            AcceptanceCriterion(
                name="lockbox_holdout",
                status="unavailable",
                observed=None,
                threshold="single untouched evaluation",
                reason="Lock-box has not been evaluated for the frozen champion.",
            )
        )
    elif lockbox_metrics is None:
        criteria.append(
            AcceptanceCriterion(
                name="lockbox_holdout",
                status="unavailable",
                observed=None,
                threshold="metrics present",
                reason="Lock-box evaluation produced no metrics.",
            )
        )
    else:
        trades = int(lockbox_metrics.get("total_trades", 0))
        criteria.append(
            AcceptanceCriterion(
                name="lockbox_trades",
                status="passed" if trades >= config.lockbox_min_trades else "failed",
                observed=trades,
                threshold=config.lockbox_min_trades,
                reason="Lock-box minimum trade count.",
            )
        )
        if config.lockbox_max_drawdown_pct is not None:
            drawdown = float(lockbox_metrics.get("max_drawdown_pct", 0.0))
            criteria.append(
                AcceptanceCriterion(
                    name="lockbox_drawdown",
                    status="passed"
                    if drawdown <= config.lockbox_max_drawdown_pct
                    else "failed",
                    observed=drawdown,
                    threshold=config.lockbox_max_drawdown_pct,
                    reason="Lock-box maximum drawdown.",
                )
            )
        if config.require_positive_lockbox_return:
            lockbox_return = float(lockbox_metrics.get("total_return_pct", 0.0))
            criteria.append(
                AcceptanceCriterion(
                    name="lockbox_return",
                    status="passed" if lockbox_return > 0 else "failed",
                    observed=lockbox_return,
                    threshold="> 0",
                    reason="Lock-box return must be positive.",
                )
            )
        if config.require_positive_lockbox_sharpe:
            lockbox_sharpe = float(lockbox_metrics.get("sharpe_ratio", 0.0))
            criteria.append(
                AcceptanceCriterion(
                    name="lockbox_sharpe",
                    status="passed" if lockbox_sharpe > 0 else "failed",
                    observed=lockbox_sharpe,
                    threshold="> 0",
                    reason="Lock-box Sharpe must be positive.",
                )
            )

    if tail.worst_window_return is not None:
        criteria.append(
            AcceptanceCriterion(
                name="tail_worst_window",
                status="passed",
                observed=tail.worst_window_return,
                threshold=None,
                reason="Lower-tail diagnostic (evidence only unless gated).",
            )
        )
    if tail.p25_window_return is not None:
        criteria.append(
            AcceptanceCriterion(
                name="tail_p25_window",
                status="passed",
                observed=tail.p25_window_return,
                threshold=None,
                reason="Lower-tail diagnostic (evidence only unless gated).",
            )
        )

    return criteria


_EVIDENCE_ONLY_CRITERIA = frozenset(
    {"tail_worst_window", "tail_p25_window"},
)


def resolve_verdict(criteria: list[AcceptanceCriterion]) -> AcceptanceVerdict:
    """Map structured criterion rows to the final acceptance verdict."""
    for criterion in criteria:
        if criterion.name in _EVIDENCE_ONLY_CRITERIA:
            continue
        if criterion.status == "unavailable":
            return "inconclusive"
    for criterion in criteria:
        if criterion.name in _EVIDENCE_ONLY_CRITERIA:
            continue
        if criterion.status == "failed":
            return "rejected"
    return "ready_for_paper"


@dataclass
class ResearchAcceptanceResult:
    acceptance_id: str
    candidate_id: str
    verdict: AcceptanceVerdict
    criteria: list[AcceptanceCriterion]
    champion_seed: int | None
    champion_hash: str | None
    seeds: list[SeedRunRecord]
    plateau: PlateauResult | None
    tail_diagnostics: TailDiagnostics
    dsr_value: float | None
    effective_attempt_count: int
    lockbox_metrics: dict[str, Any] | None
    manifest_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceptance_id": self.acceptance_id,
            "candidate_id": self.candidate_id,
            "verdict": self.verdict,
            "criteria": [row.to_dict() for row in self.criteria],
            "champion_seed": self.champion_seed,
            "champion_hash": self.champion_hash,
            "seeds": [seed.to_dict() for seed in self.seeds],
            "plateau": self.plateau.to_dict() if self.plateau else None,
            "tail_diagnostics": self.tail_diagnostics.to_dict(),
            "dsr_value": self.dsr_value,
            "effective_attempt_count": self.effective_attempt_count,
            "lockbox_metrics": self.lockbox_metrics,
            "manifest_hash": self.manifest_hash,
        }


def evaluate_research_acceptance_from_evidence(
    *,
    acceptance_id: str,
    candidate_id: str,
    config: ResearchAcceptanceConfig,
    seeds: list[SeedRunRecord],
    plateau: PlateauResult | None,
    dsr_value: float | None,
    effective_attempt_count: int,
    lockbox_metrics: dict[str, Any] | None,
    lockbox_evaluated: bool,
    lockbox_blocked: bool,
    champion_hash: str | None = None,
    manifest_hash: str | None = None,
    tail: TailDiagnostics | None = None,
) -> ResearchAcceptanceResult:
    """Pure acceptance evaluation from precomputed seed/plateau/lockbox evidence."""
    champion = select_champion_seed(seeds)
    tail_diagnostics = tail or compute_tail_diagnostics(
        window_returns=champion.window_returns if champion else [],
    )
    criteria = build_acceptance_criteria(
        config=config,
        champion=champion,
        seeds=seeds,
        plateau=plateau,
        dsr_value=dsr_value,
        lockbox_metrics=lockbox_metrics,
        lockbox_evaluated=lockbox_evaluated,
        lockbox_blocked=lockbox_blocked,
        tail=tail_diagnostics,
    )
    verdict = resolve_verdict(criteria)
    return ResearchAcceptanceResult(
        acceptance_id=acceptance_id,
        candidate_id=candidate_id,
        verdict=verdict,
        criteria=criteria,
        champion_seed=champion.seed if champion else None,
        champion_hash=champion_hash,
        seeds=seeds,
        plateau=plateau,
        tail_diagnostics=tail_diagnostics,
        dsr_value=dsr_value,
        effective_attempt_count=effective_attempt_count,
        lockbox_metrics=lockbox_metrics,
        manifest_hash=manifest_hash,
    )
