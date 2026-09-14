"""Arrow IPC conversion using the vendored stream payload declarations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pyarrow as pa

import q_contracts


def _schema(name: str) -> pa.Schema:
    path = Path(q_contracts.__file__).parent / "schema/api/arrow" / f"{name}.schema.json"
    if not path.is_file():
        path = Path(__file__).resolve().parents[4] / "contracts/schema/api/arrow" / f"{name}.schema.json"
    declaration = json.loads(path.read_text(encoding="utf-8"))
    type_map = {
        "float64": pa.float64(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "timestamp[ms]": pa.timestamp("ms"),
        "timestamp[us]": pa.timestamp("us"),
    }
    return pa.schema(
        [
            pa.field(field["name"], type_map[field["type"]], nullable=field["nullable"])
            for field in declaration["fields"]
        ]
    )


TICKS_SCHEMA = _schema("ticks")
BARS_SCHEMA = _schema("bars")


def _to_ipc(columns: Mapping[str, np.ndarray], schema: pa.Schema) -> bytes:
    arrays = [pa.array(columns[field.name], type=field.type) for field in schema]
    batch = pa.record_batch(arrays, schema=schema)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_batch(batch)
    return sink.getvalue().to_pybytes()


def ticks_to_ipc(columns: Mapping[str, np.ndarray]) -> bytes:
    return _to_ipc(columns, TICKS_SCHEMA)


def bars_to_ipc(columns: Mapping[str, np.ndarray]) -> bytes:
    return _to_ipc(columns, BARS_SCHEMA)
