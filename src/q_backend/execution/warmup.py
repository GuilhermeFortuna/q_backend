"""Warm-up / rolling-window bound calculation for forward strategy evaluation."""

from __future__ import annotations

from typing import Any

from q_backend.backtesting.entry_config import normalize_entries
from q_backend.backtesting.exit_rules.registry import enabled_rules
from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.optimization.walkforward import _WARMUP_MULTIPLIER, _WARMUP_PARAM_KEYS

# Small cushion so recursive indicators and exit columns settle on the evaluated bar.
_WINDOW_BUFFER_BARS = 5

# Exit params whose values are indicator lookbacks (not trade-duration limits).
_EXIT_LOOKBACK_KEYS = (
    "atr_period",
    "donchian_exit_period",
    "chandelier_atr_period",
    "parabolic_sar_period",
)


class WindowBoundUndeterminedError(ValueError):
    """Raised when a deployment's indicator window cannot be bounded safely."""


def _numeric_lookbacks_from_mapping(mapping: dict[str, Any]) -> list[int]:
    lookbacks: list[int] = []
    for key, value in mapping.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        normalized = key.split("__", 1)[-1].lower()
        if any(fragment in normalized for fragment in _WARMUP_PARAM_KEYS):
            lookbacks.append(int(value))
        if normalized in _EXIT_LOOKBACK_KEYS:
            lookbacks.append(int(value))
    return lookbacks


def _flatten_compiled_params(compiled_config: dict[str, Any]) -> dict[str, Any]:
    """Merge entry, exit, and legacy single-strategy params into one mapping."""
    try:
        request = BacktestRequest.model_validate(compiled_config)
    except Exception as exc:
        raise WindowBoundUndeterminedError(
            "compiled_config is not a valid backtest configuration"
        ) from exc

    if request.engine == "tick":
        raise WindowBoundUndeterminedError(
            "tick/sub-second strategies are outside forward execution scope"
        )

    entries, _manager, exit_params = normalize_entries(request)
    flat: dict[str, Any] = dict(exit_params)
    for entry in entries:
        flat.update(entry.params)
    if request.entries is None:
        flat.update(request.strategy_params)
    return flat


def compute_window_bound_bars(compiled_config: dict[str, Any]) -> int:
    """Return the maximum rolling window size required for steady-state evaluation.

    The bound is derived from strategy period-like parameters and enabled exit-rule
    indicator lookbacks. Deployments whose bound cannot be determined are rejected
    rather than loading unbounded history.

    Formula (documented for WO170):
    ``bound = max(longest_period_like_param) * 3 + 5``
    where period-like keys match ``period``, ``lookback``, or ``window`` (same rule
    as walk-forward warm-up), plus explicit exit indicator periods such as
    ``atr_period`` and ``donchian_exit_period``.
    """
    flat = _flatten_compiled_params(compiled_config)
    lookbacks = _numeric_lookbacks_from_mapping(flat)

    for rule in enabled_rules(flat):
        for col in rule.required_columns(flat):
            if col.startswith("atr_"):
                lookbacks.append(int(col.split("_", 1)[1]))
            elif col.startswith("donchian_high_") or col.startswith("donchian_low_"):
                prefix = (
                    "donchian_high_"
                    if col.startswith("donchian_high_")
                    else "donchian_low_"
                )
                lookbacks.append(int(col.removeprefix(prefix)))

    if not lookbacks:
        raise WindowBoundUndeterminedError(
            "no period/lookback parameters found in compiled_config; "
            "cannot bound indicator history safely"
        )

    longest = max(lookbacks)
    if longest <= 0:
        raise WindowBoundUndeterminedError(
            "longest lookback is non-positive; cannot bound indicator history"
        )

    return longest * _WARMUP_MULTIPLIER + _WINDOW_BUFFER_BARS
