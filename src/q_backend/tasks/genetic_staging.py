"""Redis-backed state for the generation-barrier genetic discovery flow.

Genetic search cannot fan every candidate out at once: each generation's population
is *bred* from the previous generation's fitness, so generation N+1 does not exist
until generation N has been evaluated and ranked. The flow therefore fans out one
generation's population, barriers on the shared fan-in counter, breeds the next
population in the finalizer, and dispatches again — for G generations.

That barrier needs three pieces of state to survive across Dramatiq messages, since
no per-run state lives in a single process anymore:

* **provider state** — the evolving RNG, champion, best-fitness, generation counter,
  and current population. Reloaded by the finalizer to breed the next generation.
* **generation results** — every generation's candidate results, accumulated for the
  final all-generations leaderboard and DB persistence.
* **candidate metadata** — per-candidate genome/node-count/complexity, accumulated
  for the final genome artifacts.

The within-generation barrier reuses the existing ``fanin``/``staging`` primitives
(one generation at a time, cleared between generations); only the cross-generation
state lives here.
"""

import json
from typing import Any, Optional

import redis

from q_backend.storage.redis.client import get_redis

_TTL_SECONDS = 86400


def _state_key(run_id: str) -> str:
    return f"job:genetic:state:{run_id}"


def _results_key(run_id: str) -> str:
    return f"job:genetic:results:{run_id}"


def _meta_key(run_id: str) -> str:
    return f"job:genetic:meta:{run_id}"


def _windows_key(run_id: str) -> str:
    return f"job:genetic:windows:{run_id}"


def bump_completed_windows(run_id: str, *, client: Optional[redis.Redis] = None) -> int:
    """Atomically count one more completed walk-forward window; return the new total.

    Drives a fine-grained progress bar: candidates run in parallel and each is a long
    walk-forward, so candidate-completion counting leaves the UI at 0% through the
    whole first wave. Counting windows lets the bar move continuously from the start.
    """
    client = client or get_redis()
    key = _windows_key(run_id)
    value = int(client.incr(key))
    client.expire(key, _TTL_SECONDS)
    return value


def get_completed_windows(run_id: str, *, client: Optional[redis.Redis] = None) -> int:
    client = client or get_redis()
    raw = client.get(_windows_key(run_id))
    return int(raw) if raw is not None else 0


def set_provider_state(
    run_id: str, state: dict[str, Any], *, client: Optional[redis.Redis] = None
) -> None:
    """Persist the evolving provider state (RNG, champion, population, counters)."""
    client = client or get_redis()
    client.set(_state_key(run_id), json.dumps(state), ex=_TTL_SECONDS)


def get_provider_state(
    run_id: str, *, client: Optional[redis.Redis] = None
) -> Optional[dict[str, Any]]:
    client = client or get_redis()
    raw = client.get(_state_key(run_id))
    return json.loads(raw) if raw is not None else None


def get_generation_genome(
    run_id: str, index: int, *, client: Optional[redis.Redis] = None
) -> dict[str, Any]:
    """Return the genome a candidate worker should evaluate, by population index.

    Sourced from the stashed provider state, whose ``population`` is always the
    generation currently being dispatched.
    """
    state = get_provider_state(run_id, client=client)
    if state is None:
        raise KeyError(f"no genetic provider state stashed for run {run_id}")
    return state["population"][index]


def append_generation_results(
    run_id: str,
    generation: int,
    results: list[dict[str, Any]],
    *,
    client: Optional[redis.Redis] = None,
) -> None:
    """Stash one generation's serialized candidate results, keyed by generation."""
    client = client or get_redis()
    key = _results_key(run_id)
    client.hset(key, str(generation), json.dumps(results))
    client.expire(key, _TTL_SECONDS)


def get_all_generation_results(
    run_id: str, *, client: Optional[redis.Redis] = None
) -> list[list[dict[str, Any]]]:
    """Return every generation's results, ordered by generation index."""
    client = client or get_redis()
    raw = client.hgetall(_results_key(run_id))
    return [json.loads(raw[key]) for key in sorted(raw, key=int)]


def set_candidate_meta(
    run_id: str,
    metas: dict[str, dict[str, Any]],
    *,
    client: Optional[redis.Redis] = None,
) -> None:
    """Merge per-candidate metadata (genome, node count, complexity) into the run."""
    if not metas:
        return
    client = client or get_redis()
    key = _meta_key(run_id)
    client.hset(key, mapping={cid: json.dumps(meta) for cid, meta in metas.items()})
    client.expire(key, _TTL_SECONDS)


def get_all_candidate_meta(
    run_id: str, *, client: Optional[redis.Redis] = None
) -> dict[str, dict[str, Any]]:
    client = client or get_redis()
    raw = client.hgetall(_meta_key(run_id))
    return {cid: json.loads(value) for cid, value in raw.items()}


def clear_genetic_keys(run_id: str, *, client: Optional[redis.Redis] = None) -> None:
    """Remove all cross-generation genetic state once the run is terminal."""
    client = client or get_redis()
    client.delete(
        _state_key(run_id),
        _results_key(run_id),
        _meta_key(run_id),
        _windows_key(run_id),
    )
