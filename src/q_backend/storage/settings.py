from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="Q_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://q:q@localhost:5434/q"
    redis_url: str = "redis://localhost:6380/0"
    data_lake_root: str = "data/lake"
    market_data_root: str = "data/market"
    port: int = 8000
    # Number of OS processes the Dramatiq worker pool runs. This is the single
    # backend-wide CPU budget shared by every heavy job (backtests, optimization
    # trials, walk-forward windows, discovery candidates). Defaults to 28 of the
    # dev machine's 32 hardware threads, leaving headroom for the API process, the
    # MT5 data thread, and the OS. Override with Q_WORKER_PROCESSES in .env.
    worker_processes: int = 28
    ai_strategy_enabled: bool = False
    ai_strategy_provider: str = "openai_compatible"
    ai_strategy_base_url: str = "http://localhost:11434/v1"
    ai_strategy_model: str = "qwen2.5-coder:14b"
    # Comma-separated allowlist: "model_id|Label,model_id2|Label2". The "|"
    # delimiter avoids colliding with the colon in Ollama "name:tag" model ids.
    ai_strategy_models: str = ""
    ai_strategy_api_key: str = ""
    ai_strategy_gemini_api_key: str = ""
    ai_strategy_gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # Comma-separated Gemini curated list (same "model_id|Label" format as
    # Q_AI_STRATEGY_MODELS). Defaults in code when unset.
    ai_strategy_gemini_models: str = ""
    ai_strategy_timeout_seconds: int = 60
    ai_strategy_max_output_tokens: int = 4096
    # Cap how much prior chat is embedded in the interpret user prompt. Local
    # Ollama models have tight context windows; unbounded history crowds out the
    # capability registry and the current StrategySpec draft.
    ai_strategy_max_conversation_turns: int = 12
    ai_strategy_conversation_char_budget: int = 8000
    sentry_dsn: str = ""
    sentry_environment: str = "local"
    sentry_traces_sample_rate: float = 0.2
    sentry_profiles_sample_rate: float = 0.1
    # Forward execution worker (standalone process — not Dramatiq, not API lifespan).
    execution_worker_id: str = "execution-worker-1"
    execution_lease_ttl_seconds: int = 30
    execution_poll_interval_seconds: float = 1.0
    execution_max_quote_age_seconds: float = 30.0
    execution_max_bar_age_seconds: float = 7200.0
    execution_paper_slippage_points: float = 0.0
    execution_paper_cost_per_contract: float = 0.0
    execution_paper_cost_bps: float = 0.0
    execution_default_point_value: float = 0.2
    execution_benchmark_p95_budget_ms: float = 500.0
    execution_initial_window_bars: int = 260
    execution_live_capability_locked: bool = True
    mt5_edge_url: str = "http://127.0.0.1:18813"
    mt5_edge_connect_timeout_s: float = 1.0
    mt5_edge_read_timeout_s: float = 5.0
    mt5_edge_submit_timeout_s: float = 15.0
    execution_lookup_window_lead_s: float = 60.0
    # MT5 live execution gates (WO172) — all default deny; capability stays live_locked.
    live_execution_enabled: bool = False
    live_execution_account_allowlist: str = ""
    live_execution_validated: bool = False
    live_execution_max_quote_age_seconds: float = 30.0
    live_execution_slippage_deviation: int = 20
    live_execution_dry_run: bool = True
    stream_symbols: str = ""
    stream_bar_timeframes: str = "M1"
    stream_tick_poll_interval_s: float = 0.25
    stream_bar_poll_interval_s: float = 1.0
    catalog_tombstone_grace_s: int = 604_800


@lru_cache
def get_settings() -> Settings:
    return Settings()
