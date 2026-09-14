from collections.abc import Mapping
from typing import Literal

JobKind = Literal[
    "alpha_research",
    "backtest",
    "discovery_ab",
    "encoder_ablation",
    "neural_training",
    "optimization",
    "storage_ingest",
    "strategy_search",
    "walkforward",
]

NAMESPACE_TO_KIND: Mapping[str, JobKind] = {
    "alpha_research": "alpha_research",
    "backtest": "backtest",
    "discovery_ab": "discovery_ab",
    "encoder_ablation": "encoder_ablation",
    "job": "optimization",
    "neural_training": "neural_training",
    "optimization": "optimization",
    "storage_ingest": "storage_ingest",
    "strategy_search": "strategy_search",
    "walkforward": "walkforward",
}

StreamStatus = Literal["queued", "running", "completed", "failed", "cancelled"]

STATUS_TO_STREAM: Mapping[str, StreamStatus] = {
    "cancelled": "cancelled",
    "completed": "completed",
    "done": "completed",
    "error": "failed",
    "failed": "failed",
    "no_result": "failed",
    "pending": "queued",
    "queued": "queued",
    "running": "running",
}
