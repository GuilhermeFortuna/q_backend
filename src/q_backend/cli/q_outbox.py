"""Operator CLI for transactional outbox management."""

from __future__ import annotations

import argparse
from datetime import timedelta
import logging
import sys

from q_backend.storage.db.engine import session_scope
from q_backend.streaming.outbox import prune_relayed, rotate_epoch

logger = logging.getLogger("q_backend.cli.q_outbox")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="q-outbox",
        description="Transactional outbox operator management",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # rotate-epoch TOPIC --reason REASON
    rotate_parser = subparsers.add_parser("rotate-epoch", help="Rotate epoch for a durable topic")
    rotate_parser.add_argument("topic", help="Durable topic name")
    rotate_parser.add_argument("--reason", required=True, help="Operator reason for epoch rotation")

    # prune [--older-than-days N]
    prune_parser = subparsers.add_parser("prune", help="Prune relayed events older than retention cutoff")
    prune_parser.add_argument(
        "--older-than-days",
        type=int,
        default=30,
        help="Retention threshold in days (default: 30)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.command == "rotate-epoch":
        with session_scope() as session:
            old_epoch, new_epoch = rotate_epoch(session, args.topic, reason=args.reason)
            logger.info(
                "Rotated topic %s epoch: %s -> %s (reason: %s)",
                args.topic,
                old_epoch,
                new_epoch,
                args.reason,
            )
            print(f"Rotated topic '{args.topic}' epoch: {old_epoch} -> {new_epoch}")
        return 0

    elif args.command == "prune":
        older_than = timedelta(days=args.older_than_days)
        with session_scope() as session:
            retained = prune_relayed(session, older_than=older_than)
            logger.info(
                "Pruned relayed events older than %s days. Oldest retained seqs: %s",
                args.older_than_days,
                retained,
            )
            print(
                f"Pruned relayed events older than {args.older_than_days} days. Oldest retained seq per topic: {retained}"
            )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
