import json
import logging
import os
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DataSource = Literal["auto", "mt5", "local"]
_VALID_SOURCES: frozenset[str] = frozenset({"auto", "mt5", "local"})
_DEFAULT_SOURCE: DataSource = "auto"


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
    except Exception as exc:
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
        raise ValueError(
            f"Invalid data_source '{value}'. Choose from: {sorted(_VALID_SOURCES)}"
        )
    config = _read_config()
    config["data_source"] = normalized
    _write_config(config)
