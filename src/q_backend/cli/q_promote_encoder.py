"""CLI entry point for operator-run neural model status promotion (WO146)."""

from __future__ import annotations

import argparse
import sys

import sentry_sdk

from q_backend.neural.promotion import (
    IllegalNeuralStatusTransition,
    NeuralModelNotFoundError,
    promote_neural_model,
)
from q_backend.observability.sentry import init_sentry
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    init_sentry(get_settings(), component="cli")
    sentry_sdk.set_tag("cli_command", "q_promote_encoder")
    parser = argparse.ArgumentParser(description="Promote a neural encoder model version")
    parser.add_argument("--model-hash", required=True, dest="model_hash")
    parser.add_argument(
        "--to",
        required=True,
        choices=[status.value for status in NeuralModelStatus],
        dest="target_status",
        help="Target lifecycle status",
    )
    args = parser.parse_args(argv)

    try:
        with session_scope() as session:
            version = promote_neural_model(
                session,
                model_hash=args.model_hash,
                target_status=args.target_status,
            )
    except NeuralModelNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except IllegalNeuralStatusTransition as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print({"model_hash": version.model_hash, "status": version.status})
    return 0


if __name__ == "__main__":
    sys.exit(main())
