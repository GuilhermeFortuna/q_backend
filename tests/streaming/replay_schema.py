"""Load Q-009 replay JSON Schemas from the vendored q_contracts package."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import referencing

import q_contracts

STREAM_DIR = Path(q_contracts.__file__).resolve().parent / "schema" / "stream"
REPLAY_DIR = STREAM_DIR / "replay"


def load_replay_validator(name: str) -> jsonschema.Draft202012Validator:
    schema_path = REPLAY_DIR / f"{name}.schema.json"
    schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

    resources = []
    for file in STREAM_DIR.rglob("*.schema.json"):
        schema = json.loads(file.read_text(encoding="utf-8"))
        resource = referencing.Resource.from_contents(schema)
        resources.append((file.name, resource))
        resources.append((file.as_posix(), resource))
        resources.append((f"../{file.name}", resource))
        if "$id" in schema:
            resources.append((schema["$id"], resource))
            resources.append((f"{schema['$id']}.schema.json", resource))
    registry = referencing.Registry().with_resources(resources)
    return jsonschema.Draft202012Validator(schema_data, registry=registry)


def assert_valid_replay(name: str, payload: dict) -> None:
    validator = load_replay_validator(name)
    errors = list(validator.iter_errors(payload))
    assert not errors, errors[0].message if errors else "validation failed"
