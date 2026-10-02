"""Session trade snapshot and history schemas, taken from the vendored Q-079 contracts."""

from q_contracts.stream import (
    TradeHistoryPageHeaders,
    TradeHistoryPending,
    TradeSnapshotResponse,
    TradeSourceStatus,
    TradeWatermark,
)

# Response header names are declared by the contract (``http_header``); the generated
# dataclass carries the snake_case field names, so the mapping lives here once.
HISTORY_HEADER_NAMES = {
    "snapshot_id": "X-Q-Trade-Snapshot-Id",
    "source_generation": "X-Q-Trade-Source-Generation",
    "symbol": "X-Q-Trade-Symbol",
    "volume_field": "X-Q-Trade-Volume-Field",
    "volume_unit": "X-Q-Trade-Volume-Unit",
    "page_count": "X-Q-Trade-Page-Count",
    "frozen_epoch": "X-Q-Trade-Frozen-Epoch",
    "frozen_seq": "X-Q-Trade-Frozen-Seq",
    "next_cursor": "X-Q-Trade-Next-Cursor",
}

__all__ = [
    "HISTORY_HEADER_NAMES",
    "TradeHistoryPageHeaders",
    "TradeHistoryPending",
    "TradeSnapshotResponse",
    "TradeSourceStatus",
    "TradeWatermark",
]
