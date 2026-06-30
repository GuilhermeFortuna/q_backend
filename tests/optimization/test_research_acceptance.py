"""Tests for WO163 research acceptance, plateau, DSR, and lock-box consumption."""

from __future__ import annotations

import json

import pytest

from q_backend.optimization.dsr import deflated_sharpe_ratio
from q_backend.optimization.hypothesis import RESEARCH_PROFILES
from q_backend.optimization.lockbox_consumption import (
    HoldoutConsumedError,
    check_lockbox_consumption,
    record_lockbox_consumption,
)
from q_backend.optimization.models import (
    CategoricalParam,
    IntParam,
    SearchSpaceConfig,
)
from q_backend.optimization.research_acceptance import (
    AcceptanceCriterion,
    AttemptCountInputs,
    PlateauNeighborResult,
    PlateauResult,
    ResearchAcceptanceConfig,
    SeedRunRecord,
    compute_champion_hash,
    compute_effective_attempt_count,
    compute_tail_diagnostics,
    evaluate_plateau_result,
    evaluate_research_acceptance_from_evidence,
    generate_parameter_neighbors,
    research_acceptance_config_for_profile,
    resolve_verdict,
)
from q_backend.optimization.strategy_search import GateConfig, _apply_gates
from q_backend.storage.lake.artifacts import (
    read_lockbox_consumption,
    read_research_acceptance_result,
)


def _completed_seed(
    seed: int,
    *,
    return_pct: float = 0.05,
    trades: int = 40,
    windows: int = 6,
    sharpe: float = 1.2,
) -> SeedRunRecord:
    window_returns = [return_pct] * windows
    return SeedRunRecord(
        seed=seed,
        status="completed",
        study_config={"seed": seed},
        best_params={"strategy_params": {"period": 20 + seed}},
        oos_metrics={
            "total_return_pct": return_pct,
            "total_trades": trades,
            "sharpe_ratio": sharpe,
        },
        window_returns=window_returns,
        window_count=windows,
        completed_windows=windows,
        objective_value=return_pct,
    )


def _acceptance_config() -> ResearchAcceptanceConfig:
    return ResearchAcceptanceConfig()


def _positive_plateau() -> PlateauResult:
    neighbors = [
        PlateauNeighborResult(params={"strategy_params": {"period": 19}}, objective_value=0.04, profitable=True),
        PlateauNeighborResult(params={"strategy_params": {"period": 21}}, objective_value=0.045, profitable=True),
        PlateauNeighborResult(params={"strategy_params": {"period": 22}}, objective_value=0.03, profitable=True),
        PlateauNeighborResult(params={"strategy_params": {"period": 18}}, objective_value=0.02, profitable=True),
    ]
    return evaluate_plateau_result(champion_objective=0.05, neighbors=neighbors)


def _isolated_plateau() -> PlateauResult:
    neighbors = [
        PlateauNeighborResult(params={"strategy_params": {"period": 19}}, objective_value=-0.02, profitable=False),
        PlateauNeighborResult(params={"strategy_params": {"period": 21}}, objective_value=-0.01, profitable=False),
        PlateauNeighborResult(params={"strategy_params": {"period": 22}}, objective_value=0.01, profitable=True),
        PlateauNeighborResult(params={"strategy_params": {"period": 18}}, objective_value=-0.03, profitable=False),
    ]
    return evaluate_plateau_result(champion_objective=0.08, neighbors=neighbors)


def _lockbox_metrics() -> dict:
    return {
        "total_return_pct": 0.03,
        "sharpe_ratio": 0.8,
        "total_trades": 8,
        "max_drawdown_pct": 0.08,
    }


def test_profiles_expose_research_acceptance_defaults():
    for profile_id in ("ccm_h1_swing", "win_h1_swing", "wdo_m15_day"):
        profile = RESEARCH_PROFILES[profile_id]
        config = research_acceptance_config_for_profile(profile)
        assert config.min_completed_oos_windows == 6
        if profile_id == "wdo_m15_day":
            assert config.min_stitched_oos_trades == 100
        else:
            assert config.min_stitched_oos_trades == 30
        assert config.optimization_seeds == 5
        assert config.min_positive_seed_outcomes == 4
        assert config.min_dsr == 0.95


