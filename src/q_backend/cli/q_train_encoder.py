"""CLI entry point for operator-run neural encoder training (WO142)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from q_backend.features.matrix import FeatureRequest, build_feature_matrix
from q_backend.neural.gate import evaluate_latents
from q_backend.neural.training import default_train_encoder_config, train_encoder
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

    def window_builder(symbol: str, timeframe: str, start: datetime, end: datetime):
        requests = [
            FeatureRequest(name=name, version=None, params={}) for name in args.features
        ]
        matrix = build_feature_matrix(
            symbol,
            timeframe,
            start,
            end,
            requests,
            use_cache=True,
        )
        return matrix.frame

    with session_scope() as session:
        version = train_encoder(
            session,
            config,
            window_builder=window_builder,
        )
        gate_result = None
        if args.evaluate is not None:
            target_name, horizon_raw = args.evaluate
            gate_result = evaluate_latents(
                session,
                version,
                target_name=target_name,
                horizon=int(horizon_raw),
            )

    output: dict[str, object] = {
        "model_key": config.model_key,
        "model_hash": version.model_hash,
        "version": version.version,
        "val_metrics": version.val_metrics,
        "artifact_path": version.artifact_path,
    }
    if gate_result is not None:
        output["gate"] = {
            "baseline_ic": gate_result.baseline_ic,
            "best_latent_ic": gate_result.best_latent_ic,
            "n_latents_beating_baseline": gate_result.n_latents_beating_baseline,
            "passed": gate_result.passed,
            "evaluation_run_id": gate_result.evaluation_run_id,
        }
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
