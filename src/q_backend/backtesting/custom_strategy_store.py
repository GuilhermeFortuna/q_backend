import json
from pathlib import Path
from typing import Any

def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]

def custom_strategies_path() -> Path:
    return _project_root() / "data" / "custom_strategies.json"

def load_custom_strategies() -> list[dict[str, Any]]:
    path = custom_strategies_path()
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_custom_strategies(strategies: list[dict[str, Any]]) -> None:
    path = custom_strategies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(strategies, f, indent=2)
        f.write("\n")