def test_ready_for_paper_positive_fixture():
    seeds = [_completed_seed(seed) for seed in range(5)]
    dsr = max(
        deflated_sharpe_ratio(
            sr_observed=1.2,
            num_trials=compute_effective_attempt_count(
                AttemptCountInputs(hypothesis_count=1, optuna_trials=50, optimization_seeds=5)
            ),
            num_observations=120,
        ),
        0.96,
    )
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-ready",
        candidate_id="ccm_h1_swing_v1_breakout",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_positive_plateau(),
        dsr_value=dsr,
        effective_attempt_count=56,
        lockbox_metrics=_lockbox_metrics(),
        lockbox_evaluated=True,
        lockbox_blocked=False,
        champion_hash="champion-a",
        manifest_hash="manifest-a",
    )
    assert result.verdict == "ready_for_paper"
    assert result.champion_seed is not None


def test_best_of_weak_population_rejected():
    seeds = [_completed_seed(seed, return_pct=-0.02) for seed in range(5)]
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-weak",
        candidate_id="weak",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_positive_plateau(),
        dsr_value=0.99,
        effective_attempt_count=10,
        lockbox_metrics=_lockbox_metrics(),
        lockbox_evaluated=True,
        lockbox_blocked=False,
    )
    assert result.verdict == "rejected"
    failed = {row.name for row in result.criteria if row.status == "failed"}
    assert "aggregate_oos_return" in failed
    assert "seed_robustness" in failed


def test_dsr_below_threshold_rejected():
    seeds = [_completed_seed(seed) for seed in range(5)]
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-dsr",
        candidate_id="high-score",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_positive_plateau(),
        dsr_value=0.50,
        effective_attempt_count=200,
        lockbox_metrics=_lockbox_metrics(),
        lockbox_evaluated=True,
        lockbox_blocked=False,
    )
    assert result.verdict == "rejected"
    dsr_row = next(row for row in result.criteria if row.name == "dsr")
    assert dsr_row.status == "failed"


def test_isolated_plateau_fails_acceptance():
    seeds = [_completed_seed(seed) for seed in range(5)]
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-plateau-fail",
        candidate_id="spike",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_isolated_plateau(),
        dsr_value=0.99,
        effective_attempt_count=10,
        lockbox_metrics=_lockbox_metrics(),
        lockbox_evaluated=True,
        lockbox_blocked=False,
    )
    plateau_row = next(row for row in result.criteria if row.name == "parameter_plateau")
    assert plateau_row.status == "failed"
    assert result.verdict == "rejected"


def test_broad_plateau_passes_acceptance():
    plateau = _positive_plateau()
    assert plateau.profitable_fraction == 1.0
    assert plateau.score_retention is not None
    assert plateau.score_retention >= 0.7


def test_four_completed_seeds_plus_one_failed_is_inconclusive():
    seeds = [_completed_seed(seed) for seed in range(4)]
    seeds.append(
        SeedRunRecord(seed=44, status="failed", failure_reason="optimizer crashed")
    )
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-inconclusive",
        candidate_id="partial",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_positive_plateau(),
        dsr_value=0.99,
        effective_attempt_count=10,
        lockbox_metrics=_lockbox_metrics(),
        lockbox_evaluated=True,
        lockbox_blocked=False,
    )
    seed_row = next(row for row in result.criteria if row.name == "seed_robustness")
    assert seed_row.status == "unavailable"
    assert result.verdict == "inconclusive"


