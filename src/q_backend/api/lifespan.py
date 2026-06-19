import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from q_backend.api import backtest_jobs
from q_backend.api import optimization_jobs
from q_backend.api import strategy_search_jobs
from q_backend.api import walkforward_jobs
from q_backend.api.dependencies import market_data_service

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    backtest_jobs.reconcile_orphaned_runs()
    yield
    # Shutdown: Disconnect from MetaTrader 5
    logger.info("Shutting down API, disconnecting from MetaTrader 5...")
    market_data_service.shutdown()
