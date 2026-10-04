from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

FEATURE_ALLOWLIST = (
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "real_volume",
    "ma_short",
    "ma_long",
    "delta",
    "prev_delta",
    "side",
)

_ALIASES = {
    "volume": "tick_volume",
    "e0_ma_short": "ma_short",
    "e0_ma_long": "ma_long",
    "e0_delta": "delta",
    "e0_prev_delta": "prev_delta",
}
_ALIASES.update(
    {
        "e0__ma_short": "ma_short",
        "e0__ma_long": "ma_long",
        "e0__delta": "delta",
        "e0__prev_delta": "prev_delta",
    }
)


def _source_column(frame: pd.DataFrame, name: str) -> str | None:
    if name in frame.columns:
        return name
    for alias, canonical in _ALIASES.items():
        if canonical == name and alias in frame.columns:
            return alias
    if name == "tick_volume" and "volume" in frame.columns:
        return "volume"
    return None


def build_entry_features(
    frame: pd.DataFrame,
    sides: Sequence[int],
    feature_names: Sequence[str],
) -> pd.DataFrame:
    """Build ordered signal-bar features for candidate entries.

    ``frame`` is already aligned to signal bars. This function never shifts,
    resamples, or consults a following entry bar.
    """
    if len(frame) != len(sides):
        raise ValueError("frame and sides must have the same number of rows")
    names = list(feature_names)
    if len(names) != len(set(names)):
        raise ValueError("feature_names must not contain duplicates")
    unknown = [name for name in names if name not in FEATURE_ALLOWLIST]
    if unknown:
        raise ValueError(f"Unsupported entry feature(s): {', '.join(unknown)}")
    if "side" not in names:
        raise ValueError("side is a required entry feature")
    if len(names) < 2:
        raise ValueError("select at least one market feature in addition to side")

    columns: dict[str, pd.Series] = {}
    for name in names:
        if name == "side":
            side_values = pd.Series(sides, index=frame.index, dtype="int8")
            if not side_values.isin([-1, 1]).all():
                raise ValueError("sides must contain only -1 or 1")
            columns[name] = side_values
            continue
        source = _source_column(frame, name)
        if source is None:
            raise ValueError(f"Selected feature '{name}' is missing from source bars")
        columns[name] = pd.to_numeric(frame[source], errors="coerce")

    result = pd.DataFrame(columns, index=frame.index)
    return result.loc[:, names]
