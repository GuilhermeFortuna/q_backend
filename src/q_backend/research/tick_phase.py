"""Tick-phase exit decisions for a forming candle, in-process or across worker processes."""

from __future__ import annotations

import multiprocessing
import os
import pickle
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Literal, Protocol

import numpy as np
import pandas as pd

from q_backend.research.hooks import HookCall, invoke_hook
from q_backend.research.positions import ResearchPosition
from q_backend.research.strategy import ResearchStrategy

_FORMING_COLUMNS = ("open", "high", "low", "close", "tick_volume")
# Decisions each worker takes per wave: large enough to amortise the round trip, small
# enough that little work is discarded once an earlier tick has exited.
_DECISIONS_PER_WORKER = 16


# Memory budgeted per worker when sizing "auto": each one imports the research stack and the
# strategy's module, then holds a copy of the history.
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


class TickDecisions(Protocol):
    """Finds the first tick of a forming candle at which ``exit_strategy`` closes."""

    wave_size: int

    def first_exit(
        self,
        position: int,
        ticks: np.ndarray,
        candles: np.ndarray,
        positions: tuple[ResearchPosition, ...],
    ) -> int | None: ...

    def close(self) -> None: ...


class TickEvaluator:
    """Runs ``exit_strategy(phase="tick")`` on the history ending in a forming candle.

    Each decision recomputes the strategy's indicators over the raw history with the candle
    observed so far as the last row, so a tick decision sees nothing after its own price.
    """

    wave_size = 1

    def __init__(self, strategy: ResearchStrategy, raw_history: pd.DataFrame, call: HookCall) -> None:
        self._strategy = strategy
        self._raw = raw_history
        self._call = call
        self._template: tuple[int, pd.DataFrame, list[int]] | None = None

    def first_exit(
        self,
        position: int,
        ticks: np.ndarray,
        candles: np.ndarray,
        positions: tuple[ResearchPosition, ...],
    ) -> int | None:
        """Index into ``ticks`` of the first exit, evaluating in order and stopping there.

        ``candles`` holds one ``(open, high, low, close)`` row per entry of ``ticks``; the
        tick ordinal plus one is the forming candle's ``tick_volume``.
        """
        for i, tick in enumerate(ticks.tolist()):
            frame = self._frame(position, candles[i], tick + 1)
            decision = invoke_hook(
                self._strategy, self._call, "exit_strategy", frame, positions, phase="tick", detail=f" tick {tick}"
            )
            if decision is not None:
                return i
        return None

    def close(self) -> None:
        return None

    def _frame(self, position: int, candle: np.ndarray, count: int) -> pd.DataFrame:
        template, columns = self._forming_template(position)
        frame = template.copy()
        values = [*candle.tolist(), float(count)]
        frame.iloc[-1, columns] = values[: len(columns)]
        # DataFrame.copy shares index storage; the hook must own what it is given.
        frame.index = frame.index.copy(deep=True)
        name = type(self._strategy).__name__
        try:
            augmented = self._strategy.compute_indicators(frame)
        except Exception as exc:
            raise RuntimeError(f"Error in {name}.compute_indicators during tick replay: {exc}") from exc
        if not isinstance(augmented, pd.DataFrame) or not augmented.index.equals(frame.index):
            raise ValueError(f"{name}.compute_indicators modified row count or index during tick replay")
        augmented.attrs = {}
        return augmented

    def _forming_template(self, position: int) -> tuple[pd.DataFrame, list[int]]:
        """History before ``position`` plus one unset row, built once per candle."""
        if self._template is None or self._template[0] != position:
            raw = self._raw
            row = pd.DataFrame(np.nan, index=raw.index[[position]], columns=raw.columns, dtype="float64")
            template = pd.concat([raw.iloc[:position], row])
            template.attrs = {}
            columns = [template.columns.get_loc(name) for name in _FORMING_COLUMNS if name in template.columns]
            self._template = (position, template, columns)
        return self._template[1], self._template[2]

    def __getstate__(self) -> dict[str, object]:
        return {"_strategy": self._strategy, "_raw": self._raw, "_call": self._call, "_template": None}


_worker_evaluator: TickEvaluator | None = None


def _start_worker(payload: bytes) -> None:
    global _worker_evaluator
    _worker_evaluator = pickle.loads(payload)


def _worker_first_exit(
    position: int,
    ticks: np.ndarray,
    candles: np.ndarray,
    positions: tuple[ResearchPosition, ...],
) -> int | None:
    if _worker_evaluator is None:
        raise RuntimeError("Tick worker was not initialised")
    return _worker_evaluator.first_exit(position, ticks, candles, positions)


class TickWorkerPool:
    """Spreads a wave of tick decisions over worker processes; the earliest exit wins.

    Hooks run in other processes, so the strategy must be picklable and its hooks must not
    rely on side effects.
    """

    def __init__(self, evaluator: TickEvaluator, workers: int) -> None:
        name = type(evaluator._strategy).__name__
        try:
            self._payload = pickle.dumps(evaluator)
        except Exception as exc:
            raise ValueError(
                f"{name} cannot be sent to worker processes ({exc}); "
                "define it at module level without unpicklable state, or pass workers=1"
            ) from exc
        self._name = name
        self._module = type(evaluator._strategy).__module__
        self._workers = workers
        self.wave_size = workers * _DECISIONS_PER_WORKER
        self._executor: ProcessPoolExecutor | None = None

    def first_exit(
        self,
        position: int,
        ticks: np.ndarray,
        candles: np.ndarray,
        positions: tuple[ResearchPosition, ...],
    ) -> int | None:
        executor = self._ensure_executor()
        bounds = np.linspace(0, len(ticks), min(self._workers, len(ticks)) + 1, dtype=int)
        futures: list[tuple[int, Future[int | None]]] = []
        try:
            for start, stop in zip(bounds[:-1].tolist(), bounds[1:].tolist(), strict=True):
                future = executor.submit(
                    _worker_first_exit, position, ticks[start:stop], candles[start:stop], positions
                )
                futures.append((start, future))
            # In tick order: an exit, or an error, in an earlier batch hides later batches.
            for start, future in futures:
                hit = future.result()
                if hit is not None:
                    return start + hit
            return None
        except BrokenProcessPool as exc:
            raise RuntimeError(
                f"Tick worker processes for {self._name} could not start or stopped unexpectedly; "
                "the strategy must be importable from a module (or a script guarded by "
                '`if __name__ == "__main__":`). Pass workers=1 to run in-process.'
            ) from exc
        finally:
            for _start, future in futures:
                future.cancel()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None

    def _ensure_executor(self) -> ProcessPoolExecutor:
        if self._executor is None:
            context = multiprocessing.get_context("forkserver")
            # Workers fork from a server that has already imported the research stack and the
            # strategy's module, so they share that memory instead of each loading a copy.
            context.set_forkserver_preload([__name__, self._module])
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=context,
                initializer=_start_worker,
                initargs=(self._payload,),
            )
        return self._executor
