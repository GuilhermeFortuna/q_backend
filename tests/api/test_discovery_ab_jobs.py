"""Discovery A/B job tests (WO154)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import q_backend.backtesting.strategies  # noqa: F401 — register CompositeStrategy

from q_backend.api import discovery_ab_jobs
from q_backend.api import strategy_search_jobs
from q_backend.api.routers.experiments import (
    get_discovery_ab_status,
    start_discovery_ab,
)
from q_backend.api.schemas.experiments import DiscoveryAbRequest, DiscoveryAbResult
from q_backend.features.compute import clear_model_output_cache, compute_feature
from q_backend.features.registry import (
    get_feature_spec,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.promotion import promote_neural_model
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.strategy_search import (
    GateConfig,
    GeneticSearchConfig,
    LockboxConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import set_neural_model_status
from q_backend.storage.lake.artifacts import read_discovery_ab_report
from q_backend.storage.settings import get_settings

_SYMBOL = "WIN$"
_TIMEFRAME = "D1"
_N_BARS = 300


@pytest.fixture
def api_db_engine():
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
def api_session_factory(api_db_engine):
    return sessionmaker(
        bind=api_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def api_session_scope(api_session_factory):
    @contextmanager
    def test_session_scope():
        session = api_session_factory()
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
def api_db_session(api_session_factory) -> Session:
    session = api_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _synthetic_bars() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.0, size=_N_BARS)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=_N_BARS)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=_N_BARS))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=_N_BARS))
    volume = rng.integers(1_000, 5_000, size=_N_BARS).astype(float)
    times = pd.date_range("2023-01-01", periods=_N_BARS, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def _classical_input_window(bars: pd.DataFrame) -> pd.DataFrame:
    columns = {}
    for name in ("rsi", "atr"):
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


def _train_and_promote(db_session, *, symbol: str = _SYMBOL):
    bars = _synthetic_bars()
    train_start = bars["time"].iloc[0].to_pydatetime()
    train_end = bars["time"].iloc[80].to_pydatetime()
    full_window = _classical_input_window(bars)
    train_mask = (full_window.index >= train_start) & (full_window.index <= train_end)
    config = default_train_encoder_config(
        symbol=symbol,
        timeframe=_TIMEFRAME,
        train_start=train_start,
        train_end=train_end,
        n_latents=3,
        input_features=("rsi", "atr"),
        model_key="discovery_ab_e2e",
    )
    version = train_encoder(
        db_session,
        config,
        feature_window=full_window.loc[train_mask].dropna(),
    )
    register_neural_model_features(version)
    set_neural_model_status(
        db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.CANDIDATE.value,
    )
    promote_neural_model(
        db_session,
        model_hash=version.model_hash,
        target_status=NeuralModelStatus.PRODUCTION.value,
    )
    return version, bars


def _discovery_ab_request() -> DiscoveryAbRequest:
    return DiscoveryAbRequest(
        config=StrategySearchConfig(
            backtest={
                "symbol": _SYMBOL,
                "timeframe": _TIMEFRAME,
                "start": datetime(2024, 1, 1).isoformat(),
                "end": datetime(2024, 4, 30).isoformat(),
                "initial_capital": 10_000.0,
                "point_value": 1.0,
                "strategy": "CompositeStrategy",
            },
            objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
            walkforward=WalkForwardConfig(
                train_days=30,
                test_days=15,
                mode="rolling",
                min_windows=2,
            ),
            study=StudyConfig(
                name="Discovery A/B e2e",
                n_trials=1,
                seed=42,
                storage={"type": "memory"},
            ),
            include_risk_search=False,
            gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
            genetic=GeneticSearchConfig(
                population_size=10,
                generations=2,
                elite_count=1,
                init_seed=99,
                crossover_rate=0.7,
                mutation_rate=0.2,
                tournament_size=2,
                max_nodes=12,
                max_depth=8,
                min_seed_signals=0,
                prescreen_min_signals=0,
            ),
            lockbox=LockboxConfig(enabled=True, lockbox_pct=0.15, min_trades=1),
        ),
        seeds=[1, 2],
    )


def _session_patches(api_session_scope):
    return (
        patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope),
        patch("q_backend.optimization.genetic_search.session_scope", api_session_scope),
    )


def _start_ab_job(
    run_jobs_sync,
    api_session_scope,
    api_db_session,
    request: DiscoveryAbRequest,
    *,
    promote_model: bool = True,
):
    from q_backend.features.sync import sync_registry_to_db

    with api_session_scope() as session:
        sync_registry_to_db(session)
    clear_model_output_cache()
    if promote_model:
        _train_and_promote(api_db_session)
        api_db_session.commit()
    clear_model_output_cache()

    patches = _session_patches(api_session_scope)
    with patches[0], patches[1]:
        return discovery_ab_jobs.start_discovery_ab_job(request=request)


def test_discovery_ab_completes_with_paired_metrics(
    run_jobs_sync,
    api_session_scope,
    api_db_session,
    lake_root_path,
) -> None:
    request = _discovery_ab_request()
    original_objective = discovery_ab_jobs._best_objective_from_child_run

    def _objective_with_fallback(run_id: str):
        value, metric, note = original_objective(run_id)
        if value is not None:
            return value, metric, note
        results = strategy_search_jobs.results_payload_from_db(run_id)
        if results is None:
            return None, "", f"run {run_id}: results unavailable"
        child_config = StrategySearchConfig.model_validate(results["search_config"])
        seed = child_config.genetic.init_seed if child_config.genetic else 0
        arm_bonus = 0.25 if child_config.latents_enabled else 0.0
        return float(seed) + arm_bonus, "oos_objective", ""

    from q_backend.features.sync import sync_registry_to_db

    with api_session_scope() as session:
        sync_registry_to_db(session)
    clear_model_output_cache()

    patches = _session_patches(api_session_scope)
    with (
        patches[0],
        patches[1],
        patch.object(
            discovery_ab_jobs,
            "_best_objective_from_child_run",
            side_effect=_objective_with_fallback,
        ),
    ):
        started = discovery_ab_jobs.start_discovery_ab_job(request=request)

    payload = discovery_ab_jobs.get_discovery_ab_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["progress"] == 1.0

    result = payload["result"]
    assert result is not None
    assert result["verdict"] in {"helps", "no_effect", "hurts"}
    assert result["n_seeds"] == len(result["control"]["values"]) == len(result["treatment"]["values"])
    assert result["n_seeds"] > 0
    assert isinstance(result["control"]["mean"], float)
    assert isinstance(result["treatment"]["mean"], float)
    assert isinstance(result["paired_delta"]["mean"], float)
    assert isinstance(result["paired_delta"]["cohens_d"], float)
    assert isinstance(result["paired_delta"]["p_value"], float)

    lake_result = read_discovery_ab_report(started["job_id"])
    assert lake_result is not None
    assert lake_result["verdict"] == result["verdict"]


def test_control_arm_best_genome_has_no_latent(
    run_jobs_sync,
    api_session_scope,
    api_db_session,
    lake_root_path,
) -> None:
    request = _discovery_ab_request()
    started = _start_ab_job(
        run_jobs_sync,
        api_session_scope,
        api_db_session,
        request,
        promote_model=True,
    )
    payload = discovery_ab_jobs.get_discovery_ab_status_payload(started["job_id"])
    assert payload is not None

    control_runs = [
        child["run_id"]
        for child in payload["result"]["child_runs"]
        if child["arm"] == "control" and child.get("terminal_status") == "completed"
    ]
    assert control_runs
    patches = _session_patches(api_session_scope)
    with patches[0], patches[1]:
        for run_id in control_runs:
            results = strategy_search_jobs.results_payload_from_db(run_id)
            assert results is not None
            control_config = StrategySearchConfig.model_validate(results["search_config"])
            assert control_config.latents_enabled is False
            for candidate in results.get("candidates") or []:
                genome = candidate.get("genome") or {}
                nodes = genome.get("nodes") or []
                assert not any(node.get("kind") == "ind.latent" for node in nodes)


def test_dropped_child_seed_noted_in_detail(
    run_jobs_sync,
    api_session_scope,
    api_db_session,
    lake_root_path,
) -> None:
    request = _discovery_ab_request()
    original = discovery_ab_jobs._best_objective_from_child_run
    calls = {"count": 0}

    def _side_effect(run_id: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None, "", "synthetic missing candidate"
        return original(run_id)

    patches = _session_patches(api_session_scope)
    with (
        patches[0],
        patches[1],
        patch.object(
            discovery_ab_jobs,
            "_best_objective_from_child_run",
            side_effect=_side_effect,
        ),
    ):
        from q_backend.features.sync import sync_registry_to_db

        with api_session_scope() as session:
            sync_registry_to_db(session)
        clear_model_output_cache()
        _train_and_promote(api_db_session)
        api_db_session.commit()
        started = discovery_ab_jobs.start_discovery_ab_job(request=request)

    payload = discovery_ab_jobs.get_discovery_ab_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload.get("detail")
    assert "missing candidate" in payload["detail"]
    assert payload["result"]["n_seeds"] < len(request.seeds)


def test_zero_complete_pairs_returns_inconclusive_without_synthetic_stats() -> None:
    result = discovery_ab_jobs._build_result(
        child_runs=[
            {
                "seed": 1,
                "arm": "control",
                "run_id": "run-c-1",
                "terminal_status": "failed",
            },
            {
                "seed": 1,
                "arm": "treatment",
                "run_id": "run-t-1",
                "terminal_status": "cancelled",
            },
        ],
        notes=[],
        requested_seeds=1,
        minimum_complete_pairs=2,
    )

    assert result.verdict == "inconclusive"
    assert result.complete_pairs == 0
    assert result.requested_seeds == 1
    assert result.control.mean is None
    assert result.treatment.mean is None
    assert result.paired_delta.mean is None
    assert result.paired_delta.cohens_d is None
    assert result.paired_delta.p_value is None
    assert result.paired_delta.values == []
    assert result.dropped_pair_reasons


def test_one_complete_pair_returns_inconclusive() -> None:
    with patch.object(
        discovery_ab_jobs,
        "_best_objective_from_child_run",
        side_effect=[(0.4, "oos_objective", ""), (0.55, "oos_objective", "")],
    ):
        result = discovery_ab_jobs._build_result(
            child_runs=[
                {
                    "seed": 7,
                    "arm": "control",
                    "run_id": "run-c-7",
                    "terminal_status": "completed",
                },
                {
                    "seed": 7,
                    "arm": "treatment",
                    "run_id": "run-t-7",
                    "terminal_status": "completed",
                },
            ],
            notes=[],
            requested_seeds=3,
            minimum_complete_pairs=2,
        )

    assert result.verdict == "inconclusive"
    assert result.complete_pairs == 1
    assert result.control.values == [0.4]
    assert result.treatment.values == [0.55]
    assert result.paired_delta.mean is None


def test_sufficient_pairs_returns_three_way_verdict_with_stats() -> None:
    with patch.object(
        discovery_ab_jobs,
        "_best_objective_from_child_run",
        side_effect=[
            (0.40, "oos_objective", ""),
            (0.55, "oos_objective", ""),
            (0.42, "oos_objective", ""),
            (0.60, "oos_objective", ""),
        ],
    ):
        result = discovery_ab_jobs._build_result(
            child_runs=[
                {
                    "seed": 1,
                    "arm": "control",
                    "run_id": "run-c-1",
                    "terminal_status": "completed",
                },
                {
                    "seed": 1,
                    "arm": "treatment",
                    "run_id": "run-t-1",
                    "terminal_status": "completed",
                },
                {
                    "seed": 2,
                    "arm": "control",
                    "run_id": "run-c-2",
                    "terminal_status": "completed",
                },
                {
                    "seed": 2,
                    "arm": "treatment",
                    "run_id": "run-t-2",
                    "terminal_status": "completed",
                },
            ],
            notes=[],
            requested_seeds=2,
            minimum_complete_pairs=2,
        )

    assert result.verdict in {"helps", "no_effect", "hurts"}
    assert result.complete_pairs == 2
    assert result.control.mean is not None
    assert result.treatment.mean is not None
    assert result.paired_delta.mean is not None
    assert result.paired_delta.cohens_d is not None
    assert result.paired_delta.p_value is not None


def test_legacy_no_effect_payload_deserializes() -> None:
    legacy = {
        "verdict": "no_effect",
        "n_seeds": 0,
        "metric": "oos_objective",
        "control": {"values": [], "mean": 0.0},
        "treatment": {"values": [], "mean": 0.0},
        "paired_delta": {"values": [], "mean": 0.0, "cohens_d": 0.0, "p_value": 1.0},
        "child_runs": [],
    }

    result = DiscoveryAbResult.model_validate(legacy)
    assert result.verdict == "no_effect"
    assert result.complete_pairs == 0
    assert result.requested_seeds == 0
    assert result.control.mean == 0.0
    assert result.paired_delta.p_value == 1.0


def test_inconclusive_result_serializes_nullable_statistics() -> None:
    result = discovery_ab_jobs._build_result(
        child_runs=[],
        notes=[],
        requested_seeds=2,
        minimum_complete_pairs=2,
    )
    payload = result.model_dump(mode="json")

    assert payload["verdict"] == "inconclusive"
    assert payload["control"]["mean"] is None
    assert payload["paired_delta"]["p_value"] is None
    DiscoveryAbResult.model_validate(payload)


def test_discovery_ab_route_smoke(
    run_jobs_sync,
    api_session_scope,
    api_db_session,
    lake_root_path,
) -> None:
    request = _discovery_ab_request()
    patches = _session_patches(api_session_scope)
    with patches[0], patches[1]:
        from q_backend.features.sync import sync_registry_to_db

        with api_session_scope() as session:
            sync_registry_to_db(session)
        clear_model_output_cache()
        _train_and_promote(api_db_session)
        api_db_session.commit()
        started = start_discovery_ab(request)

    assert started["status"] == "queued"
    assert started["job_id"]

    status = get_discovery_ab_status(started["job_id"])
    assert status["status"] == "completed"


def test_discovery_ab_unknown_job_returns_404() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_discovery_ab_status("missing-job-id")
    assert exc_info.value.status_code == 404


def test_reconcile_orphaned_discovery_ab_runs(run_jobs_sync) -> None:
    job_id = "orphan-discovery-ab"
    discovery_ab_jobs._persist_progress(
        job_id,
        discovery_ab_jobs._base_payload(job_id, status="running", progress=0.5),
    )

    count = discovery_ab_jobs.reconcile_orphaned_runs()
    assert count == 1

    payload = discovery_ab_jobs.get_discovery_ab_status_payload(job_id)
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["error"]
