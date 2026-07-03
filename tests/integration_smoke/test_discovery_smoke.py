"""Deterministic end-to-end discovery-pipeline smoke suite (WO182).

One module that answers "is discovery healthy end-to-end?" by driving the *real*
production wiring: ``start_job`` → coordinator → per-candidate genetic workers →
generation barrier / breeding → finalizer → DB + lake persistence → results payload.

Nothing here is a test-only orchestration shortcut. Faking is confined to the two
seams the guardrails allow:

* **Data provider** — ``run_jobs_sync`` (tests/conftest.py) rewires every actor
  ``.send`` to run its orchestration function in-process and feeds the workers a
  fixed-seed synthetic OHLCV frame (``_synthetic_ohlcv``). The job layer, genetic
  search, persistence, and finalizer all run unmodified.
* **Storage** — a per-test in-memory SQLite database (the same test-DB approach
  ``tests/api/test_strategy_search_persistence.py`` uses) plus ``fakeredis``. Both
  the job layer (``strategy_search_jobs.session_scope``) and the genetic provider's
  latent-universe resolution (``genetic_search.session_scope``) are pointed at the
  one SQLite database so a registered PRODUCTION model is visible to the run.

No network, no MT5, no live market data. Fixed seeds everywhere.
"""

from __future__ import annotations

import math
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import q_backend.backtesting.strategies  # noqa: F401 — register CompositeStrategy

