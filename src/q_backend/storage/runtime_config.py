import json
import logging
import os
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DataSource = Literal["auto", "mt5", "remote", "local"]
_VALID_SOURCES: frozenset[str] = frozenset({"auto", "mt5", "remote", "local"})
_DEFAULT_SOURCE: DataSource = "auto"

# Runtime-config keys + env overrides for the remote MT5 gateway (WO184).
_REMOTE_GATEWAY_URL_KEY = "remote_gateway_url"
_REMOTE_GATEWAY_URL_ENV = "Q_MT5_GATEWAY_URL"
_REMOTE_GATEWAY_TOKEN_KEY = "remote_gateway_token"
_REMOTE_GATEWAY_TOKEN_ENV = "Q_MT5_GATEWAY_TOKEN"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def runtime_config_path() -> Path:
    explicit = os.getenv("Q_RUNTIME_CONFIG_PATH")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = _project_root() / path
    else:
        path = _project_root() / "data" / "runtime_config.json"
    return path


def _read_config() -> dict:
    path = runtime_config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - best-effort config read; logged, returns {}
        logger.warning("Failed to read runtime config %s: %s", path, exc)
        return {}


def _write_config(data: dict) -> None:
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def get_data_source() -> DataSource:
    value = _read_config().get("data_source", _DEFAULT_SOURCE)
    if value not in _VALID_SOURCES:
        return _DEFAULT_SOURCE
    return value  # type: ignore[return-value]


def set_data_source(value: str) -> None:
    normalized = value.strip().lower()
    if normalized not in _VALID_SOURCES:
        raise ValueError(f"Invalid data_source '{value}'. Choose from: {sorted(_VALID_SOURCES)}")
    config = _read_config()
    config["data_source"] = normalized
    _write_config(config)


def _get_env_or_config(env_var: str, config_key: str) -> str | None:
    """Env value takes precedence over the JSON config; blank env is ignored."""
    env_value = os.getenv(env_var)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    value = _read_config().get(config_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_remote_gateway_url() -> str | None:
    """Resolve the remote MT5 gateway base URL.

    Precedence: ``Q_MT5_GATEWAY_URL`` env var, then the ``remote_gateway_url`` key in
    the runtime JSON config, else ``None`` (no gateway configured).
    """
    return _get_env_or_config(_REMOTE_GATEWAY_URL_ENV, _REMOTE_GATEWAY_URL_KEY)


def set_remote_gateway_url(value: str | None) -> None:
    """Persist (or clear, when ``None``/blank) the gateway URL in the JSON config."""
    config = _read_config()
    if value is None or not value.strip():
        config.pop(_REMOTE_GATEWAY_URL_KEY, None)
    else:
        config[_REMOTE_GATEWAY_URL_KEY] = value.strip()
    _write_config(config)


def get_remote_gateway_token() -> str | None:
    """Resolve the optional shared secret for the remote MT5 gateway.

    Precedence: ``Q_MT5_GATEWAY_TOKEN`` env var, then the ``remote_gateway_token`` key
    in the runtime JSON config, else ``None``.
    """
    return _get_env_or_config(_REMOTE_GATEWAY_TOKEN_ENV, _REMOTE_GATEWAY_TOKEN_KEY)


def set_remote_gateway_token(value: str | None) -> None:
    """Persist (or clear, when ``None``/blank) the gateway token in the JSON config."""
    config = _read_config()
    if value is None or not value.strip():
        config.pop(_REMOTE_GATEWAY_TOKEN_KEY, None)
    else:
        config[_REMOTE_GATEWAY_TOKEN_KEY] = value.strip()
    _write_config(config)
