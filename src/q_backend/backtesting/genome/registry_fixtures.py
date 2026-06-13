"""Canonical registry-equivalent genome fixtures (design Appendix A)."""

from __future__ import annotations

from typing import Any

MA_CROSSOVER_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-ma-crossover",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.ma",
            "params": {
                "period": {"param": "short_period"},
                "ma_type": {"param": "short_ma_type"},
            },
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "ind.ma",
            "params": {
                "period": {"param": "long_period"},
                "ma_type": {"param": "long_ma_type"},
            },
            "inputs": ["n1"],
        },
        {"id": "n4", "kind": "ind.diff", "params": {}, "inputs": ["n2", "n3"]},
        {
            "id": "n5",
            "kind": "cmp.cross_above",
            "params": {"threshold": {"param": "threshold"}},
            "inputs": ["n4"],
        },
        {
            "id": "n6",
            "kind": "cmp.cross_below",
            "params": {"threshold": {"param": "threshold", "negate": True}},
            "inputs": ["n4"],
        },
    ],
    "entry_long": {"ref": "n5"},
    "entry_short": {"ref": "n6"},
    "exit_long": {"ref": "n6"},
    "exit_short": {"ref": "n5"},
    "metadata": {"equivalent_registry": "MACrossover"},
}

RSI_MEAN_REVERSION_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-rsi-mean-reversion",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.rsi",
            "params": {"period": {"param": "period"}},
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "cmp.cross_above",
            "params": {"threshold": {"param": "oversold"}},
            "inputs": ["n2"],
        },
        {
            "id": "n4",
            "kind": "cmp.cross_above",
            "params": {"threshold": {"param": "overbought"}},
            "inputs": ["n2"],
        },
    ],
    "entry_long": {"ref": "n3"},
    "entry_short": {"ref": "n4"},
    "exit_long": {"ref": "n4"},
    "exit_short": {"ref": "n3"},
    "metadata": {"equivalent_registry": "RSIMeanReversion"},
}

BOLLINGER_REVERSION_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-bollinger-reversion",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.bollinger",
            "params": {
                "period": {"param": "period"},
                "num_std": {"param": "num_std"},
            },
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "cmp.touch_below",
            "params": {},
            "inputs": ["n1", "n2:bb_lower"],
        },
        {
            "id": "n4",
            "kind": "cmp.touch_above",
            "params": {},
            "inputs": ["n1", "n2:bb_upper"],
        },
        {
            "id": "n5",
            "kind": "exit.middle_band",
            "params": {},
            "inputs": ["n1", "n2:bb_middle"],
        },
    ],
    "entry_long": {"ref": "n3"},
    "entry_short": {"ref": "n4"},
    "exit_long": {"ref": "n5"},
    "exit_short": {"ref": "n5"},
    "metadata": {"equivalent_registry": "BollingerReversion"},
}

DONCHIAN_BREAKOUT_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-donchian-breakout",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.donchian",
            "params": {"period": {"param": "period"}},
            "inputs": [],
        },
        {
            "id": "n3",
            "kind": "cmp.cross_above",
            "params": {},
            "inputs": ["n1", "n2:donchian_upper"],
        },
        {
            "id": "n4",
            "kind": "cmp.cross_below",
            "params": {},
            "inputs": ["n1", "n2:donchian_lower"],
        },
    ],
    "entry_long": {"ref": "n3"},
    "entry_short": {"ref": "n4"},
    "exit_long": {"ref": "n4"},
    "exit_short": {"ref": "n3"},
    "metadata": {"equivalent_registry": "DonchianBreakout"},
}

MACD_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-macd",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.macd",
            "params": {
                "fast_period": {"param": "fast_period"},
                "slow_period": {"param": "slow_period"},
                "signal_period": {"param": "signal_period"},
            },
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "cmp.cross_above",
            "params": {},
            "inputs": ["n2:macd", "n2:macd_signal"],
        },
        {
            "id": "n4",
            "kind": "cmp.cross_below",
            "params": {},
            "inputs": ["n2:macd", "n2:macd_signal"],
        },
    ],
    "entry_long": {"ref": "n3"},
    "entry_short": {"ref": "n4"},
    "exit_long": {"ref": "n4"},
    "exit_short": {"ref": "n3"},
    "metadata": {"equivalent_registry": "MACD"},
}

