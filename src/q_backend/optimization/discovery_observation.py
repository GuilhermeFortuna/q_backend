"""Observational Discovery state, separate from scheduling and ranking.

Each candidate/window has an identity. Atomic monotonic merges make repeated or
out-of-order notifications safe without shared last-worker-wins counters.
"""

import json
import logging
import math
import uuid
from functools import wraps
from typing import Any

import redis
from sqlalchemy.exc import SQLAlchemyError

from q_backend.storage.redis.client import get_redis
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import StrategySearchRun
from q_backend.storage.db.discovery_trials import save_candidate, save_trial
from q_backend.streaming.jobs import publish_job_progress

logger = logging.getLogger(__name__)
TTL = 86400


def _best_effort(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (redis.RedisError, SQLAlchemyError, OSError, ValueError):
            logger.warning("Discovery observation %s unavailable", function.__name__, exc_info=True)
            return None

    return wrapper


def _key(run_id: str, kind: str) -> str:
    return f"strategy_search:observation:{run_id}:{kind}"


def _merge(client: redis.Redis, key: str, field: str, payload: dict[str, Any]) -> None:
    while True:
        with client.pipeline() as pipe:
            try:
                pipe.watch(key)
                previous = json.loads(pipe.hget(key, field) or "{}")
                if previous.get("phase") == "done" or previous.get("state") not in (None, "running"):
                    return
                merged = {**previous, **payload}
                merged["trials"] = max(previous.get("trials", 0), payload.get("trials", 0))
                if previous.get("phase") == "testing" and payload.get("phase") == "optimizing":
                    merged["phase"] = "testing"
                pipe.multi()
                pipe.hset(key, field, json.dumps(merged))
                pipe.expire(key, TTL)
                pipe.execute()
                return
            except redis.WatchError:
                continue


@_best_effort
def initialize(run_id: str, total_candidates: int, windows_per_candidate: int, trial_budget: int) -> None:
    client = get_redis()
    client.set(
        _key(run_id, "config"),
        json.dumps(
            dict(
                total_candidates=total_candidates,
                windows_per_candidate=windows_per_candidate,
                trial_budget=trial_budget,
            )
        ),
        ex=TTL,
    )
    set_stage(run_id, "preparing")


@_best_effort
def set_stage(run_id: str, stage: str) -> None:
    client = get_redis()
    client.set(_key(run_id, "stage"), stage, ex=TTL)
    _notify(run_id, client)


@_best_effort
def observe_candidate(
    run_id: str,
    candidate_id: str,
    *,
    strategy: str,
    total_windows: int,
    state: str = "running",
    generation: int | None = None,
    client: redis.Redis | None = None,
) -> None:
    client = client or get_redis()
    _merge(
        client,
        _key(run_id, "candidates"),
        candidate_id,
        dict(
            candidate_id=candidate_id,
            strategy=strategy,
            total_windows=total_windows,
            state=state,
            generation=generation,
        ),
    )
    _notify(run_id, client)


@_best_effort
def observe_window(
    run_id: str, candidate_id: str, window_index: int, *, phase: str, trials: int = 0, client: redis.Redis | None = None
) -> None:
    client = client or get_redis()
    _merge(
        client,
        _key(run_id, "windows"),
        f"{candidate_id}:{window_index}",
        dict(candidate_id=candidate_id, window_index=window_index, phase=phase, trials=trials),
    )
    _notify(run_id, client)


def activity_snapshot(
    run_id: str,
    *,
    total_candidates: int,
    windows_per_candidate: int,
    trial_budget: int,
    client: redis.Redis | None = None,
) -> dict[str, Any]:
    client = client or get_redis()
    candidates = [json.loads(v) for v in client.hvals(_key(run_id, "candidates"))]
    windows = [json.loads(v) for v in client.hvals(_key(run_id, "windows"))]
    budget = max(trial_budget, 1) + 1
    finished = sum(w["phase"] == "done" for w in windows)
    settled = {c["candidate_id"] for c in candidates if c["state"] != "running"}
    units = len(settled) * windows_per_candidate * budget
    units += sum(
        budget if w["phase"] == "done" else min(w["trials"], trial_budget)
        for w in windows
        if w["candidate_id"] not in settled
    )
    total_windows = total_candidates * windows_per_candidate
    active = []
    for candidate in candidates:
        if candidate["state"] != "running":
            continue
        own = sorted(
            (w for w in windows if w["candidate_id"] == candidate["candidate_id"]), key=lambda w: w["window_index"]
        )
        active.append(
            {
                **candidate,
                "finished_windows": sum(w["phase"] == "done" for w in own),
                "window_index": own[-1]["window_index"] if own else None,
                "phase": own[-1]["phase"] if own else "preparing",
                "trials": own[-1]["trials"] if own else 0,
                "trial_budget": trial_budget,
            }
        )
    stage = client.get(_key(run_id, "stage")) or "evaluation"
    if isinstance(stage, bytes):
        stage = stage.decode()
    return dict(
        progress=min(1.0, units / (total_windows * budget)) if total_windows else 0.0,
        completed_candidates=len(settled),
        finished_windows=finished,
        planned_windows=total_windows,
        settled_trials=sum(w["trials"] for w in windows),
        active_candidates=active,
        stage=stage,
    )


def _notify(run_id: str, client: redis.Redis) -> None:
    config = client.get(_key(run_id, "config"))
    if config:
        # The REST snapshot computes the aggregate once per UI refresh, rather
        # than scanning all candidates/windows for every high-rate trial event.
        progress = float(client.get(_key(run_id, "progress")) or 0)
        publish_job_progress(
            "strategy_search",
            run_id,
            {"status": "running", "progress": progress, "message": "Discovery results updated"},
            client=client,
        )


def snapshot(run_id: str) -> dict[str, Any]:
    client = get_redis()
    config = client.get(_key(run_id, "config"))
    if not config:
        return {}
    result = activity_snapshot(run_id, client=client, **json.loads(config))
    client.set(_key(run_id, "progress"), result["progress"], ex=TTL)
    return result


def record_trial(
    run_id: str, candidate_id: str, window_index: int, trial: Any, *, generation: int | None = None
) -> None:
    """Commit a terminal trial before notifying subscribers; degrade observably."""
    try:
        state = {"COMPLETE": "completed", "PRUNED": "pruned", "FAIL": "error"}.get(trial.state.name, "error")
        if trial.user_attrs.get("status") in ("error", "pruned"):
            state = trial.user_attrs["status"]
        objective = trial.value
        if objective is not None and not math.isfinite(objective):
            objective = None
        payload = dict(
            candidate_id=candidate_id,
            generation=generation if generation is not None else -1,
            window_index=window_index,
            trial_number=trial.number,
            state=state,
            objective_value=objective,
            parameters=trial.params,
            metrics=trial.user_attrs.get("metrics", {}),
            started_at=trial.datetime_start,
            finished_at=trial.datetime_complete,
            error=trial.user_attrs.get("error"),
        )
        with session_scope() as session:
            if session.get(StrategySearchRun, uuid.UUID(run_id)) is not None:
                save_trial(session, uuid.UUID(run_id), payload)
    except (redis.RedisError, SQLAlchemyError, OSError, ValueError):
        logger.warning(
            "Could not persist discovery trial for %s/%s/%s", run_id, candidate_id, trial.number, exc_info=True
        )
    try:
        key = _key(run_id, f"trial_ids:{candidate_id}:{window_index}")
        client = get_redis()
        client.sadd(key, trial.number)
        client.expire(key, TTL)
        observe_window(run_id, candidate_id, window_index, phase="optimizing", trials=client.scard(key))
    except (redis.RedisError, OSError, ValueError):
        logger.warning("Could not publish discovery trial activity for %s", run_id, exc_info=True)


@_best_effort
def remember_result(run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
    _remember_durable_result(run_id, payload, metadata)
    client = get_redis()
    key = _key(run_id, "results")
    client.hset(key, payload["candidate_id"], json.dumps({"result": payload, "metadata": metadata or {}}))
    client.expire(key, TTL)


@_best_effort
def _remember_durable_result(run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None) -> None:
    with session_scope() as session:
        identifier = uuid.UUID(run_id)
        if session.get(StrategySearchRun, identifier) is not None:
            save_candidate(session, run_id=identifier, **{**payload, **(metadata or {})})


def partial_results(run_id: str) -> list[dict[str, Any]]:
    return [json.loads(v) for v in get_redis().hvals(_key(run_id, "results"))]


@_best_effort
def clear(run_id: str) -> None:
    client = get_redis()
    keys = list(client.scan_iter(match=f"strategy_search:observation:{run_id}:*"))
    for index in range(0, len(keys), 500):
        client.delete(*keys[index : index + 500])
