import optuna
import pytest

from q_backend.optimization.models import TrialParams
from q_backend.optimization.validators import validate_trial_params


def test_invalid_ma_periods_pruned():
    with pytest.raises(optuna.TrialPruned):
        validate_trial_params(
            TrialParams(strategy_params={"short_period": 50, "long_period": 20}),
            "MACrossover",
        )


def test_invalid_max_contracts_pruned():
    with pytest.raises(optuna.TrialPruned):
        validate_trial_params(
            TrialParams(
                strategy_params={},
                risk_params={
                    "type": "fixed_safety_margin",
                    "min_contracts": 5,
                    "max_contracts": 2,
                },
            ),
            "MACrossover",
        )


def test_valid_params_pass():
    validate_trial_params(
        TrialParams(strategy_params={"short_period": 5, "long_period": 20}),
        "MACrossover",
    )
