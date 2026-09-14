from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from q_backend.api import (
    alpha_research_jobs,
    backtest_jobs,
    discovery_ab_jobs,
    encoder_ablation_jobs,
    neural_jobs,
    optimization_jobs,
    storage_jobs,
    strategy_search_jobs,
    walkforward_jobs,
)
from q_backend.api.schemas.experiments import (
    AlphaResearchRequest,
    DiscoveryAbRequest,
    EncoderAblationRequest,
    EncoderConfigSpec,
)
from q_backend.api.schemas.neural import NeuralTrainRequest
from q_backend.api.storage_jobs import IngestJobRequest
from q_backend.api.walkforward_jobs import WalkForwardRequest
from q_backend.market_data.models import OHLCV
from q_backend.optimization.models import (
    ObjectiveConfig,
    ObjectiveMode,
    OptimizationConfig,
    StudyConfig,
)
from q_backend.optimization.strategy_search import (
    GateConfig,
    GeneticSearchConfig,
    LockboxConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.outbox_models import OutboxEvent
from q_backend.streaming.jobs import is_job_terminal_flagged

ALL_JOB_KINDS = [
    "backtest",
    "optimization",
    "walkforward",
    "strategy_search",
    "alpha_research",
    "encoder_ablation",
    "discovery_ab",
    "neural_training",
    "storage_ingest",
]


def _get_progress_entries(fake_redis, kind: str, job_id: str):
    raw_stream = fake_redis.xrange("q:stream:jobs.progress")
    entries = []
    for entry_id, fields in raw_stream:
        payload = json.loads(fields.get("p", "{}"))
        if payload.get("kind") == kind and payload.get("job_id") == job_id:
            entries.append((entry_id, payload))
    return entries


def _get_terminal_events(harness_session_factory, kind: str, job_id: str):
    with harness_session_factory() as session:
        return session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.topic == "jobs.terminal",
                OutboxEvent.producer_id == f"job-{kind}-{job_id}",
            )
        ).all()


