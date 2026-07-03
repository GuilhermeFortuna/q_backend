"""Shared test harness for the Dramatiq-dispatched job managers.

Heavy jobs now run on the worker pool: ``start_job`` persists state and enqueues an
actor message, and a chain of coordinator → leaf → finalizer actors does the work.
These fixtures let tests exercise that chain in-process, without a broker, real Redis,
or MetaTrader 5:

* ``run_jobs_sync`` rewires every actor ``.send`` to call its orchestration function
  synchronously, routes all Redis access to one ``fakeredis`` instance, points the
  distributed Optuna study at a temp SQLite file (so trial workers and the finalizer
  share it without Postgres), and feeds synthetic OHLCV into the workers.

After ``start_job`` returns under this fixture, the whole job has already run and its
terminal state is persisted — assert via ``get_status_payload`` / ``results_payload``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

try:  # fakeredis is a dev dependency; only needed by the harness
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None


@pytest.fixture(autouse=True)
def _isolate_remote_gateway_env(monkeypatch):
    """Keep a developer's live MT5 gateway out of the test suite.

    MarketDataService loads the repo .env into os.environ, so a configured
    Q_MT5_GATEWAY_URL (with the gateway actually running) would make the remote
    provider reachable and flip routing/availability assertions. load_env() is
    no-oped too, since every MarketDataService() re-reads .env into os.environ.
    Gateway tests that need a URL set it explicitly after this runs.
    """
    monkeypatch.setattr("q_backend.market_data.service.load_env", lambda: None)
    monkeypatch.delenv("Q_MT5_GATEWAY_URL", raising=False)
    monkeypatch.delenv("Q_MT5_GATEWAY_TOKEN", raising=False)

    # The api.dependencies singleton was built at import time, when .env may
    # already have injected the URL — its remote client baked it in. Swap in a
    # fresh, unconfigured client (constructed after the delenv above).
    from q_backend.api import dependencies
    from q_backend.market_data.clients.remote import RemoteMt5Client

    monkeypatch.setattr(
        dependencies.market_data_service, "_remote_client", RemoteMt5Client()
    )


def pytest_addoption(parser):
    """Register the golden-regeneration flag (see tests/backtesting/test_goldens.py).

    Regenerating a golden is a deliberate act: the resulting file diff shows up in
    git and must be justified in the commit/WO message that regenerates it.
    """
    parser.addoption(
        "--regen-goldens",
        action="store_true",
        default=False,
        help="Rewrite backtest golden files from current engine output instead of "
        "comparing against the committed goldens.",
    )


@pytest.fixture
def regen_goldens(request) -> bool:
    return bool(request.config.getoption("--regen-goldens"))


def _synthetic_ohlcv(symbol, timeframe, start, end) -> pd.DataFrame:
    """A deterministic daily OHLCV frame spanning [start, end].

    The close oscillates so short/long moving-average crossovers — and therefore
    trades — occur reliably in every window, keeping walk-forward/discovery
    assertions stable.
    """
    idx = pd.date_range(start=start, end=end, freq="D")
    if len(idx) == 0:
        idx = pd.date_range(start=start, periods=2, freq="D")
    n = len(idx)
    steps = np.arange(n)
    close = 100 + 10 * np.sin(steps / 2.0) + 0.05 * steps
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1000),
        },
        index=idx,
    )


@pytest.fixture
def synthetic_ohlcv():
    return _synthetic_ohlcv


@pytest.fixture(autouse=True)
def _restore_feature_catalog():
    """Isolate the process-global feature catalog across tests.

    ``register_neural_model_features`` mutates the module-global ``FEATURE_SPECS``;
    snapshot it before each test and restore after, so a leaked neural spec can't
    cascade into catalog-count/iteration assertions in later tests.
    """
    from q_backend.features.registry import FEATURE_SPECS

    snapshot = dict(FEATURE_SPECS)
    try:
        yield
    finally:
        FEATURE_SPECS.clear()
        FEATURE_SPECS.update(snapshot)


@pytest.fixture
def run_jobs_sync(monkeypatch, tmp_path):
    if fakeredis is None:  # pragma: no cover
        pytest.skip("fakeredis is required for the synchronous job harness")

    fake = fakeredis.FakeRedis(decode_responses=True)

    from q_backend.api import backtest_jobs as bj
    from q_backend.api import discovery_ab_jobs as dab
    from q_backend.api import encoder_ablation_jobs as eaj
    from q_backend.api import alpha_research_jobs as arj
    from q_backend.api import neural_jobs as nj
    from q_backend.api import optimization_jobs as oj
    from q_backend.api import strategy_search_jobs as sj
    from q_backend.api import walkforward_jobs as wj
    from q_backend.optimization.models import StorageConfig
    from q_backend.tasks import actors, fanin, genetic_staging, staging

    # Route every Redis user (fan-in counters, partial staging, progress mirrors,
    # cross-generation genetic state) at one in-memory fakeredis.
    for module in (fanin, staging, genetic_staging, oj, wj, sj, bj, nj, eaj, dab, arj):
        if hasattr(module, "get_redis"):
            monkeypatch.setattr(module, "get_redis", lambda: fake, raising=False)

    # Feed synthetic market data to the window/candidate workers.
    monkeypatch.setattr("q_backend.tasks.data.load_ohlcv_frame", _synthetic_ohlcv)
    for module in (wj, sj):
        if hasattr(module, "load_ohlcv_frame"):
            monkeypatch.setattr(
                module, "load_ohlcv_frame", _synthetic_ohlcv, raising=False
            )

    # Distributed Optuna needs storage that persists across the trial workers and the
    # finalizer; a temp SQLite file gives that without Postgres.
    def _sqlite_config(config, study_id):
        worker_config = config.model_copy(deep=True)
        worker_config.study.storage = StorageConfig(
            type="sqlite", path=str(tmp_path / f"{study_id}.db")
        )
        worker_config.study.name = f"opt-{study_id}"
        return worker_config

    monkeypatch.setattr(oj, "_distributed_config", _sqlite_config)

    # Optimization trial workers build their backtest runner from cached data; feed
    # them the synthetic frame too.
    from q_backend.optimization.backtest_runner import DefaultBacktestRunner

    monkeypatch.setattr(
        oj,
        "_build_worker_runner",
        lambda config: (
            DefaultBacktestRunner.from_frame_sliced(
                _synthetic_ohlcv(
                    config.backtest.symbol,
                    config.backtest.timeframe,
                    config.backtest.start,
                    config.backtest.end,
                )
            ),
            _synthetic_ohlcv(
                config.backtest.symbol,
                config.backtest.timeframe,
                config.backtest.start,
                config.backtest.end,
            ),
        ),
    )

    class _SyncActor:
        def __init__(self, fn):
            self._fn = fn

        def send(self, *args, **kwargs):
            return self._fn(*args, **kwargs)

    monkeypatch.setattr(
        actors, "optimization_coordinator", _SyncActor(oj.dispatch_study)
    )
    monkeypatch.setattr(
        actors, "run_optimization_trials", _SyncActor(oj.run_trials_chunk)
    )
    monkeypatch.setattr(
        actors, "walkforward_coordinator", _SyncActor(wj.dispatch_windows)
    )
    monkeypatch.setattr(actors, "run_walkforward_window", _SyncActor(wj.run_window))
    monkeypatch.setattr(
        actors, "discovery_coordinator", _SyncActor(sj.dispatch_candidates)
    )
    monkeypatch.setattr(
        actors, "evaluate_discovery_candidate", _SyncActor(sj.run_candidate)
    )
    monkeypatch.setattr(
        actors, "evaluate_genetic_candidate", _SyncActor(sj.run_genetic_candidate)
    )
    monkeypatch.setattr(actors, "run_backtest", _SyncActor(bj.run_backtest_job))
    monkeypatch.setattr(
        actors, "run_neural_training", _SyncActor(nj.run_training_job)
    )
    monkeypatch.setattr(
        actors, "run_encoder_ablation", _SyncActor(eaj.run_encoder_ablation_job)
    )
    monkeypatch.setattr(
        actors, "run_discovery_ab", _SyncActor(dab.run_discovery_ab_job)
    )
    monkeypatch.setattr(
        actors,
        "run_alpha_research",
        _SyncActor(lambda job_id, request_json, resume=False: arj.run_alpha_research_job(
            job_id, request_json, resume=resume
        )),
    )

    return fake
