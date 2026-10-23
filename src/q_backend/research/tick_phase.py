"""Worker-count resolution for research backtests."""

from __future__ import annotations

import os
from typing import Literal

# Memory budgeted per worker when sizing "auto": each one imports the research stack and the
# strategy's module, then holds a copy of the prepared history.
_AUTO_BYTES_PER_WORKER = 1 << 30


def _available_memory() -> int | None:
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def resolve_workers(workers: int | Literal["auto"]) -> int:
    """Validate ``workers`` and size ``"auto"`` from the usable CPUs and the free memory."""
    if workers == "auto":
        if hasattr(os, "sched_getaffinity"):
            cpus = len(os.sched_getaffinity(0))
        else:
            cpus = os.cpu_count() or 1
        memory = _available_memory()
        if memory is not None:
            cpus = min(cpus, memory // _AUTO_BYTES_PER_WORKER)
        return max(1, cpus)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError(f"workers must be a positive integer or 'auto', got {workers!r}")
    return workers
