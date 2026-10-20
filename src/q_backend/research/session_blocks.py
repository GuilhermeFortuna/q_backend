"""Day-trade sessions run in worker processes, in contiguous blocks, and joined in session order.

A day-trade position closes within its session, and working orders lapse at the end of the day,
so the sessions of one run do not depend on each other. Each worker takes a contiguous block of
whole sessions, runs the candle kernel over it, and returns the raw kernel run. The parent joins
the runs in session order, so the result matches a sequential run.
"""

from __future__ import annotations

import multiprocessing
import pickle
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pandas as pd

from q_backend.backtesting.candle_kernel import ChunkRun, merge_chunk_runs
from q_backend.backtesting.engine import BacktestEngine
from q_backend.market_data.timezone import BRASILIA_TZ

# The engine and the prepared history of the run in this worker process.
_worker_state: tuple[BacktestEngine, pd.DataFrame] | None = None


def session_blocks(index: pd.DatetimeIndex, workers: int) -> list[tuple[int, int]] | None:
    """``[start, stop)`` bar ranges of up to ``workers`` blocks of whole sessions, in order.

    Returns None when there is one session or one worker, so there is nothing to split.
    """
    days = np.asarray(index.tz_convert(BRASILIA_TZ).date)
    starts = np.flatnonzero(np.concatenate(([True], days[1:] != days[:-1])))
    if len(starts) < 2 or workers < 2:
        return None
    stops = np.append(starts[1:], len(days))
    groups = np.array_split(np.arange(len(starts)), min(workers, len(starts)))
    return [(int(starts[group[0]]), int(stops[group[-1]])) for group in groups]


def run_session_blocks(
    engine: BacktestEngine,
    prepared: pd.DataFrame,
    blocks: list[tuple[int, int]],
    *,
    force_close_at_end: bool,
    workers: int,
    strategy_name: str,
    strategy_module: str,
    on_block_done: Callable[[int, pd.Timestamp], None],
) -> ChunkRun:
    """Run each block in a worker process and return the kernel run of the whole prepared history.

    ``on_block_done(bars, timestamp)`` is called as each block finishes, with the bars finished so
    far across all blocks. A block that fails raises the error it raised in its worker.
    """
    try:
        payload = pickle.dumps((engine, prepared))
    except Exception as exc:
        raise ValueError(
            f"{strategy_name} cannot be sent to worker processes ({exc}); "
            "define it at module level without unpicklable state, or pass workers=1"
        ) from exc
    context = multiprocessing.get_context("forkserver")
    # Workers fork from a server that has already imported the research stack and the strategy's
    # module, so they share that memory instead of each loading a copy.
    context.set_forkserver_preload([__name__, strategy_module])
    last = len(blocks) - 1
    runs: list[ChunkRun | None] = [None] * len(blocks)
    finished = 0
    try:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(blocks)),
            mp_context=context,
            initializer=_start_worker,
            initargs=(payload,),
        ) as pool:
            futures = {
                pool.submit(_run_block, start, stop, force_close_at_end and position == last): position
                for position, (start, stop) in enumerate(blocks)
            }
            for future in as_completed(futures):
                position = futures[future]
                runs[position] = future.result()
                start, stop = blocks[position]
                finished += stop - start
                on_block_done(finished, prepared.index[stop - 1])
    except BrokenProcessPool as exc:
        raise RuntimeError(
            f"Worker processes for {strategy_name} could not start or stopped unexpectedly; "
            "the strategy must be importable from a module (or a script guarded by "
            '`if __name__ == "__main__":`). Pass workers=1 to run in-process.'
        ) from exc
    return merge_chunk_runs([run for run in runs if run is not None], [start for start, _ in blocks])


def _start_worker(payload: bytes) -> None:
    global _worker_state
    _worker_state = pickle.loads(payload)


def _run_block(start: int, stop: int, force_close_at_end: bool) -> ChunkRun:
    if _worker_state is None:
        raise RuntimeError("Session worker was not initialised")
    engine, prepared = _worker_state
    block = prepared.iloc[start:stop]
    block.attrs = prepared.attrs
    return engine.run_prepared_block(block, force_close_at_end=force_close_at_end)
