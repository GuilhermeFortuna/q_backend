"""Validation helpers for execution API deployment creation."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy.orm import Session

from q_backend.backtesting.strategy_registry import get_registered_strategy
from q_backend.execution.domain import BrokerMode, StrategyIdentity
from q_backend.market_data.exogenous_context import _TF_MINUTES
from q_backend.storage.db.repositories import get_backtest_run

_SUPPORTED_TIMEFRAMES = frozenset(_TF_MINUTES)
_MIN_TIMEFRAME_MINUTES = 15
_MAX_DAILY_LOSS = Decimal("10000000")
_MAX_NOTIONAL = Decimal("1000000000")


class ExecutionValidationError(ValueError):
    """Raised when deployment or account input fails API validation."""


def compute_config_hash(compiled_config: dict[str, Any]) -> str:
    payload = json.dumps(compiled_config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_positive_decimal(value: Any, *, field: str, max_value: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ExecutionValidationError(f"{field} must be a decimal") from exc
    if parsed <= 0:
        raise ExecutionValidationError(f"{field} must be positive")
    if parsed > max_value:
        raise ExecutionValidationError(f"{field} exceeds allowed maximum")
    return parsed


def validate_timeframe(timeframe: str) -> str:
    key = timeframe.upper()
    if key not in _SUPPORTED_TIMEFRAMES:
        raise ExecutionValidationError(f"unsupported timeframe '{timeframe}'")
    if _TF_MINUTES[key] < _MIN_TIMEFRAME_MINUTES:
        raise ExecutionValidationError(
            f"timeframe '{timeframe}' is below the M15 minimum for forward execution"
        )
    return key


def validate_risk_config(risk_config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not risk_config:
        return {}
    validated: dict[str, Any] = {}
    if "max_daily_loss" in risk_config and risk_config["max_daily_loss"] is not None:
        amount = _parse_positive_decimal(
            risk_config["max_daily_loss"],
            field="max_daily_loss",
            max_value=_MAX_DAILY_LOSS,
        )
        validated["max_daily_loss"] = str(amount)
    if "max_notional" in risk_config and risk_config["max_notional"] is not None:
        amount = _parse_positive_decimal(
            risk_config["max_notional"],
            field="max_notional",
            max_value=_MAX_NOTIONAL,
        )
        validated["max_notional"] = str(amount)
    return validated


def validate_sizing_config(sizing_config: dict[str, Any]) -> dict[str, Any]:
    if not sizing_config:
        raise ExecutionValidationError("sizing_config is required")
    quantity = sizing_config.get("quantity")
    if quantity is None:
        raise ExecutionValidationError("sizing_config.quantity is required")
    _parse_positive_decimal(quantity, field="quantity", max_value=Decimal("1000000"))
    return sizing_config


def validate_broker_mode(
    broker_mode: str,
    *,
    live_capability_locked: bool,
    live_activation_enabled: bool,
) -> BrokerMode:
    try:
        mode = BrokerMode(broker_mode)
    except ValueError as exc:
        raise ExecutionValidationError(f"unsupported broker_mode '{broker_mode}'") from exc
    if mode != BrokerMode.PAPER:
        raise ExecutionValidationError("only paper broker_mode is supported")
    if live_activation_enabled:
        raise ExecutionValidationError("live_activation_enabled is locked")
    if mode == BrokerMode.MT5_LIVE and live_capability_locked:
        raise ExecutionValidationError("live execution capability is locked")
    return mode


def validate_strategy_identity(
    *,
    strategy_name: str,
    strategy_version: int,
    compiled_config: dict[str, Any],
    config_hash: str,
    symbol: str,
    timeframe: str,
    sizing_config: dict[str, Any],
    risk_config: Optional[dict[str, Any]] = None,
) -> StrategyIdentity:
    if not strategy_name:
        raise ExecutionValidationError("strategy_name is required")
    if strategy_version < 1:
        raise ExecutionValidationError("strategy_version must be >= 1")
    if not symbol:
        raise ExecutionValidationError("symbol is required")
    if not config_hash:
        raise ExecutionValidationError("config_hash is required")
    expected_hash = compute_config_hash(compiled_config)
    if config_hash != expected_hash:
        raise ExecutionValidationError("config_hash does not match compiled_config")
    try:
        info = get_registered_strategy(strategy_name)
    except ValueError as exc:
        raise ExecutionValidationError(str(exc)) from exc
    if info.info.engine != "candle":
        raise ExecutionValidationError("tick strategies cannot be deployed to forward execution")
    normalized_tf = validate_timeframe(timeframe)
    cfg_symbol = compiled_config.get("symbol")
    cfg_tf = compiled_config.get("timeframe")
    if cfg_symbol and cfg_symbol != symbol:
        raise ExecutionValidationError("symbol must match compiled_config.symbol")
    if cfg_tf and cfg_tf.upper() != normalized_tf:
        raise ExecutionValidationError("timeframe must match compiled_config.timeframe")
    return StrategyIdentity(
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        compiled_config=compiled_config,
        config_hash=config_hash,
        symbol=symbol,
        timeframe=normalized_tf,
        sizing_config=validate_sizing_config(sizing_config),
        risk_config=validate_risk_config(risk_config),
    )


def identity_from_saved_backtest(
    session: Session,
    *,
    source_backtest_run_id: uuid.UUID,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    sizing_config: Optional[dict[str, Any]] = None,
    risk_config: Optional[dict[str, Any]] = None,
) -> StrategyIdentity:
    run = get_backtest_run(session, source_backtest_run_id)
    if run is None:
        raise ExecutionValidationError("source_backtest_run_id not found")
    if not run.is_saved:
        raise ExecutionValidationError("source backtest run must be saved")
    config = dict(run.config or {})
    if config.get("engine") == "tick":
        raise ExecutionValidationError("tick backtests cannot be deployed to forward execution")
    strategy_name = str(config.get("strategy") or "")
    compiled_config = {
        "strategy": strategy_name,
        "strategy_params": dict(config.get("strategy_params") or {}),
        "symbol": symbol or str(config.get("symbol") or ""),
        "timeframe": timeframe or str(config.get("timeframe") or "H1"),
    }
    if config.get("entries") is not None:
        compiled_config["entries"] = config.get("entries")
    if config.get("entry_manager") is not None:
        compiled_config["entry_manager"] = config.get("entry_manager")
    if config.get("exit_params") is not None:
        compiled_config["exit_params"] = config.get("exit_params")
    config_hash = compute_config_hash(compiled_config)
    default_sizing = sizing_config or {"type": "fixed_quantity", "quantity": 1.0}
    return validate_strategy_identity(
        strategy_name=strategy_name,
        strategy_version=1,
        compiled_config=compiled_config,
        config_hash=config_hash,
        symbol=compiled_config["symbol"],
        timeframe=compiled_config["timeframe"],
        sizing_config=default_sizing,
        risk_config=risk_config,
    )
