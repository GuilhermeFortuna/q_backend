"""Operator CLI for market-data lake dataset catalog management."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import uuid

from q_backend.market_data.catalog.repository import get_dataset, to_manifest
from q_backend.market_data.catalog.service import get_lake_catalog

logger = logging.getLogger("q_backend.cli.q_catalog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="q-catalog",
        description="Market-data lake dataset catalog management",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # adopt
    subparsers.add_parser(
        "adopt",
        help="Scan the lake directory and adopt uncataloged existing series",
    )

    # sweep [--dry-run]
    sweep_parser = subparsers.add_parser(
        "sweep",
        help="Delete unshared files of tombstoned datasets whose grace period expired",
    )
    sweep_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without removing files",
    )

    # show <dataset_id>
    show_parser = subparsers.add_parser(
        "show",
        help="Show JSON manifest for a dataset identifier",
    )
    show_parser.add_argument("dataset_id", help="Dataset UUID")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    catalog = get_lake_catalog()

    if args.command == "adopt":
        report = catalog.adopt()
        bars_count = sum(1 for s in report.adopted if s.kind == "bars")
        ticks_count = sum(1 for s in report.adopted if s.kind == "ticks")
        print(
            f"Adoption complete.\n"
            f"  Adopted: {len(report.adopted)} datasets (bars: {bars_count}, ticks: {ticks_count})\n"
            f"  Files: {report.files}\n"
            f"  Bytes: {report.bytes}\n"
            f"  Skipped (already cataloged): {len(report.skipped_existing)}"
        )
        return 0

    elif args.command == "sweep":
        report = catalog.sweep(dry_run=args.dry_run)
        mode = "Dry-run sweep report" if args.dry_run else "Sweep report"
        print(
            f"{mode}:\n"
            f"  Datasets deleted: {report.datasets_deleted}\n"
            f"  Files deleted: {report.files_deleted}\n"
            f"  Bytes freed: {report.bytes_freed}\n"
            f"  Unreferenced files: {len(report.unreferenced_files)}"
        )
        for uf in report.unreferenced_files:
            print(f"    - {uf}")
        return 0

    elif args.command == "show":
        try:
            uid = uuid.UUID(args.dataset_id)
        except ValueError:
            print(f"Error: '{args.dataset_id}' is not a valid UUID", file=sys.stderr)
            return 2

        with catalog.session_factory() as session:
            dataset = get_dataset(session, uid)
            if dataset is None:
                print(f"Dataset '{args.dataset_id}' not found", file=sys.stderr)
                return 1

            manifest = to_manifest(dataset)
            print(json.dumps(dataclasses.asdict(manifest), indent=2))
            return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
