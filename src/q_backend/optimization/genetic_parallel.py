"""Process-parallel evaluation of one genetic generation (WO54)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import pandas as pd

from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.strategy_search import (
    CandidateResult,
    SearchCandidate,
    SearchProgress,
    StrategySearchConfig,
    evaluate_candidate,
)

_WORKER_OHLCV: pd.DataFrame | None = None


def eval_config_for_parallel_workers(
    config: StrategySearchConfig,
) -> StrategySearchConfig:
    """Force walk-forward window parallelism off inside candidate workers."""
    walkforward = config.walkforward.model_copy(update={"max_workers": 1})
    return config.model_copy(update={"walkforward": walkforward})


def _init_genetic_worker(df: pd.DataFrame) -> None:
    global _WORKER_OHLCV
    _WORKER_OHLCV = df


def _search_progress_to_dict(progress: SearchProgress) -> dict[str, Any]:
    return {
        "current_candidate": progress.current_candidate,
        "total_candidates": progress.total_candidates,
        "candidate_id": progress.candidate_id,
        "strategy": progress.strategy,
        "phase": progress.phase,
        "window_index": progress.window_index,
        "total_windows": progress.total_windows,
        "generation": progress.generation,
        "total_generations": progress.total_generations,
    }


def _search_progress_from_dict(data: dict[str, Any]) -> SearchProgress:
    return SearchProgress(
        current_candidate=data["current_candidate"],
        total_candidates=data["total_candidates"],
        candidate_id=data["candidate_id"],
        strategy=data["strategy"],
        phase=data["phase"],
        window_index=data["window_index"],
        total_windows=data["total_windows"],
        generation=data.get("generation"),
        total_generations=data.get("total_generations"),
    )


@dataclass(frozen=True)
class GeneticEvalTask:
    candidate_index: int
    candidate: SearchCandidate
    eval_config: dict[str, Any]
    generation: int
    total_generations: int
    total_candidates: int


@dataclass(frozen=True)
class GeneticEvalWorkerResult:
    candidate_index: int
    result: CandidateResult
    progress_events: list[dict[str, Any]]


def _evaluate_candidate_worker(task: GeneticEvalTask) -> GeneticEvalWorkerResult:
    if _WORKER_OHLCV is None:
        raise RuntimeError("Worker OHLCV frame was not initialized")

    config = StrategySearchConfig.model_validate(task.eval_config)
    runner = DefaultBacktestRunner.from_frame_sliced(_WORKER_OHLCV)
    progress_events: list[dict[str, Any]] = []

    def worker_progress(progress: SearchProgress) -> None:
        progress_events.append(
            _search_progress_to_dict(
                SearchProgress(
                    current_candidate=task.candidate_index + 1,
                    total_candidates=task.total_candidates,
                    candidate_id=progress.candidate_id,
                    strategy=progress.strategy,
                    phase=progress.phase,
                    window_index=progress.window_index,
                    total_windows=progress.total_windows,
                    generation=task.generation,
                    total_generations=task.total_generations,
                )
            )
        )

    result = evaluate_candidate(
        task.candidate,
        config,
        runner,
        ohlcv=_WORKER_OHLCV,
        progress_callback=worker_progress,
        should_stop=None,
    )
    return GeneticEvalWorkerResult(
        candidate_index=task.candidate_index,
        result=result,
        progress_events=progress_events,
    )


def evaluate_generation_parallel(
    candidates: list[SearchCandidate],
    eval_config: StrategySearchConfig,
    ohlcv: pd.DataFrame,
    *,
    max_workers: int | None,
    generation: int,
    total_generations: int,
    progress_callback: Callable[[SearchProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[CandidateResult]:
    """Evaluate one generation's candidates across worker processes."""
    if not candidates:
        return []

    worker_config = eval_config_for_parallel_workers(eval_config)
    eval_config_dict = worker_config.model_dump(mode="json")
    workers = resolve_worker_count(max_workers, len(candidates))
    tasks = [
        GeneticEvalTask(
            candidate_index=index,
            candidate=candidate,
            eval_config=eval_config_dict,
            generation=generation,
            total_generations=total_generations,
            total_candidates=len(candidates),
        )
        for index, candidate in enumerate(candidates)
    ]

    completed: dict[int, CandidateResult] = {}
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_genetic_worker,
        initargs=(ohlcv,),
    ) as pool:
        pending: list[tuple[GeneticEvalTask, Future[GeneticEvalWorkerResult]]] = []
        task_iter = iter(tasks)

        def submit_batch(batch_size: int) -> None:
            for _ in range(batch_size):
                try:
                    task = next(task_iter)
                except StopIteration:
                    return
                pending.append((task, pool.submit(_evaluate_candidate_worker, task)))

        submit_batch(workers)

        while pending:
            if should_stop is not None and should_stop():
                for _task, future in pending:
                    future.cancel()
                pending.clear()
                break

            task, future = pending.pop(0)
            worker_result = future.result()
            completed[worker_result.candidate_index] = worker_result.result

            if progress_callback is not None:
                for event in worker_result.progress_events:
                    progress_callback(_search_progress_from_dict(event))
                progress_callback(
                    SearchProgress(
                        current_candidate=task.candidate_index + 1,
                        total_candidates=len(candidates),
                        candidate_id=task.candidate.candidate_id,
                        strategy=task.candidate.strategy,
                        phase="done",
                        window_index=None,
                        total_windows=None,
                        generation=generation,
                        total_generations=total_generations,
                    )
                )

            submit_batch(1)

    return [completed[index] for index in range(len(candidates)) if index in completed]
