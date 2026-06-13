"""CPU-budget helpers for fanning work out across the worker pool."""

from q_backend.storage.settings import get_settings


def worker_budget() -> int:
    """Total worker processes available — the backend-wide CPU budget."""
    return max(1, get_settings().worker_processes)


def fan_out_count(units: int) -> int:
    """How many leaf messages to dispatch for a job with ``units`` of work.

    Capped at the worker budget so a single job never enqueues more parallel
    leaves than there are worker slots; the pool then shares those slots fairly
    with any other jobs already running.
    """
    return max(1, min(units, worker_budget()))
