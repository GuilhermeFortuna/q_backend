import argparse
import sys
from pathlib import Path

import sentry_sdk

from q_backend.observability.sentry import init_sentry
from q_backend.optimization import (
    DefaultBacktestRunner,
    OptimizationRunner,
    export_results,
    load_optimization_config,
)
from q_backend.storage.settings import get_settings


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    init_sentry(get_settings(), component="cli")
    sentry_sdk.set_tag("cli_command", "q_optimize")
    parser = argparse.ArgumentParser(description="Run strategy parameter optimization")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to optimization YAML config",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for exported results (default: data/optimization/results/<study_name>)",
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=None,
        help="Override number of trials from config",
    )
    args = parser.parse_args(argv)

    config = load_optimization_config(args.config)
    if args.n_trials is not None:
        config.study.n_trials = args.n_trials

    backtest_runner = DefaultBacktestRunner()
    runner = OptimizationRunner(config, backtest_runner)
    result = runner.run()

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else _project_root() / "data" / "optimization" / "results" / config.study.name
    )
    paths = export_results(result, config, output_dir)
    print(paths.summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
