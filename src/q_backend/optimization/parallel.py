import os


def resolve_worker_count(max_workers: int | None, units: int) -> int:
    """Resolve the number of worker processes for parallel optimization.

    ``None`` => auto (one worker per available CPU), capped by ``units`` since
    that is the unit of parallelism (trials, windows, etc.). Never returns
    less than 1.
    """
    configured = max_workers if max_workers is not None else (os.cpu_count() or 1)
    return max(1, min(configured, max(units, 1)))
