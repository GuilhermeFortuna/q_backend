"""Job manager for Discovery A/B experiments (WO154)."""

from __future__ import annotations

import json
import logging
import statistics
import time
import uuid
from typing import Any, Literal, Optional

from scipy import stats

from q_backend.api import strategy_search_jobs
from q_backend.api.schemas.experiments import (
    DEFAULT_MINIMUM_COMPLETE_PAIRS,
    DiscoveryAbArmSummary,
    DiscoveryAbPairedDelta,
    DiscoveryAbRequest,
    DiscoveryAbResult,
)
from q_backend.optimization.models import ObjectiveMode
from q_backend.optimization.objectives import resolve_objective
from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.storage.lake.artifacts import (
    read_discovery_ab_report,
    write_discovery_ab_report,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import (
    DEFAULT_PROGRESS_TTL_SECONDS,
    get_job_progress,
    set_job_progress,
)

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "discovery_ab"

JobStatus = Literal["queued", "running", "completed", "failed"]
_VERDICT = Literal["helps", "no_effect", "hurts", "inconclusive"]
_FINISHED_CHILD_STATUSES = frozenset({"completed", "failed", "cancelled"})
_ALPHA = 0.05
_CHILD_POLL_INTERVAL_SEC = 2.0
_CHILD_POLL_TIMEOUT_SEC = 6 * 60 * 60


def _persist_progress(job_id: str, payload: dict[str, Any]) -> None:
    try:
        set_job_progress(
            get_redis(),
            job_id,
            payload,
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:
        logger.debug("Redis progress unavailable for discovery A/B job %s", job_id)


def _base_payload(
    job_id: str,
    *,
    status: JobStatus,
    progress: float = 0.0,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "detail": detail,
        "result": None,
        "error": None,
    }


def _arm_config(
    base: StrategySearchConfig,
    *,
    seed: int,
    latents_enabled: bool,
) -> StrategySearchConfig:
    genetic = base.genetic
    if genetic is None:
        raise ValueError("Discovery A/B requires genetic search config")
    return base.model_copy(
        update={
            "genetic": genetic.model_copy(update={"init_seed": seed}),
            "latents_enabled": latents_enabled,
        }
    )


def _best_objective_from_child_run(
    run_id: str,
) -> tuple[float | None, str, str]:
    """Return (objective, metric_name, note) for a completed child discovery run."""
    results = strategy_search_jobs.results_payload_from_db(run_id)
    if results is None:
        return None, "", f"run {run_id}: results unavailable"
    status = results.get("status")
    if status != "completed":
        return None, "", f"run {run_id}: ended with status {status}"

    summary = results.get("summary") or {}
    search_config = StrategySearchConfig.model_validate(results.get("search_config") or {})
    objective_mode = ObjectiveMode(results["objective_mode"])

    lockbox_metrics = summary.get("lockbox_metrics")
    if search_config.lockbox.enabled and lockbox_metrics:
        return (
            float(resolve_objective(lockbox_metrics, objective_mode)),
            "lockbox_objective",
            "",
        )

    best = results.get("best")
    if best is None:
        return None, "", f"run {run_id}: no best candidate"
    oos_metrics = best.get("oos_metrics")
    if oos_metrics:
        return (
            float(resolve_objective(oos_metrics, objective_mode)),
            "oos_objective",
            "",
        )
    objective_value = best.get("objective_value")
    if objective_value is not None:
        return float(objective_value), "oos_objective", ""
    return None, "", f"run {run_id}: best candidate missing objective metrics"


def _best_genome_has_latent(run_id: str) -> bool:
    results = strategy_search_jobs.results_payload_from_db(run_id)
    if results is None:
        return False
    best = results.get("best") or {}
    genome = best.get("genome") or {}
    nodes = genome.get("nodes") or []
    return any(node.get("kind") == "ind.latent" for node in nodes)


def _wait_for_child_run(run_id: str, *, deadline: float) -> str:
    while time.monotonic() < deadline:
        status = strategy_search_jobs.get_persisted_run_status(run_id)
        if status in _FINISHED_CHILD_STATUSES:
            return status or "failed"
        time.sleep(_CHILD_POLL_INTERVAL_SEC)
    raise TimeoutError(f"Child discovery run {run_id} timed out")


def _compute_verdict(
    control_values: list[float],
    treatment_values: list[float],
) -> tuple[_VERDICT, list[float], float, float, float]:
    deltas = [treatment - control for control, treatment in zip(control_values, treatment_values)]
    mean_delta = statistics.mean(deltas)

    if len(deltas) < 2:
        _, p_value = stats.ttest_rel(treatment_values, control_values)
        if p_value != p_value:  # NaN guard
            p_value = 1.0
    else:
        _, p_value = stats.ttest_rel(treatment_values, control_values)
        if p_value != p_value:  # NaN guard
            p_value = 1.0

    if len(deltas) > 1:
        stdev = statistics.stdev(deltas)
        cohens_d = mean_delta / stdev if stdev > 0 else 0.0
    else:
        cohens_d = 0.0

    if mean_delta > 0 and p_value < _ALPHA:
        verdict: _VERDICT = "helps"
    elif mean_delta < 0 and p_value < _ALPHA:
        verdict = "hurts"
    else:
        verdict = "no_effect"
    return verdict, deltas, mean_delta, cohens_d, float(p_value)


def _build_result(
    *,
    child_runs: list[dict[str, Any]],
    notes: list[str],
    requested_seeds: int,
    minimum_complete_pairs: int = DEFAULT_MINIMUM_COMPLETE_PAIRS,
) -> DiscoveryAbResult:
    control_by_seed: dict[int, float] = {}
    treatment_by_seed: dict[int, float] = {}
    metric_name = ""
    dropped_pair_reasons: list[str] = []

    for child in child_runs:
        seed = child["seed"]
        arm = child["arm"]
        if child.get("terminal_status") != "completed":
            reason = (
                f"seed {seed} {arm}: child run {child['run_id']} "
                f"ended {child.get('terminal_status')}"
            )
            notes.append(reason)
            dropped_pair_reasons.append(reason)
            continue
        value, metric, note = _best_objective_from_child_run(child["run_id"])
        if note:
            reason = f"seed {seed} {arm}: {note}"
            notes.append(reason)
            dropped_pair_reasons.append(reason)
            continue
        if not metric_name:
            metric_name = metric
        if arm == "control":
            control_by_seed[seed] = value  # type: ignore[assignment]
        else:
            treatment_by_seed[seed] = value  # type: ignore[assignment]

    surviving_seeds = sorted(set(control_by_seed) & set(treatment_by_seed))
    complete_pairs = len(surviving_seeds)
    control_values = [control_by_seed[seed] for seed in surviving_seeds]
    treatment_values = [treatment_by_seed[seed] for seed in surviving_seeds]

    for seed in sorted(set(control_by_seed) ^ set(treatment_by_seed)):
        missing_arm = "treatment" if seed in control_by_seed else "control"
        reason = f"seed {seed}: missing paired {missing_arm} objective"
        dropped_pair_reasons.append(reason)

    if not metric_name:
        metric_name = "oos_objective"

    if complete_pairs < minimum_complete_pairs:
        return DiscoveryAbResult(
            verdict="inconclusive",
            n_seeds=complete_pairs,
            requested_seeds=requested_seeds,
            complete_pairs=complete_pairs,
            minimum_complete_pairs=minimum_complete_pairs,
            dropped_pair_reasons=dropped_pair_reasons,
            metric=metric_name,  # type: ignore[arg-type]
            control=DiscoveryAbArmSummary(values=control_values, mean=None),
            treatment=DiscoveryAbArmSummary(values=treatment_values, mean=None),
            paired_delta=DiscoveryAbPairedDelta(
                values=[],
                mean=None,
                cohens_d=None,
                p_value=None,
            ),
            child_runs=child_runs,
        )

    verdict, deltas, mean_delta, cohens_d, p_value = _compute_verdict(
        control_values,
        treatment_values,
    )

    return DiscoveryAbResult(
        verdict=verdict,
        n_seeds=complete_pairs,
        requested_seeds=requested_seeds,
        complete_pairs=complete_pairs,
        minimum_complete_pairs=minimum_complete_pairs,
        dropped_pair_reasons=dropped_pair_reasons,
        metric=metric_name,  # type: ignore[arg-type]
        control={
            "values": control_values,
            "mean": statistics.mean(control_values),
        },
        treatment={
            "values": treatment_values,
            "mean": statistics.mean(treatment_values),
        },
        paired_delta={
            "values": deltas,
            "mean": mean_delta,
            "cohens_d": cohens_d,
            "p_value": p_value,
        },
        child_runs=child_runs,
    )


def start_discovery_ab_job(*, request: DiscoveryAbRequest) -> dict[str, str]:
    """Validate, persist the job record, and enqueue the orchestration actor."""
    job_id = str(uuid.uuid4())
    _persist_progress(
        job_id,
        _base_payload(job_id, status="queued", progress=0.0, detail="queued"),
    )

    from q_backend.tasks import actors

    actors.run_discovery_ab.send(job_id, request.model_dump_json())
    return {"job_id": job_id, "status": "queued"}


def run_discovery_ab_job(job_id: str, request_json: str) -> None:
    """Orchestrate paired control/treatment discovery runs per seed."""
    request = DiscoveryAbRequest.model_validate_json(request_json)
    base_config = request.config
    child_runs: list[dict[str, Any]] = []
    notes: list[str] = []

    try:
        _persist_progress(
            job_id,
            _base_payload(job_id, status="running", progress=0.0, detail="submitting"),
        )

        for seed in request.seeds:
            for arm, latents_enabled in (("control", False), ("treatment", True)):
                child_config = _arm_config(
                    base_config,
                    seed=seed,
                    latents_enabled=latents_enabled,
                )
                child_job = strategy_search_jobs.start_job(child_config)
                child_runs.append(
                    {
                        "seed": seed,
                        "arm": arm,
                        "run_id": child_job.run_id,
                        "latents_enabled": latents_enabled,
                    }
                )

        total_children = len(child_runs)
        deadline = time.monotonic() + _CHILD_POLL_TIMEOUT_SEC
        completed_children = 0
        for child in child_runs:
            child["terminal_status"] = _wait_for_child_run(
                child["run_id"],
                deadline=deadline,
            )
            completed_children += 1
            detail = "; ".join(notes) if notes else "polling child runs"
            _persist_progress(
                job_id,
                {
                    **_base_payload(
                        job_id,
                        status="running",
                        progress=completed_children / total_children,
                        detail=detail,
                    ),
                },
            )

        result = _build_result(
            child_runs=child_runs,
            notes=notes,
            requested_seeds=len(request.seeds),
            minimum_complete_pairs=request.minimum_complete_pairs,
        )
        detail = "; ".join(notes) if notes else None
        result_payload = result.model_dump(mode="json")
        write_discovery_ab_report(job_id, result_payload)
        _persist_progress(
            job_id,
            {
                **_base_payload(
                    job_id,
                    status="completed",
                    progress=1.0,
                    detail=detail,
                ),
                "result": result_payload,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure to the client
        logger.exception("Discovery A/B job %s failed", job_id)
        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status="failed", progress=1.0),
                "error": str(exc),
                "detail": "; ".join(notes) if notes else None,
            },
        )


def get_discovery_ab_status_payload(job_id: str) -> Optional[dict[str, Any]]:
    try:
        payload = get_job_progress(get_redis(), job_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug("Redis progress unavailable for discovery A/B job %s", job_id)
        return None
    if payload is None:
        return None
    if payload.get("status") == "completed" and payload.get("result") is None:
        try:
            report = read_discovery_ab_report(job_id)
        except FileNotFoundError:
            report = None
        if report is not None:
            payload = {**payload, "result": report}
    return payload


def reconcile_orphaned_runs() -> int:
    """Mark queued/running discovery A/B jobs failed after a process restart."""
    try:
        client = get_redis()
    except Exception as exc:
        logger.warning("Failed to reconcile orphaned discovery A/B jobs: %s", exc)
        return 0

    count = 0
    try:
        for key in client.scan_iter(f"{PROGRESS_NAMESPACE}:progress:*"):
            raw = client.get(key)
            if raw is None:
                continue
            payload = json.loads(raw)
            if payload.get("status") not in ("queued", "running"):
                continue
            payload["status"] = "failed"
            payload["error"] = "Cancelled after backend restart (job was orphaned)."
            client.set(key, json.dumps(payload), ex=DEFAULT_PROGRESS_TTL_SECONDS)
            count += 1
    except Exception as exc:
        logger.warning("Failed to reconcile orphaned discovery A/B jobs: %s", exc)
        return count

    if count:
        logger.info("Reconciled %d orphaned discovery A/B job(s) on startup.", count)
    return count
