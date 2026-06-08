from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="Q_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://q:q@localhost:5432/q"
    redis_url: str = "redis://localhost:6380/0"
    data_lake_root: str = "data/lake"


@lru_cache
def get_settings() -> Settings:
    return Settings()
