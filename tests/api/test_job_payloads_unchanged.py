from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch
import numpy as np
import pandas as pd
import pytest

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
from q_backend.api.routers.backtest import get_backtest_result, get_backtest_status
from q_backend.api.routers.experiments import (
    get_alpha_research_status,
    get_discovery_ab_status,
    get_encoder_ablation_status,
)
from q_backend.api.routers.neural import get_neural_training_status
from q_backend.api.routers.optimization import get_optimization_results, get_optimization_status
from q_backend.api.routers.storage import get_storage_ingest_status
from q_backend.api.routers.walkforward import get_walkforward_results, get_walkforward_status
from q_backend.api.routers.strategy_search import get_strategy_search_results, get_strategy_search_status
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
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, OptimizationConfig, StudyConfig
from q_backend.optimization.strategy_search import (
    GateConfig,
    GeneticSearchConfig,
    LockboxConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "job_payloads_pre_q012"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
HEX32_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$")
UUID_SUB_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
HEX32_SUB_RE = re.compile(r"[0-9a-f]{32}", re.I)


def to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(x) for x in obj]
    return obj


def normalize_payload(obj: Any) -> Any:
    import math

    obj = to_jsonable(obj)
    if isinstance(obj, dict):
        normalized = {}
        for k, v in obj.items():
            if k in ("id", "run_id", "study_id", "job_id", "evaluation_run_id"):
                normalized[k] = "<NORMALIZED_ID>"
            elif k in ("created_at", "updated_at", "started_at", "finished_at", "time", "timestamp"):
                normalized[k] = "<NORMALIZED_TIMESTAMP>"
            elif k in ("model_hash", "backend_version"):
                normalized[k] = "<NORMALIZED_HASH>"
            elif k == "root":
                normalized[k] = "<NORMALIZED_PATH>"
            else:
                normalized[k] = normalize_payload(v)
        return normalized
    elif isinstance(obj, list):
        return [normalize_payload(item) for item in obj]
    elif isinstance(obj, str):
        if UUID_RE.match(obj) or HEX32_RE.match(obj):
            return "<NORMALIZED_ID>"
        if HEX64_RE.match(obj):
            return "<NORMALIZED_HASH>"
        if ISO_TS_RE.match(obj):
            return "<NORMALIZED_TIMESTAMP>"
        s = UUID_SUB_RE.sub("<NORMALIZED_ID>", obj)
        s = HEX32_SUB_RE.sub("<NORMALIZED_ID>", s)
        return s
    elif isinstance(obj, float):
        if math.isnan(obj):
            return "<NAN>"
        return round(obj, 6)
    return obj


def _save_or_compare(kind: str, live_payload: dict[str, Any]) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    fixture_path = FIXTURES_DIR / f"{kind}.json"
    normalized_live = normalize_payload(live_payload)

    if not fixture_path.exists():
        fixture_path.write_text(json.dumps(normalized_live, indent=2, sort_keys=True), encoding="utf-8")
        return

    expected = json.loads(fixture_path.read_text(encoding="utf-8"))
    if normalized_live != expected:

        def diff(a, b, path=""):
            if type(a) != type(b):
                return [f"{path}: type mismatch {type(a)} != {type(b)}"]
            if isinstance(a, dict):
                d = []
                for k in set(a.keys()) | set(b.keys()):
                    if k not in a:
                        d.append(f"{path}.{k}: missing in live")
                    elif k not in b:
                        d.append(f"{path}.{k}: missing in expected")
                    else:
                        d.extend(diff(a[k], b[k], f"{path}.{k}"))
                return d
            if isinstance(a, list):
                if len(a) != len(b):
                    return [f"{path}: list len mismatch {len(a)} != {len(b)}"]
                d = []
                for i, (x, y) in enumerate(zip(a, b)):
                    d.extend(diff(x, y, f"{path}[{i}]"))
                return d
            if a != b:
                return [f"{path}: {a!r} != {b!r}"]
            return []

        differences = diff(normalized_live, expected)
        raise AssertionError(f"Payload mismatch for job kind {kind}:\n" + "\n".join(differences[:25]))


def test_backtest_payload_unchanged(run_jobs_sync):
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
    run_id = backtest_jobs.start_job(req)
    status = get_backtest_status(run_id)
    result = get_backtest_result(run_id)
    _save_or_compare("backtest", {"status": status, "result": result})


def test_optimization_payload_unchanged(run_jobs_sync):
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
    status = get_optimization_status(job.study_id)
    results = get_optimization_results(job.study_id)
    _save_or_compare("optimization", {"status": status, "result": results})


def test_walkforward_payload_unchanged(run_jobs_sync):
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
    status = get_walkforward_status(job.run_id)
    results = get_walkforward_results(job.run_id)
    _save_or_compare("walkforward", {"status": status, "result": results})


def test_strategy_search_payload_unchanged(run_jobs_sync):
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
    status = get_strategy_search_status(job.run_id)
    results = get_strategy_search_results(job.run_id)
    _save_or_compare("strategy_search", {"status": status, "result": results})


def test_alpha_research_payload_unchanged(run_jobs_sync):
    with patch("q_backend.alpha_research.preflight.read_ohlcv_fresh", return_value=[]):
        req = AlphaResearchRequest(
            profile_id="ccm_h1_swing",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 2, 1),
        )
        started = alpha_research_jobs.start_alpha_research_job(request=req)
        status = get_alpha_research_status(started["job_id"])
        _save_or_compare("alpha_research", {"status": status})


def test_encoder_ablation_payload_unchanged(run_jobs_sync, monkeypatch):
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
    status = get_encoder_ablation_status(started["job_id"])
    _save_or_compare("encoder_ablation", {"status": status})


def test_discovery_ab_payload_unchanged(run_jobs_sync, monkeypatch):
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
    status = get_discovery_ab_status(started["job_id"])
    _save_or_compare("discovery_ab", {"status": status})


def test_neural_training_payload_unchanged(run_jobs_sync, monkeypatch):
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
    status = get_neural_training_status(started["job_id"])
    _save_or_compare("neural_training", {"status": status})


def test_storage_ingest_payload_unchanged(run_jobs_sync):
    req = IngestJobRequest(
        symbol="PETR4",
        timeframes=["D1"],
        start=datetime(2024, 1, 1),
        end=datetime(2024, 2, 1),
        kind="bars",
    )
    job_id = storage_jobs.start_job(req)
    status = get_storage_ingest_status(job_id)
    _save_or_compare("storage_ingest", {"status": status})
