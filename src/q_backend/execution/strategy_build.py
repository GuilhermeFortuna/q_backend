"""Build live evaluation strategies from immutable compiled deployment config."""

from __future__ import annotations

from typing import Any

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.entry_config import normalize_entries
from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import get_registered_strategy


class UnsupportedForwardStrategyError(ValueError):
    """Raised when a saved configuration cannot run on the forward evaluator."""


def build_strategy_from_compiled(
    compiled_config: dict[str, Any],
    *,
    symbol: str,
) -> TradingStrategy:
    """Hydrate a ``TradingStrategy`` through the existing registry/factory."""
    try:
        request = BacktestRequest.model_validate(compiled_config)
    except Exception as exc:
        raise UnsupportedForwardStrategyError("compiled_config is not a valid candle backtest configuration") from exc

    if request.engine == "tick":
        raise UnsupportedForwardStrategyError("tick/sub-second strategies are outside forward execution scope")

    entries, manager, exit_params = normalize_entries(request)
    if request.ml_filter is not None or any(
        "research_only" in get_registered_strategy(entry.strategy).info.capabilities for entry in entries
    ):
        raise UnsupportedForwardStrategyError(
            "ML-filter strategies are research-only and cannot run on forward execution"
        )
    if request.entries is not None:
        return build_composite_entry(
            [{"strategy": entry.strategy, "params": entry.params} for entry in entries],
            manager.kind,
            manager.params,
            exit_params,
            symbol,
        )

    entry_params = {key: value for key, value in entries[0].params.items()}
    merged_params = {**entry_params, **exit_params}
    return build_strategy(request.strategy, merged_params, symbol)


def build_research_strategy_from_compiled(
    compiled_config: dict[str, Any],
    *,
    bundle_root: Any = None,
    session: Any = None,
) -> Any:
    """Hydrate a frozen ``ResearchStrategy`` from its version bundle and parameters."""
    version_id = compiled_config.get("strategy_version_id")
    if not version_id:
        raise UnsupportedForwardStrategyError("compiled_config missing strategy_version_id for strategy_version source")

    import uuid
    from q_backend.research.params import coerce_params, declared_from_wire
    from q_backend.research.strategy_files import default_bundle_root, load_bundle
    from q_backend.research.strategy_versions import bundle_of
    from q_backend.storage.db.engine import session_scope
    from q_backend.storage.db.models import ResearchStrategyVersion

    def _load(s):
        version = s.get(ResearchStrategyVersion, uuid.UUID(str(version_id)))
        if version is None:
            raise UnsupportedForwardStrategyError(f"ResearchStrategyVersion '{version_id}' not found")
        bundle = bundle_of(version)
        cls = load_bundle(bundle, bundle_root or default_bundle_root())
        declared = tuple(declared_from_wire(spec) for spec in version.param_specs)
        params = coerce_params(declared, compiled_config.get("strategy_params", {}))
        return cls(**params)

    if session is not None:
        return _load(session)
    with session_scope() as s:
        return _load(s)
