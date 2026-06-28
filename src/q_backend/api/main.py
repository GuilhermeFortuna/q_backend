import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from q_backend.api.lifespan import lifespan
from q_backend.api.routers import backtest as backtest_router
from q_backend.api.routers import experiments as experiments_router
from q_backend.api.routers import features as features_router
from q_backend.api.routers import market as market_router
from q_backend.api.routers import neural as neural_router
from q_backend.api.routers import news as news_router
from q_backend.api.routers import optimization as optimization_router
from q_backend.api.routers import storage as storage_router
from q_backend.api.routers import strategies as strategies_router
from q_backend.api.routers import strategy_builder as strategy_builder_router
from q_backend.api.routers import strategy_search as strategy_search_router
from q_backend.api.routers import system as system_router
from q_backend.api.routers import walkforward as walkforward_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

app = FastAPI(
    title="QuantLauncher API Backend",
    description="Backend API for fetching market data and executing orders using MetaTrader 5",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system_router.router)
app.include_router(strategies_router.router)
app.include_router(strategy_builder_router.router)
app.include_router(market_router.router)
app.include_router(backtest_router.router)
app.include_router(optimization_router.router)
app.include_router(walkforward_router.router)
app.include_router(strategy_search_router.router)
app.include_router(features_router.router)
app.include_router(neural_router.router)
app.include_router(experiments_router.router)
app.include_router(storage_router.router)
app.include_router(news_router.router)


def run_dev():
    """Entry point for running the dev server via `uv run dev`."""
    import os

    import uvicorn

    from q_backend.storage.settings import get_settings

    port_env = os.environ.get("PORT")
    if port_env:
        port = int(port_env)
    else:
        try:
            port = get_settings().port
        except Exception:
            port = 8000

    uvicorn.run(
        "q_backend.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        reload_excludes=["data", ".venv", "**/__pycache__"],
    )
