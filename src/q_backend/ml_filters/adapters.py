from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from q_backend.ml_filters.config import Algorithm

_ALLOWED = {
    "lightgbm": {
        "n_estimators": (int, 1, 2000),
        "learning_rate": (float, 0.0, 1.0),
        "num_leaves": (int, 2, 256),
    },
    "random_forest": {
        "n_estimators": (int, 1, 2000),
        "max_depth": (int, 1, 100),
        "min_samples_leaf": (int, 1, 100),
    },
    "logistic_regression": {
        "C": (float, 0.0, 1_000_000.0),
        "max_iter": (int, 100, 10_000),
    },
}
_DEFAULTS = {
    "lightgbm": {"n_estimators": 100, "learning_rate": 0.1, "num_leaves": 31},
    "random_forest": {"n_estimators": 200, "max_depth": None, "min_samples_leaf": 1},
    "logistic_regression": {"C": 1.0, "max_iter": 1000},
}


class EntryClassifier(Protocol):
    def fit(self, X: pd.DataFrame, y: pd.Series | np.ndarray) -> "EntryClassifier": ...

    def predict_good_entry_probability(self, X: pd.DataFrame) -> np.ndarray: ...

    def dump(self) -> bytes: ...

    @classmethod
    def load(cls, payload: bytes) -> "EntryClassifier": ...


@dataclass
class SklearnEntryClassifier:
    algorithm: Algorithm
    hyperparameters: dict[str, Any]
    seed: int
    pipeline: Pipeline
    feature_names: tuple[str, ...] = ()

    def fit(self, X: pd.DataFrame, y: pd.Series | np.ndarray) -> "SklearnEntryClassifier":
        if not isinstance(X, pd.DataFrame) or X.empty:
            raise ValueError("X must be a non-empty DataFrame")
        labels = np.asarray(y, dtype=np.int8)
        if len(labels) != len(X) or not set(labels.tolist()).issubset({0, 1}):
            raise ValueError("fit requires aligned binary labels containing only classes 0 and 1")
        if set(labels.tolist()) != {0, 1}:
            raise ValueError("fit requires both classes 0 and 1")
        if not np.isfinite(X.to_numpy(dtype=float)).all():
            raise ValueError("X must contain only finite features")
        self.feature_names = tuple(str(column) for column in X.columns)
        self.pipeline.fit(X.loc[:, self.feature_names], labels)
        return self

    def predict_good_entry_probability(self, X: pd.DataFrame) -> np.ndarray:
        if not self.feature_names:
            raise ValueError("Classifier has not been fitted")
        if tuple(X.columns) != self.feature_names:
            raise ValueError("Feature columns and order must match the fitted model")
        classifier = self.pipeline.named_steps["classifier"]
        classes = list(classifier.classes_)
        if 1 not in classes:
            raise ValueError("Fitted model has no positive class labelled 1")
        probability_column = classes.index(1)
        scores = self.pipeline.predict_proba(X)[:, probability_column]
        if not np.isfinite(scores).all() or ((scores < 0) | (scores > 1)).any():
            raise ValueError("Classifier produced an invalid probability")
        return scores

    def dump(self) -> bytes:
        buffer = BytesIO()
        joblib.dump(
            {
                "format_version": 1,
                "algorithm": self.algorithm,
                "hyperparameters": self.hyperparameters,
                "seed": self.seed,
                "feature_names": self.feature_names,
                "pipeline": self.pipeline,
            },
            buffer,
        )
        return buffer.getvalue()

    @classmethod
    def load(cls, payload: bytes) -> "SklearnEntryClassifier":
        try:
            state = joblib.load(BytesIO(payload))
        except Exception as exc:
            raise ValueError("Fitted model artifact is corrupt or unsupported") from exc
        if not isinstance(state, dict) or state.get("format_version") != 1:
            raise ValueError("Unsupported fitted model artifact version")
        algorithm = state.get("algorithm")
        if algorithm not in _DEFAULTS:
            raise ValueError("Fitted model uses an unknown algorithm")
        if not isinstance(state.get("pipeline"), Pipeline) or not state.get("feature_names"):
            raise ValueError("Fitted model artifact is incomplete")
        return cls(
            algorithm=algorithm,
            hyperparameters=state["hyperparameters"],
            seed=state["seed"],
            feature_names=tuple(state["feature_names"]),
            pipeline=state["pipeline"],
        )


def _validate_hyperparameters(algorithm: Algorithm, values: dict[str, Any] | None) -> dict[str, Any]:
    if algorithm not in _DEFAULTS:
        raise ValueError(f"Unsupported ML filter algorithm: {algorithm}")
    supplied = dict(values or {})
    unknown = set(supplied) - set(_ALLOWED[algorithm])
    if unknown:
        raise ValueError(f"Unknown {algorithm} hyperparameter(s): {', '.join(sorted(unknown))}")
    result = {**_DEFAULTS[algorithm], **supplied}
    for name, value in result.items():
        if name == "max_depth" and value is None:
            continue
        expected, lower, upper = _ALLOWED[algorithm][name]
        if isinstance(value, bool) or not isinstance(value, (int, float) if expected is float else expected):
            raise ValueError(f"{name} has an invalid type")
        if not lower <= value <= upper or (name in {"learning_rate", "C"} and value == 0):
            raise ValueError(f"{name} is outside its allowed range")
        result[name] = float(value) if expected is float else int(value)
    return result


def create_classifier(
    algorithm: Algorithm,
    hyperparams: dict[str, Any] | None = None,
    seed: int = 42,
) -> SklearnEntryClassifier:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("seed must be a nonnegative 32-bit integer")
    params = _validate_hyperparameters(algorithm, hyperparams)
    if algorithm == "lightgbm":
        try:
            from lightgbm import LGBMClassifier
        except ImportError as exc:
            raise RuntimeError("LightGBM is required to fit lightgbm models") from exc
        estimator = LGBMClassifier(
            objective="binary",
            n_jobs=1,
            random_state=seed,
            verbosity=-1,
            deterministic=True,
            force_col_wise=True,
            **params,
        )
        preprocessing = FunctionTransformer(validate=False)
    elif algorithm == "random_forest":
        estimator = RandomForestClassifier(n_jobs=1, random_state=seed, **params)
        preprocessing = FunctionTransformer(validate=False)
    else:
        estimator = LogisticRegression(random_state=seed, **params)
        preprocessing = StandardScaler()
    pipeline = Pipeline([("preprocessing", preprocessing), ("classifier", estimator)])
    return SklearnEntryClassifier(algorithm, params, seed, pipeline)
