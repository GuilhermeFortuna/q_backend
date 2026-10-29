"""Declared decision timeframes: recorded by validation, enforced by stack runs and forward evaluation (Q-140)."""

from __future__ import annotations

from pathlib import Path

import pytest

from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.execution.validation import ExecutionValidationError, validate_timeframe
from q_backend.research.strategy_file_runs import _require_decision_timeframe
from q_backend.research.strategy_files import build_bundle, validate_bundle

SOURCE = '''from __future__ import annotations

from q_backend.research import ResearchStrategy, TradeOrder


class FineDecisions(ResearchStrategy):
    """Decides on M1 bars."""

    decision_timeframe = "{timeframe}"

    def entry_strategy(self, frame, positions=()):
        return None
'''


def _validate(tmp_path: Path, timeframe: str):
    root = tmp_path / "strategies"
    root.mkdir()
    (root / "fine_decisions.py").write_text(SOURCE.format(timeframe=timeframe), encoding="utf-8")
    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    return validate_bundle(build_bundle(root, "fine_decisions"), bundle_root)


def test_a_declared_decision_timeframe_is_recorded_on_the_validated_file(tmp_path: Path) -> None:
    info = _validate(tmp_path, "M1")

    assert info.valid is True
    assert info.decision_timeframe == "M1"


def test_a_file_without_a_declaration_has_no_decision_timeframe(tmp_path: Path) -> None:
    root = tmp_path / "strategies"
    root.mkdir()
    (root / "plain.py").write_text(
        SOURCE.format(timeframe="M1").replace('    decision_timeframe = "M1"\n', ""),
        encoding="utf-8",
    )
    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()

    info = validate_bundle(build_bundle(root, "plain"), bundle_root)

    assert info.valid is True
    assert info.decision_timeframe is None


def test_a_declaration_that_is_not_a_supported_timeframe_is_refused(tmp_path: Path) -> None:
    info = _validate(tmp_path, "W1")

    assert info.valid is False
    assert "not a supported timeframe" in info.error


def test_a_stack_run_refuses_a_frame_of_another_timeframe() -> None:
    class Fine:
        decision_timeframe = "M1"

    _require_decision_timeframe(Fine, "M1")
    with pytest.raises(ValueError, match="decides on M1 bars and cannot run on M5 bars"):
        _require_decision_timeframe(Fine, "M5")


def test_a_strategy_without_a_declaration_runs_on_any_timeframe() -> None:
    class Plain:
        decision_timeframe = None

    _require_decision_timeframe(Plain, "M5")
    _require_decision_timeframe(Plain, "M1")


def test_a_registry_strategy_is_still_refused_below_the_m5_floor() -> None:
    with pytest.raises(ExecutionValidationError, match="below the M5 minimum"):
        validate_timeframe("M1")


def test_a_deployment_refuses_a_strategy_whose_declared_timeframe_differs() -> None:
    from q_backend.research import ResearchStrategy

    class FineM1(ResearchStrategy):
        decision_timeframe = "M1"

        def entry_strategy(self, frame, positions=()):
            return None

    identity = StrategyIdentity(
        strategy_name="FineM1",
        strategy_version=1,
        compiled_config={"source_kind": "strategy_version", "strategy_params": {}, "timeframe": "M5"},
        config_hash="fine-m5",
        symbol="WIN$N",
        timeframe="M5",
    )

    with pytest.raises(ValueError, match="decides on M1 bars; this deployment runs on M5"):
        ResearchEvaluator(deployment_id="fine-m5", identity=identity, strategy_instance=FineM1(), window_bound=50)