def test_holdout_consumption_refuses_modified_champion(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_LAKE_ROOT", str(tmp_path))
    manifest_hash = "manifest-consumed"
    first_hash = compute_champion_hash(
        candidate_id="hyp-a",
        champion_params={"strategy_params": {"period": 20}},
    )
    second_hash = compute_champion_hash(
        candidate_id="hyp-a",
        champion_params={"strategy_params": {"period": 21}},
    )
    record_lockbox_consumption(
        manifest_hash=manifest_hash,
        champion_hash=first_hash,
        lockbox_metrics=_lockbox_metrics(),
        verdict="rejected",
        candidate_id="hyp-a",
    )
    check = check_lockbox_consumption(manifest_hash, second_hash)
    assert check.state.value == "manifest_consumed"
    with pytest.raises(HoldoutConsumedError):
        record_lockbox_consumption(
            manifest_hash=manifest_hash,
            champion_hash=second_hash,
            lockbox_metrics=_lockbox_metrics(),
            verdict="ready_for_paper",
            candidate_id="hyp-a",
        )
    stored = read_lockbox_consumption(manifest_hash)
    assert stored["champion_hash"] == first_hash


def test_criterion_serialization_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_LAKE_ROOT", str(tmp_path))
    seeds = [_completed_seed(seed) for seed in range(5)]
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-serialize",
        candidate_id="serialize",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_positive_plateau(),
        dsr_value=0.99,
        effective_attempt_count=10,
        lockbox_metrics=_lockbox_metrics(),
        lockbox_evaluated=True,
        lockbox_blocked=False,
    )
    payload = result.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    roundtrip = json.loads(serialized)
    assert roundtrip["verdict"] == result.verdict
    assert len(roundtrip["criteria"]) == len(result.criteria)
    from q_backend.storage.lake.artifacts import write_research_acceptance_result

    write_research_acceptance_result("acc-serialize", payload)
    stored = read_research_acceptance_result("acc-serialize")
    assert stored["acceptance_id"] == "acc-serialize"


def test_discovery_passed_gates_unchanged():
    flags, passed = _apply_gates(
        completed_windows=3,
        oos_metrics={"total_trades": 20},
        efficiency=0.8,
        gates=GateConfig(min_completed_windows=2, min_oos_trades=10),
        mean_is=1.0,
        oos_objective=0.5,
    )
    assert passed is True
    assert flags == []


def test_generate_parameter_neighbors_respects_search_space():
    champion = {"strategy_params": {"period": 20, "mode": "fast"}}
    search_space = SearchSpaceConfig(
        strategy_params={
            "period": IntParam(low=10, high=30, step=2),
            "mode": CategoricalParam(choices=["fast", "slow"]),
        }
    )
    neighbors = generate_parameter_neighbors(champion, search_space, max_neighbors=10)
    assert neighbors
    periods = {
        neighbor["strategy_params"]["period"]
        for neighbor in neighbors
        if "period" in neighbor.get("strategy_params", {})
    }
    assert 18 in periods or 22 in periods


def test_compute_effective_attempt_count_documents_feature_boundary():
    count = compute_effective_attempt_count(
        AttemptCountInputs(
            hypothesis_count=3,
            genomes_evaluated=48,
            optuna_trials=250,
            optimization_seeds=5,
        )
    )
    assert count == 306


def test_tail_diagnostics_and_resolve_verdict():
    tail = compute_tail_diagnostics(
        window_returns=[0.02, -0.01, -0.02, 0.03, 0.01],
        window_trade_counts=[10, 5, 20, 8, 7],
    )
    assert tail.worst_window_return == -0.02
    assert tail.max_losing_window_streak == 2
    assert tail.trade_count_concentration == pytest.approx(20 / 50)

    criteria = [
        AcceptanceCriterion("dsr", "failed", 0.5, 0.95, "too low"),
    ]
    assert resolve_verdict(criteria) == "rejected"


def test_lockbox_blocked_yields_inconclusive():
    seeds = [_completed_seed(seed) for seed in range(5)]
    result = evaluate_research_acceptance_from_evidence(
        acceptance_id="acc-blocked",
        candidate_id="blocked",
        config=_acceptance_config(),
        seeds=seeds,
        plateau=_positive_plateau(),
        dsr_value=0.99,
        effective_attempt_count=10,
        lockbox_metrics=None,
        lockbox_evaluated=False,
        lockbox_blocked=True,
        manifest_hash="manifest-blocked",
    )
    holdout_row = next(row for row in result.criteria if row.name == "lockbox_holdout")
    assert holdout_row.status == "unavailable"
    assert result.verdict == "inconclusive"