def _run_kind(kind: str, run_jobs_sync, monkeypatch) -> str:
    if kind == "backtest":
        req = backtest_jobs.BacktestJobRequest(
            symbol="WIN$",
            timeframe="D1",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 15, tzinfo=timezone.utc),
            initial_capital=100000.0,
            point_value=0.2,
            strategy="MACrossover",
            strategy_params={"short_period": 3, "long_period": 6},
        )
        return backtest_jobs.start_job(req)

    elif kind == "optimization":
        config = OptimizationConfig.model_validate(
            {
                "study": {
                    "name": "WIN$ MA sweep",
                    "n_trials": 2,
                    "seed": 42,
                    "storage": {"type": "memory"},
                },
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-02-01T00:00:00",
                    "strategy": "MACrossover",
                },
                "search_space": {
                    "strategy_params": {
                        "short_period": {"type": "int", "low": 2, "high": 5},
                        "long_period": {"type": "int", "low": 10, "high": 20},
                    },
                    "risk_params": {
                        "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                        "quantity": {"type": "float", "low": 1.0, "high": 2.0},
                    },
                },
            }
        )
        job = optimization_jobs.start_job(config)
        return job.study_id

    elif kind == "walkforward":
        req = WalkForwardRequest.model_validate(
            {
                "optimization": {
                    "study": {
                        "name": "WF WIN$ MA",
                        "n_trials": 2,
                        "seed": 42,
                        "storage": {"type": "memory"},
                    },
                    "objective": {"mode": "maximize_net_profit"},
                    "backtest": {
                        "symbol": "WIN$",
                        "timeframe": "D1",
                        "start": "2024-01-01T00:00:00",
                        "end": "2024-04-30T00:00:00",
                        "initial_capital": 10_000.0,
                        "point_value": 1.0,
                        "strategy": "MACrossover",
                    },
                    "search_space": {
                        "strategy_params": {
                            "short_period": {"type": "int", "low": 2, "high": 4},
                            "long_period": {"type": "int", "low": 6, "high": 8},
                        },
                        "risk_params": {
                            "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                            "quantity": {"type": "float", "low": 1.0, "high": 1.0},
                        },
                    },
                },
                "walkforward": {
                    "train_days": 30,
                    "test_days": 15,
                    "mode": "rolling",
                    "min_windows": 2,
                },
            }
        )
        job = walkforward_jobs.start_job(req)
        return job.run_id

    elif kind == "strategy_search":
        config = StrategySearchConfig(
            backtest={
                "symbol": "WIN$",
                "timeframe": "D1",
                "start": datetime(2024, 1, 1).isoformat(),
                "end": datetime(2024, 4, 30).isoformat(),
                "initial_capital": 10_000.0,
                "point_value": 1.0,
                "strategy": "MACrossover",
            },
            objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
            walkforward=WalkForwardConfig(
                train_days=30,
                test_days=15,
                mode="rolling",
                min_windows=2,
            ),
            study=StudyConfig(
                name="Discovery WIN$ sweep",
                n_trials=2,
                seed=42,
                storage={"type": "memory"},
            ),
            strategies=["MACrossover"],
            include_risk_search=False,
            gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
        )
        job = strategy_search_jobs.start_job(config)
        return job.run_id

    elif kind == "alpha_research":
        with patch("q_backend.alpha_research.preflight.read_ohlcv_fresh", return_value=[]):
            req = AlphaResearchRequest(
                profile_id="ccm_h1_swing",
                start=datetime(2024, 1, 1),
                end=datetime(2024, 2, 1),
            )
            started = alpha_research_jobs.start_alpha_research_job(request=req)
            return started["job_id"]

    elif kind == "encoder_ablation":
        bars = [
            OHLCV(
                time=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0 + i * 0.01,
                tick_volume=1000,
            )
            for i in range(100)
        ]
        monkeypatch.setattr("q_backend.features.matrix.read_ohlcv_fresh", lambda *a, **k: bars)
        monkeypatch.setattr("q_backend.neural.gate.read_ohlcv_fresh", lambda *a, **k: bars)
        req = EncoderAblationRequest.model_validate(
            {
                "symbol": "SYNABL",
                "timeframe": "H1",
                "target": "fwd_return",
                "horizon": 3,
                "train_start": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "train_end": datetime(2024, 1, 4, tzinfo=timezone.utc),
                "n_latents": 2,
                "input_features": ["rsi", "ma"],
                "configs": [
                    EncoderConfigSpec(label="pca", encoder_kind="pca"),
                ],
            }
        )
        started = encoder_ablation_jobs.start_encoder_ablation_job(request=req)
        return started["job_id"]

    elif kind == "discovery_ab":
        from q_backend.features.sync import sync_registry_to_db

        with run_jobs_sync.harness_session_scope() as session:
            sync_registry_to_db(session)

        original_objective = discovery_ab_jobs._best_objective_from_child_run

        def _objective_with_fallback(run_id: str):
            value, metric, note = original_objective(run_id)
            if value is not None:
                return value, metric, note
            return 1.0, "oos_objective", ""

        monkeypatch.setattr(discovery_ab_jobs, "_best_objective_from_child_run", _objective_with_fallback)

        req = DiscoveryAbRequest(
            config=StrategySearchConfig(
                backtest={
                    "symbol": "WIN$",
                    "timeframe": "D1",
                    "start": datetime(2023, 1, 1).isoformat(),
                    "end": datetime(2023, 4, 1).isoformat(),
                    "initial_capital": 10_000.0,
                    "point_value": 1.0,
                    "strategy": "MACrossover",
                },
                study=StudyConfig(
                    name="discovery_ab_baseline",
                    n_trials=1,
                    seed=42,
                    storage={"type": "memory"},
                ),
                objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
                walkforward=WalkForwardConfig(
                    train_days=30,
                    test_days=15,
                    mode="rolling",
                    min_windows=2,
                ),
                strategies=["MACrossover"],
                gates=GateConfig(min_completed_windows=1, min_oos_trades=0),
                genetic=GeneticSearchConfig(
                    population_size=10,
                    generations=2,
                    elite_count=1,
                    init_seed=99,
                    crossover_rate=0.7,
                    mutation_rate=0.2,
                    tournament_size=2,
                    max_nodes=6,
                    max_depth=4,
                    min_seed_signals=0,
                    prescreen_min_signals=0,
                ),
                lockbox=LockboxConfig(enabled=False),
            ),
            seeds=[1, 2],
        )
        started = discovery_ab_jobs.start_discovery_ab_job(request=req)
        return started["job_id"]

    elif kind == "neural_training":
        req = NeuralTrainRequest.model_validate(
            {
                "kind": "pca",
                "symbol": "TEST",
                "timeframe": "H1",
                "train_start": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "train_end": datetime(2024, 1, 5, tzinfo=timezone.utc),
                "n_latents": 2,
                "input_features": ["feature_01", "feature_02"],
                "model_key": "job_test_pca",
            }
        )
        idx = pd.date_range(req.train_start, req.train_end, freq="h", tz="UTC")
        df = pd.DataFrame(np.zeros((len(idx), 2)), index=idx, columns=req.input_features)
        monkeypatch.setattr(
            "q_backend.neural.training_pipeline.build_training_feature_window",
            lambda *_a, **_k: df,
        )
        started = neural_jobs.start_training_job(request=req)
        return started["job_id"]

    elif kind == "storage_ingest":
        req = IngestJobRequest(
            symbol="PETR4",
            timeframes=["D1"],
            start=datetime(2024, 1, 1),
            end=datetime(2024, 2, 1),
            kind="bars",
        )
        return storage_jobs.start_job(req)

    raise ValueError(f"Unknown kind: {kind}")


