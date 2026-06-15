from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="Q_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://q:q@localhost:5432/q"
    redis_url: str = "redis://localhost:6380/0"
    data_lake_root: str = "data/lake"
    port: int = 8000
    # Number of OS processes the Dramatiq worker pool runs. This is the single
    # backend-wide CPU budget shared by every heavy job (backtests, optimization
    # trials, walk-forward windows, discovery candidates). Defaults to 28 of the
    # dev machine's 32 hardware threads, leaving headroom for the API process, the
    # MT5 data thread, and the OS. Override with Q_WORKER_PROCESSES in .env.
    worker_processes: int = 28


@lru_cache
def get_settings() -> Settings:
    return Settings()