TRB_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-trb",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.trb_channel",
            "params": {
                "period": {"param": "period"},
                "band_pct": {"param": "band_pct"},
            },
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "cmp.trb_breakout_above",
            "params": {},
            "inputs": ["n1", "n2:trb_upper", "n2:channel_high"],
        },
        {
            "id": "n4",
            "kind": "cmp.trb_breakout_below",
            "params": {},
            "inputs": ["n1", "n2:trb_lower", "n2:channel_low"],
        },
        {
            "id": "n5",
            "kind": "exit.fixed_holding",
            "params": {"holding_period": {"param": "holding_period"}},
            "inputs": [],
        },
    ],
    "entry_long": {"ref": "n3"},
    "entry_short": {"ref": "n4"},
    "exit_long": {"ref": "n5"},
    "exit_short": {"ref": "n5"},
    "metadata": {"equivalent_registry": "TRB"},
}

FMA_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-fma",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.ma",
            "params": {
                "period": {"param": "period"},
                "ma_type": {"param": "ma_type"},
            },
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "ind.ma_band",
            "params": {"band_pct": {"param": "band_pct"}},
            "inputs": ["n2"],
        },
        {
            "id": "n4",
            "kind": "cmp.cross_above",
            "params": {},
            "inputs": ["n1", "n3:ma_band_upper"],
        },
        {
            "id": "n5",
            "kind": "cmp.cross_below",
            "params": {},
            "inputs": ["n1", "n3:ma_band_lower"],
        },
        {
            "id": "n6",
            "kind": "exit.fixed_holding",
            "params": {"holding_period": {"param": "holding_period"}},
            "inputs": [],
        },
    ],
    "entry_long": {"ref": "n4"},
    "entry_short": {"ref": "n5"},
    "exit_long": {"ref": "n6"},
    "exit_short": {"ref": "n6"},
    "metadata": {"equivalent_registry": "FMA"},
}

TSMOM_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "example-tsmom",
    "nodes": [
        {
            "id": "n1",
            "kind": "ind.tsmom",
            "params": {
                "lookback_bars": {"param": "lookback_bars"},
                "rebalance_bars": {"param": "rebalance_bars"},
                "vol_window": {"param": "vol_window"},
                "vol_estimator": {"param": "vol_estimator"},
            },
            "inputs": [],
        },
    ],
    "entry_long": {"ref": "n1:buy_signal"},
    "entry_short": {"ref": "n1:sell_signal"},
    "exit_long": {"ref": "n1:sell_signal"},
    "exit_short": {"ref": "n1:buy_signal"},
    "metadata": {"equivalent_registry": "TSMOM"},
}

REGISTRY_GENOME_FIXTURES: dict[str, dict[str, Any]] = {
    "MACrossover": MA_CROSSOVER_GENOME,
    "RSIMeanReversion": RSI_MEAN_REVERSION_GENOME,
    "BollingerReversion": BOLLINGER_REVERSION_GENOME,
    "DonchianBreakout": DONCHIAN_BREAKOUT_GENOME,
    "MACD": MACD_GENOME,
    "TRB": TRB_GENOME,
    "FMA": FMA_GENOME,
    "TSMOM": TSMOM_GENOME,
}

REGISTRY_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "MACrossover": {
        "short_period": 50,
        "long_period": 200,
        "short_ma_type": "sma",
        "long_ma_type": "sma",
        "threshold": 0.0,
    },
    "RSIMeanReversion": {"period": 14, "oversold": 30.0, "overbought": 70.0},
    "BollingerReversion": {"period": 20, "num_std": 2.0},
    "DonchianBreakout": {"period": 20},
    "MACD": {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    "TRB": {"period": 60, "band_pct": 0.0, "holding_period": 10},
    "FMA": {"period": 60, "band_pct": 0.0, "ma_type": "sma", "holding_period": 10},
    "TSMOM": {
        "lookback_bars": 252,
        "rebalance_bars": 21,
        "vol_window": 63,
        "vol_estimator": "yang_zhang",
    },
}
