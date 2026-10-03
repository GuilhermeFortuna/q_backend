import numpy as np
import pandas as pd
import pytest

from q_backend.ml_filters.adapters import create_classifier


@pytest.mark.parametrize("algorithm", ["lightgbm", "random_forest", "logistic_regression"])
def test_classifier_fit_and_load_preserve_order_and_scores(algorithm):
    X = pd.DataFrame({"close": [0.0, 0.2, 1.0, 1.2], "side": [1, -1, 1, -1]})
    y = np.array([0, 0, 1, 1])
    model = create_classifier(
        algorithm, {"n_estimators": 4} if algorithm != "logistic_regression" else {"max_iter": 200}
    )
    model.fit(X, y)
    before = model.predict_good_entry_probability(X)
    loaded = type(model).load(model.dump())
    after = loaded.predict_good_entry_probability(X)

    np.testing.assert_allclose(before, after, rtol=1e-7, atol=1e-9)
    with pytest.raises(ValueError, match="columns and order"):
        loaded.predict_good_entry_probability(X[["side", "close"]])


def test_logistic_scaler_uses_only_rows_passed_to_fit():
    X = pd.DataFrame({"close": [0.0, 2.0], "side": [1, -1]})
    model = create_classifier("logistic_regression", {"max_iter": 200}).fit(X, [0, 1])
    scaler = model.pipeline.named_steps["preprocessing"]

    np.testing.assert_allclose(scaler.mean_, [1.0, -0.0])


def test_classifier_rejects_unknown_or_out_of_range_parameters():
    with pytest.raises(ValueError, match="Unknown"):
        create_classifier("random_forest", {"class_weight": "balanced"})
    with pytest.raises(ValueError, match="outside its allowed range"):
        create_classifier("lightgbm", {"learning_rate": 0})


def test_classifier_requires_both_classes_and_finite_features():
    model = create_classifier("random_forest", {"n_estimators": 2})
    X = pd.DataFrame({"close": [0.0, 1.0], "side": [1, -1]})
    with pytest.raises(ValueError, match="both classes"):
        model.fit(X, [1, 1])
    X.iloc[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        model.fit(X, [0, 1])
