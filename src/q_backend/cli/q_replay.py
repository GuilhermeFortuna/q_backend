"""``q-replay``: external controller of the replay simulator (Q-145).

Controls the administrative listener only; no trading process is involved. Every state-changing
command carries a fresh UUID and the revision read just before it, so a repeated invocation
returns the recorded result and a stale view is refused rather than applied.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

from q_backend.replay.config import ReplayRunConfig
from q_backend.replay.dataset import DatasetError, load_manifest, verify_manifest
from q_backend.replay.server import (
    DEFAULT_ADMIN_PORT,
    DEFAULT_EDGE_PORT,
    DEFAULT_MARKET_PORT,
    ReplayServer,
)

TOKEN_ENV = "Q_REPLAY_TOKEN"


def _client(url: str) -> httpx.Client:
    headers = {}
    token = os.environ.get(TOKEN_ENV)
    if token:
        headers["X-Replay-Token"] = token
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=30.0)


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _prepare(args: argparse.Namespace) -> int:
    config = ReplayRunConfig.model_validate(json.loads(Path(args.config).read_text(encoding="utf-8")))
    root = Path(args.root)
    manifest = load_manifest(root / config.manifest_path)
    checksums = verify_manifest(
        manifest,
        root,
        symbols=list(config.symbols),
        start_utc=config.start_utc,
        end_utc=config.end_utc,
        warmup_start_utc=config.warmup_start_utc,
    )
    _print({"config_hash": config.config_hash(), "dataset_checksums": checksums, "status": "prepared"})
    return 0


def _serve(args: argparse.Namespace) -> int:
    server = ReplayServer(
        store_path=Path(args.store),
        dataset_root=Path(args.root),
        market_port=args.market_port,
        edge_port=args.edge_port,
        admin_port=args.admin_port,
        admin_token=os.environ.get(TOKEN_ENV),
    )
    server.start()
    print(f"q-replay serving: admin {args.admin_port}, market {args.market_port}, broker {args.edge_port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _create(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    with _client(args.url) as client:
        response = client.post("/replay/v1/runs", json=config)
    _print(response.json())
    return 0 if response.status_code < 400 else 1


def _status(args: argparse.Namespace) -> int:
    with _client(args.url) as client:
        response = client.get("/replay/v1/runs/current")
    _print(response.json())
    return 0 if response.status_code < 400 else 1


def _command(action: str, args: argparse.Namespace) -> int:
    with _client(args.url) as client:
        status = client.get("/replay/v1/runs/current")
        if status.status_code >= 400:
            _print(status.json())
            return 1
        body = {"command_id": str(uuid.uuid4()), "expected_revision": status.json()["revision"], "action": action}
        if action == "speed":
            body["rate"] = args.rate
        if action == "step":
            body["market_ms"] = args.market_ms
        response = client.post("/replay/v1/runs/current/commands", json=body)
    _print(response.json())
    return 0 if response.status_code < 400 else 1


def _journal(args: argparse.Namespace) -> int:
    with _client(args.url) as client:
        response = client.get("/replay/v1/runs/current/journal")
    _print(response.json())
    return 0 if response.status_code < 400 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="q-replay", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url", default=f"http://127.0.0.1:{DEFAULT_ADMIN_PORT}", help="administrative listener URL (loopback)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="verify a run config and its frozen dataset offline")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--root", required=True, help="directory the manifest paths are relative to")
    prepare.set_defaults(handler=_prepare)

    serve = sub.add_parser("serve", help="run the simulator listeners (admin, market, broker)")
    serve.add_argument("--store", required=True, help="SQLite run store path")
    serve.add_argument("--root", required=True)
    serve.add_argument("--market-port", type=int, default=DEFAULT_MARKET_PORT)
    serve.add_argument("--edge-port", type=int, default=DEFAULT_EDGE_PORT)
    serve.add_argument("--admin-port", type=int, default=DEFAULT_ADMIN_PORT)
    serve.set_defaults(handler=_serve)

    create = sub.add_parser("create", help="prepare a run from a config file (paused)")
    create.add_argument("--config", required=True)
    create.set_defaults(handler=_create)

    sub.add_parser("status", help="show the current run").set_defaults(handler=_status)
    sub.add_parser("pause", help="pause market time").set_defaults(handler=lambda a: _command("pause", a))
    sub.add_parser("resume", help="resume market time").set_defaults(handler=lambda a: _command("resume", a))
    sub.add_parser("stop", help="stop the run").set_defaults(handler=lambda a: _command("stop", a))
    speed = sub.add_parser("speed", help="change the market rate (0.1 to 10)")
    speed.add_argument("rate", type=float)
    speed.set_defaults(handler=lambda a: _command("speed", a))
    step = sub.add_parser("step", help="advance a paused run by a market duration")
    step.add_argument("market_ms", type=int)
    step.set_defaults(handler=lambda a: _command("step", a))
    sub.add_parser("journal", help="print the run journal").set_defaults(handler=_journal)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (DatasetError, ValueError, OSError) as exc:
        print(f"q-replay: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
