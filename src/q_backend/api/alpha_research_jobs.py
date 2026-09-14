"""Job manager for instrument alpha-research experiments (WO164)."""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import pandas as pd

from q_backend.alpha_research.config import build_alpha_search_config
from q_backend.alpha_research.preflight import run_data_preflight
from q_backend.api.schemas.experiments import (
    AlphaResearchRequest,
    AlphaResearchResult,
    AlphaResearchStageStatus,
)
from q_backend.features.split_manifest import SplitManifest
from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.feature_admission import create_profile_admission_resolver
from q_backend.optimization.hypothesis import (
    HypothesisCandidateProvider,
    RESEARCH_PROFILES,
    extract_hypothesis_metadata,
)
from q_backend.optimization.research_acceptance import research_acceptance_config_for_profile
from q_backend.optimization.lockbox_consumption import (
    LockboxConsumptionState,
    check_lockbox_consumption,
    record_lockbox_consumption,
)
from q_backend.optimization.research_acceptance import (
    AttemptCountInputs,
    ResearchAcceptanceResult,
    compute_champion_hash,
    evaluate_research_acceptance_from_evidence,
    select_champion_seed,
)
from q_backend.optimization.research_acceptance_service import run_research_acceptance
from q_backend.features.evidence_service import run_profile_feature_evidence
from q_backend.storage.db.engine import session_scope
from q_backend.storage.lake.artifacts import (
    read_alpha_research_checkpoint,
    read_alpha_research_result,
    write_alpha_research_artifact,
    write_alpha_research_checkpoint,
    write_alpha_research_result,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import (
    DEFAULT_PROGRESS_TTL_SECONDS,
    get_job_progress,
    set_job_progress,
)
from q_backend.streaming.jobs import record_job_terminal
from q_backend.tasks.fanin import is_cancelled, set_cancelled

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "alpha_research"
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]