@pytest.mark.parametrize("kind", ALL_JOB_KINDS)
def test_job_events_completion(run_jobs_sync, monkeypatch, kind):
    job_id = _run_kind(kind, run_jobs_sync, monkeypatch)

    # 1. Assert at least one progress event in jobs.progress
    progress_entries = _get_progress_entries(run_jobs_sync, kind, job_id)
    assert len(progress_entries) >= 1, f"Expected at least 1 progress event for {kind}:{job_id}"
    for _, payload in progress_entries:
        assert payload["status"] in ("queued", "running")
        assert payload["kind"] == kind
        assert payload["job_id"] == job_id

    # 2. Assert exactly one terminal event in jobs.terminal
    terminal_events = _get_terminal_events(run_jobs_sync.harness_session_factory, kind, job_id)
    assert len(terminal_events) == 1, f"Expected exactly 1 terminal event for {kind}:{job_id}"
    term_payload = terminal_events[0].payload
    assert term_payload["kind"] == kind
    assert term_payload["job_id"] == job_id
    assert term_payload["status"] == "completed"
    assert "finished_at" in term_payload

    # 3. Assert terminal flag is set in Redis
    assert is_job_terminal_flagged(run_jobs_sync, kind, job_id)


def test_job_events_forced_failure(run_jobs_sync, monkeypatch):
    """Test forced failure on storage_ingest and backtest."""
    # Forced failure on backtest by runner throwing exception
    from q_backend.api.backtest_jobs import BacktestJobRequest

    req = BacktestJobRequest(
        symbol="WIN$",
        timeframe="D1",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 15, tzinfo=timezone.utc),
        initial_capital=100000.0,
        point_value=0.2,
        strategy="InvalidStrategyNameDoesNotExist",
    )
    bt_id = backtest_jobs.start_job(req)
    terminal_events = _get_terminal_events(run_jobs_sync.harness_session_factory, "backtest", bt_id)
    assert len(terminal_events) == 1
    assert terminal_events[0].payload["status"] == "failed"
    assert terminal_events[0].payload["error"] is not None


def test_job_events_cancellation(run_jobs_sync, monkeypatch):
    """Test cancellation variant on optimization."""
    config = OptimizationConfig.model_validate(
        {
            "study": {
                "name": "Cancel Study",
                "n_trials": 10,
                "seed": 42,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "WIN$",
                "start": "2024-01-01T00:00:00",
                "end": "2024-02-01T00:00:00",
                "strategy": "MACrossover",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 5},
                    "long_period": {"type": "int", "low": 10, "high": 20},
                },
            },
        }
    )
    # Cancel the study
    study_id, db_study_id = optimization_jobs._persist_study_start(config)
    optimization_jobs.request_cancel(study_id)

    terminal_events = _get_terminal_events(run_jobs_sync.harness_session_factory, "optimization", study_id)
    assert len(terminal_events) == 1
    assert terminal_events[0].payload["status"] == "cancelled"
