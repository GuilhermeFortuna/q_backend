"""Alpha-research experiment job tests (WO164)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import q_backend.backtesting.strategies  # noqa: F401 — register CompositeStrategy

from q_backend.alpha_research.preflight import run_data_preflight
from q_backend.api import alpha_research_jobs
from q_backend.api.routers.experiments import (
    get_alpha_research_status,
    start_alpha_research,
)
from q_backend.api.schemas.experiments import AlphaResearchRequest
from q_backend.market_data.models import OHLCV
from q_backend.optimization.hypothesis import AdmittedAllFeatureAdmissionResolver
from q_backend.optimization.research_acceptance import (
    AcceptanceCriterion,
    PlateauResult,
    ResearchAcceptanceConfig,
    ResearchAcceptanceResult,
    SeedRunRecord,
    TailDiagnostics,
)
from q_backend.storage.db.base import Base
from q_backend.storage.lake.artifacts import read_alpha_research_result
from q_backend.storage.settings import get_settings

_N_BARS = 600


def _synthetic_ohlcv_records(
    symbol: str = "CCM$",
    timeframe: str = "H1",
    start: datetime | None = None,
) -> list[OHLCV]:
    start = start or datetime(2024, 1, 1)
    rows: list[OHLCV] = []
    price = 100.0
    for index in range(_N_BARS):
        timestamp = start + timedelta(hours=index)
        drift = 0.08 if index % 12 < 6 else -0.04
        price = max(50.0, price + drift)
        rows.append(
            OHLCV(
                time=timestamp,
                open=price - 0.2,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                tick_volume=1000,
            )
        )
    return rows


def _alpha_request(**overrides) -> AlphaResearchRequest:
    payload = {
        "profile_id": "ccm_h1_swing",
        "start": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "end": datetime(2024, 4, 1, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return AlphaResearchRequest(**payload)


def _seed_record(seed: int, return_pct: float) -> SeedRunRecord:
    return SeedRunRecord(
        seed=seed,
        status="completed",
        best_params={"strategy_params": {"period": 20}},
        oos_metrics={
            "total_return_pct": return_pct,
            "total_trades": 40,
            "sharpe_ratio": 1.1,
        },
        window_returns=[return_pct] * 6,
        window_count=6,
        completed_windows=6,
        objective_value=return_pct,
    )


def _acceptance_result(
    *,
    candidate_id: str,
    verdict: str,
    return_pct: float,
    acceptance_id: str,
) -> ResearchAcceptanceResult:
    seeds = [_seed_record(seed, return_pct) for seed in range(5)]
    return ResearchAcceptanceResult(
        acceptance_id=acceptance_id,
        candidate_id=candidate_id,
        verdict=verdict,  # type: ignore[arg-type]
        criteria=[
            AcceptanceCriterion(
                name="aggregate_oos_return",
                status="passed" if return_pct > 0 else "failed",
                observed=return_pct,
                threshold="> 0",
                reason="test",
            )
        ],
        champion_seed=4,
        champion_hash="hash",
        seeds=seeds,
        plateau=PlateauResult(
            neighbors_evaluated=4,
            profitable_fraction=0.75,
            score_retention=0.8,
            champion_objective=return_pct,
        ),
        tail_diagnostics=TailDiagnostics(),
        dsr_value=0.99 if return_pct > 0 else 0.4,
        effective_attempt_count=10,
        lockbox_metrics=None,
    )


@pytest.fixture
def alpha_db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def alpha_session_factory(alpha_db_engine):
    return sessionmaker(
        bind=alpha_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def alpha_session_scope(alpha_session_factory):
    @contextmanager
    def test_session_scope():
        session = alpha_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return test_session_scope


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_preflight_insufficient_data_returns_inconclusive():
    with patch(
        "q_backend.alpha_research.preflight.read_ohlcv",
        return_value=[],
    ):
        result = run_data_preflight(
            profile_id="ccm_h1_swing",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 2, 1),
        )
    assert result.verdict == "inconclusive"
    assert any("No local OHLCV" in reason for reason in result.reasons)


def test_preflight_rejects_wdo_m5_profile():
    from q_backend.optimization.hypothesis import (
        InstrumentResearchProfile,
        RESEARCH_PROFILES,
        SessionRules,
    )
    from q_backend.optimization.research_acceptance import ResearchAcceptanceConfig

    blocked = InstrumentResearchProfile(
        profile_id="wdo_m5_blocked",
        symbol="WDO$",
        timeframe="M5",
        style="day_trade",
        target_horizons=[2],
        session_rules=SessionRules(day_trade=True),
        research_acceptance=ResearchAcceptanceConfig(min_stitched_oos_trades=100),
    )
    with patch.dict(RESEARCH_PROFILES, {"wdo_m5_blocked": blocked}, clear=False):
        result = run_data_preflight(
            profile_id="wdo_m5_blocked",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 6, 1),
        )
    assert result.verdict == "inconclusive"
    assert "M5" in result.reasons[0]


def test_alpha_research_inconclusive_before_expensive_stages(
    run_jobs_sync,
    lake_root_path,
    alpha_session_scope,
    monkeypatch,
):
    monkeypatch.setattr(
        "q_backend.api.alpha_research_jobs.session_scope",
        alpha_session_scope,
    )
    with patch(
        "q_backend.alpha_research.preflight.read_ohlcv",
        return_value=[],
    ):
        started = alpha_research_jobs.start_alpha_research_job(
            request=_alpha_request(),
        )
    payload = alpha_research_jobs.get_alpha_research_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["result"]["verdict"] == "inconclusive"
    stage_names = {stage["name"] for stage in payload["result"]["stages"]}
    assert "preflight" in stage_names
    assert "feature_evidence" not in stage_names or all(
        stage["name"] != "feature_evidence" or stage["status"] != "completed"
        for stage in payload["result"]["stages"]
    )


def test_alpha_research_ready_for_paper_planted_edge(
    run_jobs_sync,
    lake_root_path,
    alpha_session_scope,
    monkeypatch,
):
    monkeypatch.setattr(
        "q_backend.api.alpha_research_jobs.session_scope",
        alpha_session_scope,
    )

    def _mock_read_ohlcv(symbol, timeframe, start, end):
        return _synthetic_ohlcv_records(symbol=symbol, timeframe=timeframe)

    def _mock_run_acceptance(**kwargs):
        candidate_id = kwargs["candidate"].candidate_id
        return _acceptance_result(
            candidate_id=candidate_id,
            verdict="ready_for_paper",
            return_pct=0.06,
            acceptance_id=kwargs.get("acceptance_id", "acc"),
        )

    def _mock_lockbox(**kwargs):
        return (
            {
                "total_return_pct": 0.04,
                "sharpe_ratio": 0.9,
                "total_trades": 8,
                "max_drawdown_pct": 0.05,
            },
            True,
            None,
        )

    with (
        patch("q_backend.alpha_research.preflight.read_ohlcv", side_effect=_mock_read_ohlcv),
        patch(
            "q_backend.api.alpha_research_jobs.run_profile_feature_evidence",
            return_value=[],
        ),
        patch(
            "q_backend.api.alpha_research_jobs.create_profile_admission_resolver",
            return_value=AdmittedAllFeatureAdmissionResolver(),
        ),
        patch(
            "q_backend.api.alpha_research_jobs.run_research_acceptance",
            side_effect=_mock_run_acceptance,
        ),
        patch(
            "q_backend.optimization.lockbox.evaluate_lockbox",
            side_effect=_mock_lockbox,
        ),
    ):
        started = alpha_research_jobs.start_alpha_research_job(request=_alpha_request())

    payload = alpha_research_jobs.get_alpha_research_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["result"]["verdict"] == "ready_for_paper"
    completed_stages = {
        stage["name"]
        for stage in payload["result"]["stages"]
        if stage["status"] == "completed"
    }
    assert "preflight" in completed_stages
    assert "feature_evidence" in completed_stages
    assert "hypothesis_eligibility" in completed_stages
    assert "candidate_evaluation" in completed_stages
    assert "acceptance" in completed_stages
    lake_result = read_alpha_research_result(started["job_id"])
    assert lake_result["provenance"]["profile_id"] == "ccm_h1_swing"


def test_alpha_research_no_edge_returns_rejected(
    run_jobs_sync,
    lake_root_path,
    alpha_session_scope,
    monkeypatch,
):
    monkeypatch.setattr(
        "q_backend.api.alpha_research_jobs.session_scope",
        alpha_session_scope,
    )

    def _mock_run_acceptance(**kwargs):
        candidate_id = kwargs["candidate"].candidate_id
        return _acceptance_result(
            candidate_id=candidate_id,
            verdict="rejected",
            return_pct=-0.03,
            acceptance_id=kwargs.get("acceptance_id", "acc"),
        )

    with (
        patch(
            "q_backend.alpha_research.preflight.read_ohlcv",
            return_value=_synthetic_ohlcv_records(),
        ),
        patch(
            "q_backend.api.alpha_research_jobs.run_profile_feature_evidence",
            return_value=[],
        ),
        patch(
            "q_backend.api.alpha_research_jobs.create_profile_admission_resolver",
            return_value=AdmittedAllFeatureAdmissionResolver(),
        ),
        patch(
            "q_backend.api.alpha_research_jobs.run_research_acceptance",
            side_effect=_mock_run_acceptance,
        ),
        patch(
            "q_backend.optimization.lockbox.evaluate_lockbox",
            return_value=({"total_return_pct": -0.01, "sharpe_ratio": -0.2, "total_trades": 6}, False, None),
        ),
    ):
        started = alpha_research_jobs.start_alpha_research_job(request=_alpha_request())

    payload = alpha_research_jobs.get_alpha_research_status_payload(started["job_id"])
    assert payload["result"]["verdict"] == "rejected"


def test_alpha_research_cancellation(
    run_jobs_sync,
    lake_root_path,
    alpha_session_scope,
    monkeypatch,
):
    monkeypatch.setattr(
        "q_backend.api.alpha_research_jobs.session_scope",
        alpha_session_scope,
    )

    def _cancel_midway(**kwargs):
        alpha_research_jobs.request_cancel(kwargs["acceptance_id"].split("__")[0])
        return _acceptance_result(
            candidate_id=kwargs["candidate"].candidate_id,
            verdict="inconclusive",
            return_pct=0.01,
            acceptance_id=kwargs.get("acceptance_id", "acc"),
        )

    with (
        patch(
            "q_backend.alpha_research.preflight.read_ohlcv",
            return_value=_synthetic_ohlcv_records(),
        ),
        patch(
            "q_backend.api.alpha_research_jobs.run_profile_feature_evidence",
            return_value=[],
        ),
        patch(
            "q_backend.api.alpha_research_jobs.create_profile_admission_resolver",
            return_value=AdmittedAllFeatureAdmissionResolver(),
        ),
        patch(
            "q_backend.api.alpha_research_jobs.run_research_acceptance",
            side_effect=_cancel_midway,
        ),
    ):
        started = alpha_research_jobs.start_alpha_research_job(request=_alpha_request())

    payload = alpha_research_jobs.get_alpha_research_status_payload(started["job_id"])
    assert payload["status"] in {"failed", "cancelled", "completed"}


def test_reconcile_resumes_pre_lockbox_but_not_consumed_holdout(
    run_jobs_sync,
    lake_root_path,
    monkeypatch,
):
    job_id = "resume-job"
    request = _alpha_request()
    checkpoint = {
        "job_id": job_id,
        "request": request.model_dump(mode="json"),
        "preflight_complete": True,
        "manifest": {
            "symbol": "CCM$",
            "timeframe": "H1",
            "range_start": "2024-01-01T00:00:00Z",
            "range_end": "2024-04-01T00:00:00Z",
            "fractions": [0.4, 0.4, 0.2],
            "evidence": {
                "name": "evidence",
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-02-01T00:00:00Z",
                "bar_count": 240,
            },
            "walkforward": {
                "name": "walkforward",
                "start": "2024-02-01T00:00:00Z",
                "end": "2024-03-15T00:00:00Z",
                "bar_count": 240,
            },
            "lockbox": {
                "name": "lockbox",
                "start": "2024-03-15T00:00:00Z",
                "end": "2024-04-01T00:00:00Z",
                "bar_count": 120,
            },
            "manifest_hash": "manifest-hash",
            "data_fingerprint": "fingerprint",
        },
        "feature_evidence_complete": True,
        "lockbox_consumed": False,
    }
    from q_backend.storage.lake.artifacts import write_alpha_research_checkpoint

    write_alpha_research_checkpoint(job_id, checkpoint)
    alpha_research_jobs._persist_progress(
        job_id,
        alpha_research_jobs._base_payload(job_id, status="running", progress=0.5),
    )

    calls: list[bool] = []

    with patch(
        "q_backend.tasks.actors.run_alpha_research.send",
        side_effect=lambda job_id, request_json, resume=False: calls.append(resume),
    ):
        count = alpha_research_jobs.reconcile_orphaned_runs()
    assert count >= 1
    assert calls and calls[0] is True

    checkpoint["lockbox_consumed"] = True
    write_alpha_research_checkpoint(job_id, checkpoint)
    alpha_research_jobs._persist_progress(
        job_id,
        alpha_research_jobs._base_payload(job_id, status="running", progress=0.9),
    )
    alpha_research_jobs.reconcile_orphaned_runs()
    payload = alpha_research_jobs.get_alpha_research_status_payload(job_id)
    assert payload["status"] == "failed"


def test_alpha_research_route_smoke(
    run_jobs_sync,
    lake_root_path,
    alpha_session_scope,
    monkeypatch,
):
    monkeypatch.setattr(
        "q_backend.api.alpha_research_jobs.session_scope",
        alpha_session_scope,
    )
    with (
        patch(
            "q_backend.alpha_research.preflight.read_ohlcv",
            return_value=[],
        ),
    ):
        started = start_alpha_research(_alpha_request())
    job_id = started["job_id"] if isinstance(started, dict) else started.job_id
    status = get_alpha_research_status(job_id)
    status_value = status["status"] if isinstance(status, dict) else status.status
    result = status["result"] if isinstance(status, dict) else status.result
    assert status_value == "completed"
    assert result is not None
    verdict = result["verdict"] if isinstance(result, dict) else result.verdict
    assert verdict == "inconclusive"


def test_alpha_research_unknown_job_returns_404():
    with pytest.raises(HTTPException) as exc:
        get_alpha_research_status("missing-job")
    assert exc.value.status_code == 404