_STAGE_ORDER = (
    "preflight",
    "feature_evidence",
    "hypothesis_eligibility",
    "candidate_evaluation",
    "acceptance",
    "lockbox",
    "complete",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _backend_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:  # noqa: BLE001 - version stamp is best-effort
        # Best-effort: a git hash is a diagnostic stamp, not required for the
        # run. Any failure (not a repo, git missing, timeout) degrades to
        # "unknown"; log at debug so it is visible without adding noise.
        logger.debug("Could not resolve backend git revision", exc_info=True)
        return "unknown"


def _persist_progress(job_id: str, payload: dict[str, Any]) -> None:
    try:
        set_job_progress(
            get_redis(),
            job_id,
            payload,
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.debug("Redis progress unavailable for alpha-research job %s", job_id)


def _base_payload(
    job_id: str,
    *,
    status: JobStatus,
    progress: float = 0.0,
    detail: str | None = None,
    stages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "detail": detail,
        "stages": stages or [],
        "result": None,
        "error": None,
        "checkpoint": None,
    }


def _stage_status(
    name: str,
    status: Literal["pending", "running", "completed", "failed", "skipped"],
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    return AlphaResearchStageStatus(name=name, status=status, detail=detail).model_dump()


def _load_checkpoint(job_id: str) -> dict[str, Any]:
    try:
        return read_alpha_research_checkpoint(job_id)
    except FileNotFoundError:
        return {}


def _save_checkpoint(job_id: str, checkpoint: dict[str, Any]) -> None:
    write_alpha_research_checkpoint(job_id, checkpoint)


def _manifest_from_checkpoint(checkpoint: dict[str, Any]) -> SplitManifest | None:
    raw = checkpoint.get("manifest")
    if not raw:
        return None
    evidence = raw["evidence"]
    walkforward = raw["walkforward"]
    lockbox = raw["lockbox"]
    from q_backend.features.split_manifest import SplitSegment

    return SplitManifest(
        symbol=raw["symbol"],
        timeframe=raw["timeframe"],
        range_start=datetime.fromisoformat(raw["range_start"].replace("Z", "+00:00")),
        range_end=datetime.fromisoformat(raw["range_end"].replace("Z", "+00:00")),
        evidence=SplitSegment(
            name=evidence["name"],
            start=datetime.fromisoformat(evidence["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(evidence["end"].replace("Z", "+00:00")),
            bar_count=int(evidence["bar_count"]),
        ),
        walkforward=SplitSegment(
            name=walkforward["name"],
            start=datetime.fromisoformat(walkforward["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(walkforward["end"].replace("Z", "+00:00")),
            bar_count=int(walkforward["bar_count"]),
        ),
        lockbox=SplitSegment(
            name=lockbox["name"],
            start=datetime.fromisoformat(lockbox["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(lockbox["end"].replace("Z", "+00:00")),
            bar_count=int(lockbox["bar_count"]),
        ),
        fractions=tuple(raw["fractions"]),
        manifest_hash=raw["manifest_hash"],
        data_fingerprint=raw["data_fingerprint"],
    )


def _build_provenance(
    *,
    request: AlphaResearchRequest,
    profile_version: int,
    manifest: SplitManifest,
    seeds: list[int],
) -> dict[str, Any]:
    return {
        "backend_version": _backend_version(),
        "profile_id": request.profile_id,
        "profile_version": profile_version,
        "catalog_version": request.catalog_version or 1,
        "requested_range": {
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
        },
        "split_manifest_hash": manifest.manifest_hash,
        "data_fingerprint": manifest.data_fingerprint,
        "optimization_seeds": seeds,
        "target_name": request.target_name,
        "started_at": _now_iso(),
    }


def _finish_inconclusive(
    *,
    job_id: str,
    request: AlphaResearchRequest,
    stages: list[dict[str, Any]],
    reasons: list[str],
    checkpoint: dict[str, Any],
    coverage: dict[str, Any] | None = None,
) -> None:
    provenance = {
        "backend_version": _backend_version(),
        "profile_id": request.profile_id,
        "catalog_version": request.catalog_version or 1,
    }
    result = AlphaResearchResult(
        verdict="inconclusive",
        profile_id=request.profile_id,
        provenance=provenance,
        split_manifest=checkpoint.get("manifest"),
        feature_evidence_summary=checkpoint.get("feature_evidence_summary", []),
        hypothesis_manifest=checkpoint.get("hypothesis_manifest", []),
        champion=None,
        acceptance=None,
        stages=[AlphaResearchStageStatus.model_validate(stage) for stage in stages],
        inconclusive_reasons=reasons,
        coverage=coverage or checkpoint.get("coverage", {}),
    )
    payload = result.model_dump(mode="json")
    write_alpha_research_result(job_id, payload)
    _save_checkpoint(job_id, checkpoint)
    try:
        with session_scope() as session:
            record_job_terminal(
                session,
                kind="alpha_research",
                job_id=job_id,
                raw_status="completed",
            )
    except Exception as term_exc:  # noqa: BLE001
        logger.warning("Failed to record terminal event for alpha research job %s: %s", job_id, term_exc)
    _persist_progress(
        job_id,
        {
            **_base_payload(
                job_id,
                status="completed",
                progress=1.0,
                detail="; ".join(reasons),
                stages=stages,
            ),
            "result": payload,
            "checkpoint": checkpoint,
        },
    )


def _select_frozen_candidate(
    acceptance_by_candidate: dict[str, ResearchAcceptanceResult],
) -> tuple[str | None, ResearchAcceptanceResult | None]:
    best_id: str | None = None
    best_result: ResearchAcceptanceResult | None = None
    best_score = float("-inf")
    for candidate_id, result in acceptance_by_candidate.items():
        champion = select_champion_seed(result.seeds)
        if champion is None or champion.objective_value is None:
            continue
        if champion.objective_value > best_score:
            best_score = champion.objective_value
            best_id = candidate_id
            best_result = result
    return best_id, best_result


def start_alpha_research_job(*, request: AlphaResearchRequest) -> dict[str, str]:
    job_id = str(uuid.uuid4())
    checkpoint = {
        "request": request.model_dump(mode="json"),
        "job_id": job_id,
    }
    write_alpha_research_checkpoint(job_id, checkpoint)
    _persist_progress(
        job_id,
        _base_payload(job_id, status="queued", progress=0.0, detail="queued"),
    )
    from q_backend.tasks import actors

    actors.run_alpha_research.send(job_id, request.model_dump_json())
    return {"job_id": job_id, "status": "queued"}


def request_cancel(job_id: str) -> bool:
    set_cancelled(job_id)
    checkpoint = _load_checkpoint(job_id)
    for child_run_id in checkpoint.get("child_run_ids", []):
        from q_backend.api import strategy_search_jobs

        strategy_search_jobs.request_cancel(child_run_id)
    try:
        with session_scope() as session:
            record_job_terminal(
                session,
                kind="alpha_research",
                job_id=job_id,
                raw_status="cancelled",
                error="Cancelled by operator.",
            )
    except Exception as term_exc:  # noqa: BLE001
        logger.warning("Failed to record terminal cancellation for alpha research job %s: %s", job_id, term_exc)
    _persist_progress(
        job_id,
        {
            **_base_payload(job_id, status="cancelled", progress=1.0, detail="cancelled"),
            "error": "Cancelled by operator.",
            "checkpoint": checkpoint,
        },
    )
    return True


def run_alpha_research_job(job_id: str, request_json: str, *, resume: bool = False) -> None:
    """Worker entry: orchestrate the alpha-research funnel with resumable checkpoints."""
    request = AlphaResearchRequest.model_validate_json(request_json)
    checkpoint = _load_checkpoint(job_id) if resume else _load_checkpoint(job_id)
    if not checkpoint:
        checkpoint = {"request": request.model_dump(mode="json"), "job_id": job_id}

    stages: list[dict[str, Any]] = list(checkpoint.get("stages", []))
    profile = RESEARCH_PROFILES.get(request.profile_id)
    acceptance_config = research_acceptance_config_for_profile(profile) if profile is not None else None
    budget = request.compute_budget
    if budget is not None and acceptance_config is not None:
        if budget.optimization_seeds is not None:
            acceptance_config = acceptance_config.model_copy(update={"optimization_seeds": budget.optimization_seeds})

    def _raise_if_cancelled(stage_name: str) -> None:
        if is_cancelled(job_id):
            checkpoint["cancelled_at_stage"] = stage_name
            _save_checkpoint(job_id, checkpoint)
            raise RuntimeError(f"Alpha-research job {job_id} cancelled at {stage_name}.")

    def _set_stage(
        name: str,
        status: Literal["pending", "running", "completed", "failed", "skipped"],
        *,
        detail: str | None = None,
    ) -> None:
        updated = [stage for stage in stages if stage["name"] != name]
        updated.append(_stage_status(name, status, detail=detail))
        stages[:] = sorted(updated, key=lambda item: _STAGE_ORDER.index(item["name"]))

    try:
        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status="running", progress=0.02, stages=stages),
                "checkpoint": checkpoint,
            },
        )

        # --- preflight ---
        if not checkpoint.get("preflight_complete"):
            _set_stage("preflight", "running")
            preflight = run_data_preflight(
                profile_id=request.profile_id,
                start=request.start,
                end=request.end,
                profile_version=request.profile_version,
            )
            checkpoint["coverage"] = preflight.coverage
            if preflight.verdict != "ok" or preflight.manifest is None:
                _set_stage("preflight", "failed", detail="; ".join(preflight.reasons))
                checkpoint["preflight_complete"] = True
                _finish_inconclusive(
                    job_id=job_id,
                    request=request,
                    stages=stages,
                    reasons=preflight.reasons,
                    checkpoint=checkpoint,
                    coverage=preflight.coverage,
                )
                return
            checkpoint["manifest"] = preflight.manifest.to_dict()
            checkpoint["preflight_complete"] = True
            _set_stage("preflight", "completed")
            _save_checkpoint(job_id, checkpoint)

        _raise_if_cancelled("preflight")
        manifest = _manifest_from_checkpoint(checkpoint)
        if manifest is None:
            raise RuntimeError("Preflight manifest missing from checkpoint.")

        profile = RESEARCH_PROFILES[request.profile_id]
        assert acceptance_config is not None
        study_n_trials = 30
        if budget is not None and budget.study_n_trials is not None:
            study_n_trials = budget.study_n_trials
        search_config = build_alpha_search_config(
            profile,
            manifest,
            study_n_trials=study_n_trials,
        )
        if budget is not None and budget.walkforward is not None:
            search_config = search_config.model_copy(update={"walkforward": budget.walkforward})

        base_seed = search_config.study.seed
        seed_count = acceptance_config.optimization_seeds
        seeds = [base_seed + index for index in range(seed_count)]

        # --- feature evidence ---
        if not checkpoint.get("feature_evidence_complete"):
            _set_stage("feature_evidence", "running")
            with session_scope() as session:
                evidences = run_profile_feature_evidence(
                    session,
                    profile_id=request.profile_id,
                    start=request.start,
                    end=request.end,
                    target_name=request.target_name,
                )
            checkpoint["feature_evidence_summary"] = [
                {
                    "feature_name": evidence.feature_name,
                    "feature_id": evidence.key.feature_id,
                    "decision": evidence.decision,
                    "deflated_score": evidence.deflated_score,
                    "n_obs": evidence.n_obs,
                }
                for evidence in evidences
            ]
            write_alpha_research_artifact(
                job_id,
                "feature_evidence",
                {"rows": checkpoint["feature_evidence_summary"]},
            )
            checkpoint["feature_evidence_complete"] = True
            _set_stage("feature_evidence", "completed")
            _save_checkpoint(job_id, checkpoint)

        _raise_if_cancelled("feature_evidence")

        # --- hypothesis eligibility ---
        if not checkpoint.get("hypothesis_manifest"):
            _set_stage("hypothesis_eligibility", "running")
            with session_scope() as session:
                resolver = create_profile_admission_resolver(
                    session,
                    profile_id=request.profile_id,
                    split_manifest_hash=manifest.manifest_hash,
                    data_fingerprint=manifest.data_fingerprint,
                    target_name=request.target_name,
                )
                provider = HypothesisCandidateProvider(search_config, resolver)
                candidates = list(provider.candidates())
                metadata = provider.candidate_metadata()
            checkpoint["hypothesis_manifest"] = [
                {
                    "candidate_id": candidate.candidate_id,
                    "strategy": candidate.strategy,
                    **metadata.get(candidate.candidate_id, {}),
                }
                for candidate in candidates
            ]
            checkpoint["candidate_specs"] = [
                {
                    "candidate_id": candidate.candidate_id,
                    "strategy": candidate.strategy,
                    "fixed_params": candidate.fixed_params,
                    "search_space": candidate.search_space.model_dump(mode="json"),
                }
                for candidate in candidates
            ]
            write_alpha_research_artifact(
                job_id,
                "hypothesis_manifest",
                {"hypotheses": checkpoint["hypothesis_manifest"]},
            )
            if not candidates:
                _set_stage("hypothesis_eligibility", "failed", detail="no admitted hypotheses")
                _finish_inconclusive(
                    job_id=job_id,
                    request=request,
                    stages=stages,
                    reasons=["No hypotheses passed feature-admission for this manifest."],
                    checkpoint=checkpoint,
                )
                return
            _set_stage("hypothesis_eligibility", "completed")
            _save_checkpoint(job_id, checkpoint)
        else:
            _set_stage("hypothesis_eligibility", "completed")

        _raise_if_cancelled("hypothesis_eligibility")

        # --- candidate evaluation (repeated seeds, no lock-box) ---
        from q_backend.optimization.strategy_search import SearchCandidate, SearchSpaceConfig

        acceptance_by_candidate: dict[str, ResearchAcceptanceResult] = {}
        if not checkpoint.get("candidate_evaluation_complete"):
            _set_stage("candidate_evaluation", "running")
            candidate_specs = checkpoint.get("candidate_specs", [])
            ohlcv = None
            if checkpoint.get("ohlcv_cached"):
                ohlcv = pd.read_json(checkpoint["ohlcv_cached"], orient="split")
            runner = DefaultBacktestRunner()
            for index, spec in enumerate(candidate_specs):
                _raise_if_cancelled("candidate_evaluation")
                candidate = SearchCandidate(
                    candidate_id=spec["candidate_id"],
                    strategy=spec["strategy"],
                    search_space=SearchSpaceConfig.model_validate(spec["search_space"]),
                    fixed_params=spec["fixed_params"],
                )
                acceptance_id = f"{job_id}__{candidate.candidate_id}"
                result = run_research_acceptance(
                    candidate=candidate,
                    config=search_config,
                    acceptance_config=acceptance_config,
                    backtest_runner=runner,
                    manifest=manifest,
                    attempt_inputs=AttemptCountInputs(
                        hypothesis_count=len(candidate_specs),
                        optuna_trials=search_config.study.n_trials * seed_count,
                        optimization_seeds=seed_count,
                    ),
                    seeds=seeds,
                    ohlcv=ohlcv,
                    acceptance_id=acceptance_id,
                    persist=True,
                    run_lockbox=False,
                )
                acceptance_by_candidate[candidate.candidate_id] = result
                checkpoint.setdefault("acceptance_ids", {})[candidate.candidate_id] = acceptance_id
                _persist_progress(
                    job_id,
                    {
                        **_base_payload(
                            job_id,
                            status="running",
                            progress=0.2 + 0.5 * (index + 1) / max(len(candidate_specs), 1),
                            stages=stages,
                            detail=f"evaluated {candidate.candidate_id}",
                        ),
                        "checkpoint": checkpoint,
                    },
                )
            checkpoint["candidate_evaluation_complete"] = True
            checkpoint["acceptance_summaries"] = {
                candidate_id: result.to_dict() for candidate_id, result in acceptance_by_candidate.items()
            }
            write_alpha_research_artifact(
                job_id,
                "candidate_acceptance",
                checkpoint["acceptance_summaries"],
            )
            _set_stage("candidate_evaluation", "completed")
            _save_checkpoint(job_id, checkpoint)
        else:
            _set_stage("candidate_evaluation", "completed")
            summaries = checkpoint.get("acceptance_summaries", {})
            for candidate_id, summary in summaries.items():
                acceptance_by_candidate[candidate_id] = _result_from_dict(summary)

        _raise_if_cancelled("candidate_evaluation")

        frozen_id, frozen_acceptance = _select_frozen_candidate(acceptance_by_candidate)
        if frozen_id is None or frozen_acceptance is None:
            _set_stage("acceptance", "failed", detail="no champion candidate")
            _finish_inconclusive(
                job_id=job_id,
                request=request,
                stages=stages,
                reasons=["No hypothesis produced a completed champion seed."],
                checkpoint=checkpoint,
            )
            return

        champion_seed = select_champion_seed(frozen_acceptance.seeds)
        champion_params = champion_seed.best_params if champion_seed else None
        champion_hash = (
            compute_champion_hash(
                candidate_id=frozen_id,
                champion_params=champion_params,
            )
            if champion_params
            else None
        )
        checkpoint["frozen_champion"] = {
            "candidate_id": frozen_id,
            "champion_seed": champion_seed.seed if champion_seed else None,
            "champion_hash": champion_hash,
            "best_params": champion_params,
        }
        write_alpha_research_artifact(job_id, "frozen_champion", checkpoint["frozen_champion"])

        # --- lock-box once ---
        lockbox_metrics: dict[str, Any] | None = None
        lockbox_evaluated = False
        lockbox_blocked = False
        if checkpoint.get("lockbox_consumed"):
            lockbox_evaluated = True
            lockbox_metrics = checkpoint.get("lockbox_metrics")
        elif champion_hash is not None:
            _set_stage("lockbox", "running")
            consumption = check_lockbox_consumption(manifest.manifest_hash, champion_hash)
            if consumption.state == LockboxConsumptionState.MANIFEST_CONSUMED:
                lockbox_blocked = True
            elif consumption.state == LockboxConsumptionState.SAME_CHAMPION:
                lockbox_evaluated = True
                lockbox_metrics = consumption.record.lockbox_metrics if consumption.record else None
                checkpoint["lockbox_consumed"] = True
                checkpoint["lockbox_metrics"] = lockbox_metrics
            else:
                from q_backend.optimization.lockbox import evaluate_lockbox as run_lockbox_evaluation
                from q_backend.optimization.strategy_search import SearchCandidate

                spec = next(item for item in checkpoint["candidate_specs"] if item["candidate_id"] == frozen_id)
                candidate = SearchCandidate(
                    candidate_id=spec["candidate_id"],
                    strategy=spec["strategy"],
                    search_space=SearchSpaceConfig.model_validate(spec["search_space"]),
                    fixed_params=spec["fixed_params"],
                )
                lockbox_params = dict(champion_params or {})
                genome = candidate.fixed_params.get("genome")
                if genome is not None:
                    strategy_params = dict(lockbox_params.get("strategy_params", {}))
                    strategy_params["genome"] = genome
                    lockbox_params["strategy_params"] = strategy_params
                runner = DefaultBacktestRunner()
                lockbox_metrics, _, _ = run_lockbox_evaluation(
                    backtest=search_config.backtest,
                    lockbox=search_config.lockbox,
                    best_params=lockbox_params,
                    strategy=candidate.strategy,
                    backtest_runner=runner,
                )
                lockbox_evaluated = True
                checkpoint["lockbox_consumed"] = True
                checkpoint["lockbox_metrics"] = lockbox_metrics
                record_lockbox_consumption(
                    manifest_hash=manifest.manifest_hash,
                    champion_hash=champion_hash,
                    lockbox_metrics=lockbox_metrics or {},
                    verdict="pending",
                    candidate_id=frozen_id,
                )
                write_alpha_research_artifact(
                    job_id,
                    "lockbox_metrics",
                    {"metrics": lockbox_metrics},
                )
            _set_stage("lockbox", "completed")
            _save_checkpoint(job_id, checkpoint)

        # --- final acceptance verdict ---
        _set_stage("acceptance", "running")
        final_acceptance = evaluate_research_acceptance_from_evidence(
            acceptance_id=checkpoint.get("acceptance_ids", {}).get(frozen_id, job_id),
            candidate_id=frozen_id,
            config=acceptance_config,
            seeds=frozen_acceptance.seeds,
            plateau=frozen_acceptance.plateau,
            dsr_value=frozen_acceptance.dsr_value,
            effective_attempt_count=frozen_acceptance.effective_attempt_count,
            lockbox_metrics=lockbox_metrics,
            lockbox_evaluated=lockbox_evaluated,
            lockbox_blocked=lockbox_blocked,
            champion_hash=champion_hash,
            manifest_hash=manifest.manifest_hash,
            tail=frozen_acceptance.tail_diagnostics,
        )
        _set_stage("acceptance", "completed")

        provenance = _build_provenance(
            request=request,
            profile_version=profile.version,
            manifest=manifest,
            seeds=seeds,
        )
        provenance["frozen_champion_id"] = frozen_id
        provenance["champion_hash"] = champion_hash
        provenance["finished_at"] = _now_iso()

        frozen_spec = next(spec for spec in checkpoint["candidate_specs"] if spec["candidate_id"] == frozen_id)
        frozen_genome = frozen_spec["fixed_params"].get("genome")
        champion_payload = {
            "candidate_id": frozen_id,
            "champion_seed": champion_seed.seed if champion_seed else None,
            "best_params": champion_params,
            "genome": frozen_genome,
            "hypothesis": extract_hypothesis_metadata(frozen_genome),
        }

        result = AlphaResearchResult(
            verdict=final_acceptance.verdict,
            profile_id=request.profile_id,
            provenance=provenance,
            split_manifest=checkpoint["manifest"],
            feature_evidence_summary=checkpoint.get("feature_evidence_summary", []),
            hypothesis_manifest=checkpoint.get("hypothesis_manifest", []),
            champion=champion_payload,
            acceptance=final_acceptance.to_dict(),
            stages=[AlphaResearchStageStatus.model_validate(stage) for stage in stages],
            inconclusive_reasons=[],
            coverage=checkpoint.get("coverage", {}),
        )
        result_payload = result.model_dump(mode="json")
        write_alpha_research_result(job_id, result_payload)
        checkpoint["final_verdict"] = final_acceptance.verdict
        _set_stage("complete", "completed")
        _save_checkpoint(job_id, checkpoint)
        try:
            with session_scope() as session:
                record_job_terminal(
                    session,
                    kind="alpha_research",
                    job_id=job_id,
                    raw_status="completed",
                )
        except Exception as term_exc:  # noqa: BLE001
            logger.warning("Failed to record terminal event for alpha research job %s: %s", job_id, term_exc)
        _persist_progress(
            job_id,
            {
                **_base_payload(
                    job_id,
                    status="completed",
                    progress=1.0,
                    stages=stages,
                    detail=final_acceptance.verdict,
                ),
                "result": result_payload,
                "checkpoint": checkpoint,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Alpha-research job %s failed", job_id)
        checkpoint["last_error"] = str(exc)
        _save_checkpoint(job_id, checkpoint)
        status: JobStatus = "cancelled" if is_cancelled(job_id) else "failed"
        try:
            with session_scope() as session:
                record_job_terminal(
                    session,
                    kind="alpha_research",
                    job_id=job_id,
                    raw_status=status,
                    error=str(exc),
                )
        except Exception as term_exc:  # noqa: BLE001
            logger.warning("Failed to record terminal failure for alpha research job %s: %s", job_id, term_exc)
        _persist_progress(
            job_id,
            {
                **_base_payload(
                    job_id,
                    status=status,
                    progress=1.0,
                    stages=stages,
                ),
                "error": str(exc),
                "checkpoint": checkpoint,
            },
        )


def _result_from_dict(payload: dict[str, Any]) -> ResearchAcceptanceResult:
    from q_backend.optimization.research_acceptance import (
        AcceptanceCriterion,
        PlateauResult,
        SeedRunRecord,
        TailDiagnostics,
    )

    criteria = [
        AcceptanceCriterion(
            name=row["name"],
            status=row["status"],
            observed=row["observed"],
            threshold=row["threshold"],
            reason=row["reason"],
        )
        for row in payload.get("criteria", [])
    ]
    seeds = [
        SeedRunRecord(
            seed=int(row["seed"]),
            status=row["status"],
            study_config=row.get("study_config"),
            best_params=row.get("best_params"),
            oos_metrics=row.get("oos_metrics"),
            window_returns=list(row.get("window_returns", [])),
            window_count=int(row.get("window_count", 0)),
            completed_windows=int(row.get("completed_windows", 0)),
            objective_value=row.get("objective_value"),
            failure_reason=row.get("failure_reason"),
        )
        for row in payload.get("seeds", [])
    ]
    plateau_raw = payload.get("plateau")
    plateau = None
    if plateau_raw:
        plateau = PlateauResult(
            neighbors_evaluated=int(plateau_raw["neighbors_evaluated"]),
            profitable_fraction=plateau_raw.get("profitable_fraction"),
            score_retention=plateau_raw.get("score_retention"),
            champion_objective=plateau_raw.get("champion_objective"),
            neighbors=[],
        )
    tail_raw = payload.get("tail_diagnostics") or {}
    tail = TailDiagnostics(
        worst_window_return=tail_raw.get("worst_window_return"),
        p25_window_return=tail_raw.get("p25_window_return"),
        max_losing_window_streak=tail_raw.get("max_losing_window_streak"),
        trade_count_concentration=tail_raw.get("trade_count_concentration"),
        regime_contribution=tail_raw.get("regime_contribution"),
    )
    return ResearchAcceptanceResult(
        acceptance_id=str(payload["acceptance_id"]),
        candidate_id=str(payload["candidate_id"]),
        verdict=payload["verdict"],
        criteria=criteria,
        champion_seed=payload.get("champion_seed"),
        champion_hash=payload.get("champion_hash"),
        seeds=seeds,
        plateau=plateau,
        tail_diagnostics=tail,
        dsr_value=payload.get("dsr_value"),
        effective_attempt_count=int(payload.get("effective_attempt_count", 1)),
        lockbox_metrics=payload.get("lockbox_metrics"),
        manifest_hash=payload.get("manifest_hash"),
    )


def get_alpha_research_status_payload(job_id: str) -> Optional[dict[str, Any]]:
    try:
        payload = get_job_progress(get_redis(), job_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.debug("Redis progress unavailable for alpha-research job %s", job_id)
        return None
    if payload is None:
        return None
    if payload.get("status") == "completed" and payload.get("result") is None:
        try:
            report = read_alpha_research_result(job_id)
        except FileNotFoundError:
            report = None
        if report is not None:
            payload = {**payload, "result": report}
    return payload


def reconcile_orphaned_runs() -> int:
    """Resume safe pre-lock-box jobs; fail jobs that already consumed the holdout."""
    try:
        client = get_redis()
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.warning("Failed to reconcile orphaned alpha-research jobs: %s", exc)
        return 0

    resumed = 0
    failed = 0
    try:
        for key in client.scan_iter(f"{PROGRESS_NAMESPACE}:progress:*"):
            raw = client.get(key)
            if raw is None:
                continue
            payload = json.loads(raw)
            if payload.get("status") not in ("queued", "running"):
                continue
            job_id = payload.get("job_id") or key.rsplit(":", 1)[-1]
            checkpoint: dict[str, Any] = {}
            try:
                checkpoint = read_alpha_research_checkpoint(job_id)
            except FileNotFoundError:
                checkpoint = {}

            if checkpoint.get("lockbox_consumed"):
                payload["status"] = "failed"
                payload["error"] = "Cancelled after backend restart; lock-box was already consumed."
                client.set(key, json.dumps(payload), ex=DEFAULT_PROGRESS_TTL_SECONDS)
                failed += 1
                continue

            request_json = json.dumps(checkpoint.get("request", {}))
            if not checkpoint.get("request"):
                payload["status"] = "failed"
                payload["error"] = "Cancelled after backend restart (job was orphaned)."
                client.set(key, json.dumps(payload), ex=DEFAULT_PROGRESS_TTL_SECONDS)
                failed += 1
                continue

            from q_backend.tasks import actors

            actors.run_alpha_research.send(job_id, request_json, True)
            resumed += 1
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.warning("Failed to reconcile orphaned alpha-research jobs: %s", exc)
        return resumed + failed

    if resumed or failed:
        logger.info(
            "Reconciled alpha-research jobs on startup: %d resumed, %d failed.",
            resumed,
            failed,
        )
    return resumed + failed
