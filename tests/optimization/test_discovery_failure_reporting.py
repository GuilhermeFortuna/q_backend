"""WO179: a discovery run must report how many candidates died and why."""

from __future__ import annotations

from q_backend.api.strategy_search_jobs import _build_result_summary
from q_backend.optimization.models import ObjectiveMode
from q_backend.optimization.strategy_search import CandidateResult, StrategySearchResult


def _candidate(candidate_id: str, status: str, error: str | None = None) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate_id,
        strategy="CompositeStrategy",
        status=status,
        error=error,
    )


def test_summary_counts_and_groups_failed_candidates():
    result = StrategySearchResult(
        candidates=[
            _candidate("c1", "completed"),
            _candidate("c2", "error", "data source down"),
            _candidate("c3", "error", "data source down"),
            _candidate("c4", "no_result", "zero out-of-sample trades"),
            _candidate("c5", "unsupported"),
        ],
        objective_mode=ObjectiveMode.MAXIMIZE_NET_PROFIT,
        best=None,
    )

    summary = _build_result_summary(result)

    assert summary["candidate_count"] == 5
    assert summary["failed_candidate_count"] == 4
    assert summary["failure_reasons"] == {
        "data source down": 2,
        "zero out-of-sample trades": 1,
        "unsupported": 1,
    }


def test_summary_reports_no_failures_for_all_completed():
    result = StrategySearchResult(
        candidates=[_candidate("c1", "completed"), _candidate("c2", "completed")],
        objective_mode=ObjectiveMode.MAXIMIZE_NET_PROFIT,
        best=None,
    )

    summary = _build_result_summary(result)

    assert summary["failed_candidate_count"] == 0
    assert summary["failure_reasons"] == {}
