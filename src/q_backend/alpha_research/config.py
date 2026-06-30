"""Strategy-search configuration for alpha-research runs (WO164)."""

from __future__ import annotations

from q_backend.features.split_manifest import SplitManifest
from q_backend.optimization.hypothesis import InstrumentResearchProfile
from q_backend.optimization.models import (
    BacktestConfig,
    ObjectiveConfig,
    ObjectiveMode,
    StudyConfig,
)
from q_backend.optimization.strategy_search import (
    GateConfig,
    LockboxConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig


def _default_walkforward(profile: InstrumentResearchProfile) -> WalkForwardConfig:
    if profile.style == "day_trade":
        return WalkForwardConfig(train_days=20, test_days=10, mode="rolling", min_windows=6)
    return WalkForwardConfig(train_days=60, test_days=30, mode="rolling", min_windows=6)


def build_alpha_search_config(
    profile: InstrumentResearchProfile,
    manifest: SplitManifest,
    *,
    study_n_trials: int = 30,
    study_seed: int = 42,
    walkforward: WalkForwardConfig | None = None,
) -> StrategySearchConfig:
    """Build a hypothesis discovery config aligned to the immutable split manifest."""
    rules = profile.session_rules
    lockbox_fraction = manifest.fractions[2]
    wf = walkforward or _default_walkforward(profile)
    min_trades = int(profile.minimum_observation_counts.get("min_trades", 30))
    min_windows = int(profile.minimum_observation_counts.get("min_oos_windows", 6))

    return StrategySearchConfig(
        backtest=BacktestConfig(
            symbol=profile.symbol,
            timeframe=profile.timeframe,
            start=manifest.range_start,
            end=manifest.range_end,
            initial_capital=100_000.0,
            point_value=1.0,
            strategy="CompositeStrategy",
            day_trade=rules.day_trade,
            day_trade_start_time=rules.day_trade_start_time,
            day_trade_end_time=rules.day_trade_end_time,
            day_trade_close_time=rules.day_trade_close_time,
        ),
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_SHARPE),
        walkforward=wf,
        study=StudyConfig(
            name=f"alpha_research_{profile.profile_id}",
            n_trials=study_n_trials,
            seed=study_seed,
        ),
        strategies=None,
        include_risk_search=False,
        gates=GateConfig(
            min_completed_windows=min_windows,
            min_oos_trades=min_trades,
        ),
        lockbox=LockboxConfig(
            enabled=True,
            lockbox_pct=lockbox_fraction,
            min_trades=max(5, min_trades // 10),
            max_drawdown_pct=(
                profile.research_acceptance.lockbox_max_drawdown_pct
                if profile.research_acceptance is not None
                else None
            ),
        ),
    )
