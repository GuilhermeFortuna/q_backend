import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from q_backend.api import backtest_jobs
from q_backend.api import discovery_ab_jobs
from q_backend.api import encoder_ablation_jobs
from q_backend.api import alpha_research_jobs
from q_backend.api import optimization_jobs
from q_backend.api import strategy_search_jobs
from q_backend.api import walkforward_jobs
from q_backend.api.dependencies import market_data_service
from q_backend.features.evaluation_service import reconcile_orphaned_eval_runs
from q_backend.features.sync import sync_registry_to_db
from q_backend.neural.sync import sync_neural_models_to_db
from q_backend.observability.sentry import init_sentry
from q_backend.storage.db.engine import session_scope
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production trigger: API process startup, before any fallible startup work.
    init_sentry(get_settings(), component="api")
    # Startup: Connect to MetaTrader 5
    logger.info("Starting up API, initializing market data providers...")
    success = market_data_service.initialize()
    if not success:
        logger.warning(
            "MetaTrader 5 terminal initialization failed on startup; "
            "auto mode will use the local provider."
        )
    else:
        logger.info("MetaTrader 5 terminal initialized successfully on startup.")
    # Reconcile orphaned runs: any job left pending/running in the DB by a
    # previous process has no live worker and would otherwise stay "running"
    # forever, so mark it cancelled.
    optimization_jobs.reconcile_orphaned_runs()
    walkforward_jobs.reconcile_orphaned_runs()
    strategy_search_jobs.reconcile_orphaned_runs()
    discovery_ab_jobs.reconcile_orphaned_runs()
    backtest_jobs.reconcile_orphaned_runs()
    encoder_ablation_jobs.reconcile_orphaned_runs()
    alpha_research_jobs.reconcile_orphaned_runs()
    reconcile_orphaned_eval_runs()
    # Seed the Feature Store from the in-code FeatureSpec registry (WO130 sync).
    # Idempotent and one-way: refreshes recipe metadata but never downgrades a
    # human-promoted status, so it is safe to run on every boot.
    try:
        with session_scope() as session:
            sync_registry_to_db(session)
        logger.info("Feature registry synced to DB on startup.")
    except Exception:
        logger.exception("Feature registry sync failed on startup.")
    try:
        with session_scope() as session:
            sync_neural_models_to_db(session)
        logger.info("Neural model registry synced to DB on startup.")
    except Exception:
        logger.exception("Neural model registry sync failed on startup.")
    yield
    # Shutdown: Disconnect from MetaTrader 5
    logger.info("Shutting down API, disconnecting from MetaTrader 5...")
    market_data_service.shutdown()
