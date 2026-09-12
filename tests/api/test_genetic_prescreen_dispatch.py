"""Staged genetic dispatch: pre-screen synthesizes no_result + corrects the barrier.

These cover the wiring fix that activates WO53's trade-viability pre-screen in the
distributed discovery path (``_dispatch_generation``): dead genomes get a synthesized
``no_result`` partial and are *not* fanned out as walk-forward actors, the fan-in
counter is sized to the dispatched (alive) candidates only, and a wholly dead
generation finalizes instead of hanging on a barrier no actor will ever trip.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from q_backend.api import strategy_search_jobs as sj
from q_backend.backtesting.genome.activity import ActivityStats
from q_backend.backtesting.genome.exit_rule_policy import (
    attach_exit_rule_policy,
    preset_by_id,
)
from q_backend.optimization.genetic_search import GeneticCandidateProvider
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.strategy_search import (
    CandidateResult,
    GateConfig,
    GeneticSearchConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig

EXPECTED_EXIT_POLICY_META = {
    "exit_policy_id": "atr_stop_chandelier",
    "exit_policy_label": "ATR stop + Chandelier trail",
    "exit_param_names": [
        "exit_atr_period",
        "exit_chandelier_atr_mult",
        "exit_stop_loss_atr",
    ],
    "last_exit_mutation_op": "swap_exit_policy",
}


def _genetic_request(*, population_size: int) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "WIN$",
            "timeframe": "D1",
            "start": datetime(2024, 1, 1).isoformat(),
            "end": datetime(2024, 4, 30).isoformat(),
            "initial_capital": 10_000.0,
            "point_value": 1.0,
            "strategy": "CompositeStrategy",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=30, test_days=15, mode="rolling", min_windows=2),
        study=StudyConfig(name="Genetic WIN$ search", n_trials=2, seed=42, storage={"type": "memory"}),
        include_risk_search=False,
        gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
        genetic=GeneticSearchConfig(
            population_size=population_size,
            generations=3,
            elite_count=1,
            init_seed=7,
            prescreen_min_signals=1,
        ),
    )


def _capture_dispatch(monkeypatch, provider, *, dead_ids, capture_meta=False):
    """Stub every side effect of ``_dispatch_generation`` and return the captures."""
    captured: dict[str, object] = {"counter": None, "finalized": False}
    stashed: list[int] = []
    sent: list[int] = []
    stashed_meta: list[dict[str, dict]] = []

    def fake_activity(genome, df, *, min_signals, **_kwargs):
        firing = 0 if genome.genome_id in dead_ids else 5
        return ActivityStats(n_entries=firing, n_exits=firing, min_signals=min_signals)

    class _FakeActor:
        @staticmethod
        def send(run_id, db_run_id_hex, config_json, generation, index):
            sent.append(index)

    import q_backend.tasks.actors as actors_module

    monkeypatch.setattr(sj, "genome_signal_activity", fake_activity)
    monkeypatch.setattr(sj, "clear_partials", lambda *a, **k: None)
    monkeypatch.setattr(sj, "stash_partial", lambda run_id, index, payload: stashed.append(index))
    monkeypatch.setattr(sj, "init_counter", lambda run_id, n: captured.__setitem__("counter", n))
    monkeypatch.setattr(sj, "_persist_genetic_progress", lambda *a, **k: None)
    monkeypatch.setattr(
        sj,
        "finalize_generation",
        lambda *a, **k: captured.__setitem__("finalized", True),
    )
    monkeypatch.setattr(sj.genetic_staging, "set_provider_state", lambda *a, **k: None)
    if capture_meta:
        monkeypatch.setattr(
            sj.genetic_staging,
            "set_candidate_meta",
            lambda run_id, meta: stashed_meta.append(meta),
        )
    else:
        monkeypatch.setattr(sj.genetic_staging, "set_candidate_meta", lambda *a, **k: None)
    monkeypatch.setattr(actors_module, "evaluate_genetic_candidate", _FakeActor)

    # A non-empty probe frame switches the pre-screen branch on.
    provider._probe_df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    return captured, stashed, sent, stashed_meta


def _genome_with_exit_policy(provider: GeneticCandidateProvider, index: int = 0):
    preset = preset_by_id("atr_stop_chandelier")
    genome = attach_exit_rule_policy(provider._population[index], preset)
    genome.metadata["last_exit_mutation_op"] = "swap_exit_policy"
    provider._population[index] = genome
    return genome


def test_dispatch_prescreens_dead_genomes(monkeypatch):
    request = _genetic_request(population_size=12)
    provider = GeneticCandidateProvider(request.genetic, request)
    population = provider.population
    dead_ids = {population[0].genome_id, population[2].genome_id}

    captured, stashed, sent, _meta = _capture_dispatch(monkeypatch, provider, dead_ids=dead_ids)

    sj._dispatch_generation("run-1", "", request.model_dump_json(), provider, generation=0)

    alive_indices = [i for i, g in enumerate(population) if g.genome_id not in dead_ids]
    # Two dead genomes: partials staged for them, only the alive ones dispatched.
    assert sorted(stashed) == [0, 2]
    assert sorted(sent) == alive_indices
    # The fan-in barrier counts only the dispatched (alive) candidates.
    assert captured["counter"] == len(alive_indices) == 10
    # Some work is still pending, so we do NOT finalize early.
    assert captured["finalized"] is False


def test_dispatch_all_dead_generation_finalizes(monkeypatch):
    request = _genetic_request(population_size=10)
    provider = GeneticCandidateProvider(request.genetic, request)
    dead_ids = {genome.genome_id for genome in provider.population}

    captured, stashed, sent, _meta = _capture_dispatch(monkeypatch, provider, dead_ids=dead_ids)

    sj._dispatch_generation("run-2", "", request.model_dump_json(), provider, generation=0)

    # Whole generation pre-screened out: everyone staged, nobody dispatched, and the
    # barrier (counter 0) is resolved by finalizing directly rather than hanging.
    assert sorted(stashed) == list(range(10))
    assert sent == []
    assert captured["counter"] == 0
    assert captured["finalized"] is True


def test_dispatch_prescreen_stashes_exit_policy_metadata(monkeypatch):
    request = _genetic_request(population_size=10)
    provider = GeneticCandidateProvider(request.genetic, request)
    genome = _genome_with_exit_policy(provider, index=0)
    dead_ids = {genome.genome_id}

    _captured, _stashed, _sent, stashed_meta = _capture_dispatch(
        monkeypatch, provider, dead_ids=dead_ids, capture_meta=True
    )

    sj._dispatch_generation("run-exit-meta", "", request.model_dump_json(), provider, generation=0)

    assert len(stashed_meta) == 1
    meta = stashed_meta[0][genome.genome_id]
    for key, value in EXPECTED_EXIT_POLICY_META.items():
        assert meta[key] == value


def test_run_genetic_candidate_stashes_exit_policy_metadata(monkeypatch):
    request = _genetic_request(population_size=10)
    provider = GeneticCandidateProvider(request.genetic, request)
    genome = _genome_with_exit_policy(provider, index=1)
    stashed_meta: list[dict[str, dict]] = []

    monkeypatch.setattr(
        sj.genetic_staging,
        "get_generation_genome",
        lambda run_id, index: genome.model_dump(),
    )
    monkeypatch.setattr(sj, "is_cancelled", lambda run_id: False)
    monkeypatch.setattr(
        sj,
        "evaluate_candidate",
        lambda *args, **kwargs: CandidateResult(
            candidate_id=genome.genome_id,
            strategy="CompositeStrategy",
            status="no_result",
            error="stub",
        ),
    )
    monkeypatch.setattr(sj, "stash_partial", lambda *a, **k: None)
    monkeypatch.setattr(
        sj.genetic_staging,
        "set_candidate_meta",
        lambda run_id, meta: stashed_meta.append(meta),
    )
    monkeypatch.setattr(sj, "decrement", lambda run_id: 1)
    monkeypatch.setattr(sj, "_persist_genetic_progress", lambda *a, **k: None)

    sj.run_genetic_candidate("run-worker-meta", "", request.model_dump_json(), generation=0, candidate_index=1)

    assert len(stashed_meta) == 1
    meta = stashed_meta[0][genome.genome_id]
    for key, value in EXPECTED_EXIT_POLICY_META.items():
        assert meta[key] == value
