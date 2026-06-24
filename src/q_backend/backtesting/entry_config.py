from __future__ import annotations

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.entry_models import EntryInstance, EntryManagerConfig
from q_backend.backtesting.exit_strategy import get_exit_strategy_params


def normalize_entries(
    request: BacktestRequest,
) -> tuple[list[EntryInstance], EntryManagerConfig, dict]:
    if request.entries is not None:
        return (
            list(request.entries),
            request.entry_manager,
            dict(request.exit_params),
        )

    exit_param_names = {spec.name for spec in get_exit_strategy_params()}
    entry_params: dict = {}
    exit_params: dict = {}
    for key, value in request.strategy_params.items():
        if key in exit_param_names:
            exit_params[key] = value
        else:
            entry_params[key] = value

    return (
        [EntryInstance(strategy=request.strategy, params=entry_params)],
        EntryManagerConfig(kind="or"),
        exit_params,
    )


def format_entry_strategy_label(
    entries: list[EntryInstance],
    manager: EntryManagerConfig,
) -> str:
    if len(entries) == 1:
        base = entries[0].strategy
    else:
        base = "+".join(entry.strategy for entry in entries)
    return f"{base} ({manager.kind})"