from q_backend.api import (
    alpha_research_jobs,
    backtest_jobs,
    discovery_ab_jobs,
    encoder_ablation_jobs,
    optimization_jobs,
    strategy_search_jobs,
    walkforward_jobs,
)
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import validate_genome
from q_backend.optimization import genetic_search
from q_backend.optimization.genetic_search import create_genetic_candidate_provider
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.strategy_search import (
    GateConfig,
    GeneticSearchConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import (
    create_backtest_run,
    create_optimization_study,
    create_strategy_search_run,
    create_walkforward_run,
    get_strategy_search_run,
)
from q_backend.storage.redis.progress import get_job_progress, set_job_progress
from q_backend.storage.settings import get_settings

SMOKE_SYMBOL = "SMOKE"
SMOKE_TIMEFRAME = "D1"
SMOKE_START = datetime(2024, 1, 1)
SMOKE_END = datetime(2024, 4, 30)

# Small-but-real GA. GeneticSearchConfig enforces population_size >= 10, so 10 is
# the smallest *valid* production config; 10 genomes × 3 generations = 30 candidates.
POPULATION_SIZE = 10
GENERATIONS = 3
EXPECTED_GENOMES = POPULATION_SIZE * GENERATIONS


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _sqlite_scope():
    """A fresh in-memory SQLite engine + a ``session_scope``-compatible factory."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return engine, factory, scope


@pytest.fixture
def smoke_lake(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    monkeypatch.delenv("DATA_LAKE_ROOT", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def smoke_db(monkeypatch):
    """One SQLite database wired at both the job layer and the latent seam."""
    engine, factory, scope = _sqlite_scope()
    # The job layer persists runs/candidates through this; the genetic provider
    # resolves the per-run latent universe through the same DB so a registered
    # PRODUCTION model is actually visible to the run.
    monkeypatch.setattr(strategy_search_jobs, "session_scope", scope)
    monkeypatch.setattr(genetic_search, "session_scope", scope)
    try:
        yield SimpleNamespace(engine=engine, factory=factory, scope=scope)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


# --------------------------------------------------------------------------- #
# Request / run helpers                                                        #
# --------------------------------------------------------------------------- #
def _smoke_request(
    *,
    init_seed: int = 1234,
    latents_enabled: bool = True,
    n_trials: int = 1,
    min_seed_signals: int = 1,
    prescreen_min_signals: int = 1,
) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": SMOKE_SYMBOL,
            "timeframe": SMOKE_TIMEFRAME,
            "start": SMOKE_START.isoformat(),
            "end": SMOKE_END.isoformat(),
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
            max_workers=1,
        ),
        study=StudyConfig(
            name="Discovery smoke",
            n_trials=n_trials,
            seed=42,
            storage={"type": "memory"},
        ),
        include_risk_search=False,
        gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
        latents_enabled=latents_enabled,
        genetic=GeneticSearchConfig(
            population_size=POPULATION_SIZE,
            generations=GENERATIONS,
            elite_count=1,
            init_seed=init_seed,
            crossover_rate=0.7,
            mutation_rate=0.2,
            tournament_size=2,
            max_nodes=12,
            max_depth=8,
            max_workers=1,
            min_seed_signals=min_seed_signals,
            prescreen_min_signals=prescreen_min_signals,
        ),
    )


def _run_discovery(request: StrategySearchConfig) -> str:
    """Start a run through the production job layer; returns after it completes."""
    return strategy_search_jobs.start_job(request).run_id


def _round(value):
    return round(value, 9) if isinstance(value, float) else value


def _leaderboard_key(payload: dict) -> list[tuple]:
    return [
        (
            candidate["candidate_id"],
            candidate["rank"],
            candidate["status"],
            _round(candidate["objective_value"]),
            _round(candidate["robustness_score"]),
        )
        for candidate in payload["candidates"]
    ]


def _assert_genomes_valid(payload: dict) -> None:
    for candidate in payload["candidates"]:
        genome = candidate.get("genome")
        assert genome is not None, "every discovery candidate persists its genome"
        validate_genome(Genome.model_validate(genome), max_depth=8, max_node_count=12)


def _assert_metrics_finite(payload: dict) -> None:
    for candidate in payload["candidates"]:
        for value in (candidate.get("oos_metrics") or {}).values():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                assert math.isfinite(value), f"non-finite metric: {value!r}"


def _genome_has_latent(genome: dict | None) -> bool:
    if not genome:
        return False
    return any(node.get("kind") == "ind.latent" for node in genome.get("nodes", []))


def _run_has_latent_genome(payload: dict) -> bool:
    return any(_genome_has_latent(c.get("genome")) for c in payload["candidates"])


def _population_has_latent(population) -> bool:
    return any(node.kind == "ind.latent" for genome in population for node in genome.nodes)


def _register_production_pca_model(smoke_db) -> None:
    """Train + promote a torch-free PCA encoder for the smoke instrument."""
    from q_backend.neural.promotion import promote_neural_model
    from q_backend.neural.training import default_train_encoder_config, train_encoder
    from q_backend.storage.db.models import NeuralModelStatus
    from q_backend.storage.db.repositories import set_neural_model_status

    features = tuple(f"feature_{index:02d}" for index in range(1, 7))
    config = default_train_encoder_config(
        kind="pca",
        symbol=SMOKE_SYMBOL,
        timeframe=SMOKE_TIMEFRAME,
        train_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        train_end=datetime(2024, 1, 10, tzinfo=timezone.utc),
        n_latents=4,
        input_features=features,
        model_key="smoke_latent",
    )
    rng = np.random.default_rng(7)
    index = pd.date_range(
        config.encoder_config.train_start,
        config.encoder_config.train_end,
        freq="h",
        tz="UTC",
    )
    frame = pd.DataFrame(
        rng.normal(size=(len(index), len(features))),
        index=index,
        columns=features,
    )
    with smoke_db.scope() as session:
        version = train_encoder(session, config, feature_window=frame)
        set_neural_model_status(
            session,
            model_hash=version.model_hash,
            status=NeuralModelStatus.CANDIDATE.value,
        )
        promote_neural_model(
            session,
            model_hash=version.model_hash,
            target_status=NeuralModelStatus.PRODUCTION.value,
        )


# --------------------------------------------------------------------------- #
# 1. Completion + determinism                                                  #
# --------------------------------------------------------------------------- #
def test_discovery_job_completes_and_is_deterministic(
    run_jobs_sync, smoke_db, smoke_lake
):
    run_a = _run_discovery(_smoke_request(init_seed=1234, latents_enabled=False))
    payload_a = strategy_search_jobs.results_payload_from_db(run_a)

    assert payload_a is not None
    assert payload_a["status"] == "completed"

    candidates = payload_a["candidates"]
    assert candidates, "leaderboard must be non-empty"
    _assert_genomes_valid(payload_a)
    _assert_metrics_finite(payload_a)

    summary = payload_a["summary"]
    assert summary["generations_completed"] == GENERATIONS
    assert summary["total_genomes_evaluated"] == EXPECTED_GENOMES
    # Every candidate is accounted for with a terminal status; the summary tallies
    # gate/failure counts. (Random genomes on the smooth synthetic series may not
    # trade, so a *passing* champion isn't guaranteed — hence no rank>=1 assertion;
    # sanity here is structural + deterministic, per the WO's "leaderboard
    # non-empty, every genome validates, metrics finite".)
    assert len(candidates) == EXPECTED_GENOMES
    assert "passed_gates_count" in summary
    assert "failed_candidate_count" in summary
    for candidate in candidates:
        assert candidate["status"] in {"completed", "no_result", "unsupported", "error"}

    # Same seed → identical leaderboard. The production discovery path evaluates
    # each candidate in its own worker message and merges via an order-independent
    # fan-in (results are re-ordered by the stashed population, not arrival order),
    # so it is deterministic and does not exercise genetic_parallel's process pool
    # (that path belongs to the in-process orchestrator only).
    run_b = _run_discovery(_smoke_request(init_seed=1234, latents_enabled=False))
    payload_b = strategy_search_jobs.results_payload_from_db(run_b)

    assert payload_b is not None
    assert _leaderboard_key(payload_a) == _leaderboard_key(payload_b)


# --------------------------------------------------------------------------- #
# 2. Latents seam (WO150–152 / WO153)                                          #
# --------------------------------------------------------------------------- #
def test_discovery_with_latents_enabled_uses_latent_nodes(
    run_jobs_sync, smoke_db, smoke_lake
):
    _register_production_pca_model(smoke_db)

    # Viability seeding/pre-screen is turned off for this scenario. WO182 FINDING:
    # the distributed candidate worker builds its SearchCandidate without the run's
    # latent_model_hash (genetic_search.search_candidate_for_genome is called in
    # strategy_search_jobs.run_genetic_candidate without it), so ind.latent nodes
    # evaluate to all-NaN during discovery (composite_strategy.py latent branch).
    # With viability ON (min_seed_signals>0) those signal-less latent genomes are
    # repaired away, so latents never reach the persisted population. Disabling
    # viability isolates the *seeding* seam this task targets ("seeds latent genome
    # nodes and completes"); the deeper NaN-evaluation gap is reported for WO153–157.
    request = _smoke_request(
        init_seed=1234,
        latents_enabled=True,
        min_seed_signals=0,
        prescreen_min_signals=0,
    )

    # Built exactly as production does: the provider resolves the per-run latent
    # universe from the registered PRODUCTION model and seeds ind.latent nodes.
    provider_on = create_genetic_candidate_provider(
        request.genetic, request, latents_enabled=True
    )
    assert "ind.latent" in provider_on._latent_universe.indicator_kinds
    assert provider_on._latent_universe.n_latents > 0
    assert _population_has_latent(provider_on.population), (
        "a PRODUCTION model must seed latent genome nodes"
    )

    run_id = _run_discovery(request)
    payload = strategy_search_jobs.results_payload_from_db(run_id)
    assert payload is not None
    assert payload["status"] == "completed"
    assert _run_has_latent_genome(payload), (
        "latent nodes must survive into the persisted population"
    )

    # WO153 seam: latents OFF → the universe never exposes ind.latent, so no latent
    # node can be seeded or mutated in, and the run still completes.
    off_request = _smoke_request(
        init_seed=1234,
        latents_enabled=False,
        min_seed_signals=0,
        prescreen_min_signals=0,
    )
    provider_off = create_genetic_candidate_provider(
        off_request.genetic, off_request, latents_enabled=False
    )
    assert "ind.latent" not in provider_off._latent_universe.indicator_kinds
    assert not _population_has_latent(provider_off.population)

    off_run = _run_discovery(off_request)
    off_payload = strategy_search_jobs.results_payload_from_db(off_run)
    assert off_payload is not None
    assert off_payload["status"] == "completed"
    assert not _run_has_latent_genome(off_payload)


# --------------------------------------------------------------------------- #
# 3. Cancellation mid-run                                                      #
# --------------------------------------------------------------------------- #
def test_cancellation_mid_run_leaves_consistent_state(
    run_jobs_sync, smoke_db, smoke_lake, monkeypatch
):
    from q_backend.tasks.fanin import set_cancelled

    threads_before = threading.active_count()

    # Trip the real cancellation flag (the same Redis flag request_cancel raises)
    # after the first candidate has been staged, i.e. genuinely mid-generation.
    original_stash = strategy_search_jobs.stash_partial
    state = {"count": 0}

    def cancel_after_first(run_id, index, payload):
        original_stash(run_id, index, payload)
        state["count"] += 1
        if state["count"] == 1:
            set_cancelled(run_id)

    monkeypatch.setattr(strategy_search_jobs, "stash_partial", cancel_after_first)

    run_id = _run_discovery(_smoke_request(init_seed=7, latents_enabled=False))

    status = strategy_search_jobs.get_status_payload(run_id)
    assert status is not None
    assert status["status"] == "cancelled"

    with smoke_db.scope() as session:
        run = get_strategy_search_run(session, uuid.UUID(hex=run_id))
        assert run is not None
        assert run.status == "cancelled"
        assert run.error_message

    # No zombie threads left behind by the cancelled run.
    assert threading.active_count() <= threads_before

    # Partial results are internally consistent: whatever was persisted validates.
    results = strategy_search_jobs.results_payload_from_db(run_id)
    if results is not None:
        _assert_genomes_valid(results)
        _assert_metrics_finite(results)


# --------------------------------------------------------------------------- #
# 4. Orphan reconciliation across every job family wired in lifespan.py        #
# --------------------------------------------------------------------------- #
# lifespan.py reconciles seven *_jobs families on startup. Four are DB-backed
# (mark_active_runs_cancelled flips the run row to "cancelled"); three are
# Redis-progress-backed (a running/queued progress payload is flipped to
# "failed"). The shared contract asserted here: an orphaned *running* job is,
# after reconcile, no longer "running" and the reconciler reports it.
_DB_FAMILIES = {
    "optimization_jobs": (
        optimization_jobs,
        lambda s: create_optimization_study(s, name="orphan", config={}, status="running"),
    ),
    "walkforward_jobs": (
        walkforward_jobs,
        lambda s: create_walkforward_run(s, name="orphan", config={}, status="running"),
    ),
    "strategy_search_jobs": (
        strategy_search_jobs,
        lambda s: create_strategy_search_run(s, name="orphan", config={}, status="running"),
    ),
    "backtest_jobs": (
        backtest_jobs,
        lambda s: create_backtest_run(
            s, backtest_config_id=uuid.uuid4(), config={}, status="running"
        ),
    ),
}
_REDIS_FAMILIES = {
    "encoder_ablation_jobs": encoder_ablation_jobs,
    "discovery_ab_jobs": discovery_ab_jobs,
    "alpha_research_jobs": alpha_research_jobs,
}


@pytest.mark.parametrize(
    "family", list(_DB_FAMILIES) + list(_REDIS_FAMILIES)
)
def test_orphaned_run_reconciled_on_startup(
    run_jobs_sync, monkeypatch, tmp_path, family
):
    # alpha_research reconcile reads a lake checkpoint; a fresh empty lake makes
    # the "no checkpoint" (orphaned) branch deterministic.
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    monkeypatch.delenv("DATA_LAKE_ROOT", raising=False)
    get_settings.cache_clear()

    if family in _DB_FAMILIES:
        module, seeder = _DB_FAMILIES[family]
        engine, _factory, scope = _sqlite_scope()
        monkeypatch.setattr(module, "session_scope", scope)
        try:
            with scope() as session:
                row = seeder(session)
                row_type = type(row)
                row_id = row.id

            count = module.reconcile_orphaned_runs()
            assert count >= 1

            with scope() as session:
                refreshed = session.get(row_type, row_id)
                assert refreshed is not None
                assert refreshed.status == "cancelled"
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()
    else:
        module = _REDIS_FAMILIES[family]
        fake = run_jobs_sync  # the shared fakeredis instance
        job_id = uuid.uuid4().hex
        set_job_progress(
            fake,
            job_id,
            {"job_id": job_id, "status": "running"},
            namespace=module.PROGRESS_NAMESPACE,
        )

        count = module.reconcile_orphaned_runs()
        assert count >= 1

        payload = get_job_progress(fake, job_id, namespace=module.PROGRESS_NAMESPACE)
        assert payload is not None
        assert payload["status"] != "running"

    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 5. Failed-candidate accounting                                              #
# --------------------------------------------------------------------------- #
def test_failed_candidates_are_accounted_for(
    run_jobs_sync, smoke_db, smoke_lake, monkeypatch
):
    # Fail the per-candidate data load for half the candidates at the same seam a
    # candidate worker loads its frame; the worker isolates each failure into an
    # error CandidateResult and the finalizer tallies them into the result summary.
    original = strategy_search_jobs._candidate_ohlcv
    state = {"count": 0}

    def flaky_candidate_ohlcv(request):
        state["count"] += 1
        if state["count"] % 2 == 0:
            raise RuntimeError("synthetic data-provider outage")
        return original(request)

    monkeypatch.setattr(strategy_search_jobs, "_candidate_ohlcv", flaky_candidate_ohlcv)

    run_id = _run_discovery(_smoke_request(init_seed=21, latents_enabled=False))
    payload = strategy_search_jobs.results_payload_from_db(run_id)

    assert payload is not None
    assert payload["status"] == "completed"

    summary = payload["summary"]
    assert summary["failed_candidate_count"] > 0
    assert summary["failure_reasons"]
    assert any(
        "synthetic data-provider outage" in reason
        for reason in summary["failure_reasons"]
    )
