from q_backend.storage.settings import Settings, get_settings


def test_settings_defaults():
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://q:q@localhost:5434/q"
    assert settings.redis_url == "redis://localhost:6380/0"
    assert settings.data_lake_root == "data/lake"


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("Q_DATABASE_URL", "postgresql+psycopg://test:test@db:5432/test")
    monkeypatch.setenv("Q_REDIS_URL", "redis://redis:6379/1")
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", "/lake/root")
    get_settings.cache_clear()
    try:
        settings = Settings()
        assert settings.database_url == "postgresql+psycopg://test:test@db:5432/test"
        assert settings.redis_url == "redis://redis:6379/1"
        assert settings.data_lake_root == "/lake/root"
    finally:
        get_settings.cache_clear()
