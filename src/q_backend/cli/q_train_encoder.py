"""CLI entry point for operator-run neural encoder training (WO142)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from q_backend.neural.training import default_train_encoder_config
from q_backend.neural.training_pipeline import (
    TrainEncoderEvaluateSpec,
    run_train_encoder_pipeline,
)
from q_backend.storage.db.engine import session_scope

_DEFAULT_INPUT_FEATURES: tuple[str, ...] = (
    "rsi",
    "macd",
    "momentum",
    "realized_vol",
    "ma",
    "bollinger_upper",
    "bollinger_lower",
)


def _parse_datetime(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train a neural encoder")
    parser.add_argument(
        "--kind",
        choices=("pca", "autoencoder"),
        default="pca",
        help="Encoder kind (default: pca)",
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--train-start", required=True, dest="train_start")
    parser.add_argument("--train-end", required=True, dest="train_end")
    parser.add_argument("--n-latents", required=True, type=int, dest="n_latents")
    parser.add_argument(
        "--model-key",
        default=None,
        help="Stable registry name (default: pca_<symbol>_<timeframe>)",
    )
    parser.add_argument(
        "--features",
        nargs="+",
        default=list(_DEFAULT_INPUT_FEATURES),
        help="Classical feature names for the encoder input window",
    )
    parser.add_argument(
        "--evaluate",
        nargs=2,
        metavar=("TARGET", "HORIZON"),
        help="After training, run the latent IC gate (e.g. fwd_return 5)",
    )
    args = parser.parse_args(argv)

    train_start = _parse_datetime(args.train_start)
    train_end = _parse_datetime(args.train_end)
    if train_end <= train_start:
        print("train-end must be after train-start", file=sys.stderr)
        return 1

    config = default_train_encoder_config(
        kind=args.kind,
        symbol=args.symbol,
        timeframe=args.timeframe,
        train_start=train_start,
        train_end=train_end,
        n_latents=args.n_latents,
        input_features=tuple(args.features),
        model_key=args.model_key,
    )

    evaluate = None
    if args.evaluate is not None:
        target_name, horizon_raw = args.evaluate
        evaluate = TrainEncoderEvaluateSpec(
            target=target_name,
            horizon=int(horizon_raw),
        )

    with session_scope() as session:
        result = run_train_encoder_pipeline(
            session,
            config,
            input_features=tuple(args.features),
            evaluate=evaluate,
        )

    output: dict[str, object] = {
        "model_key": result.model_key,
        "model_hash": result.model_hash,
        "version": result.version,
        "val_metrics": result.val_metrics,
        "artifact_path": result.artifact_path,
    }
    if result.gate is not None:
        output["gate"] = {
            "baseline_ic": result.gate.baseline_ic,
            "best_latent_ic": result.gate.best_latent_ic,
            "n_latents_beating_baseline": result.gate.n_latents_beating_baseline,
            "passed": result.gate.passed,
            "evaluation_run_id": result.gate.evaluation_run_id,
        }
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
