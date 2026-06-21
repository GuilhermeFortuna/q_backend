from __future__ import annotations

from q_backend.backtesting.strategy_registry import ExitPreset

EXIT_PRESETS: list[ExitPreset] = [
    ExitPreset(
        id="atr_stop_chandelier",
        label="ATR stop + Chandelier trail",
        description="Volatility stop with a trailing lock as the trend runs.",
        parameters={
            "stop_loss_atr": 2.0,
            "atr_period": 14,
            "chandelier_atr_mult": 3.0,
        },
    ),
    ExitPreset(
        id="breakeven_time_stop",
        label="Break-even + Time stop",
        description="Lock in a small gain after a move, then cap holding period.",
        parameters={
            "breakeven_trigger_pct": 0.02,
            "breakeven_offset_pct": 0.001,
            "max_bars_in_trade": 50,
        },
    ),
    ExitPreset(
        id="fixed_pct_bracket",
        label="Fixed % bracket",
        description="Classic symmetric stop-loss and take-profit percentages from entry.",
        parameters={
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.05,
        },
    ),
    ExitPreset(
        id="parabolic_sar_trail",
        label="Parabolic SAR trail",
        description="Wilder parabolic SAR trailing stop with standard acceleration settings.",
        parameters={
            "psar_af_start": 0.02,
            "psar_af_step": 0.02,
            "psar_af_max": 0.2,
        },
    ),
    ExitPreset(
        id="donchian_channel_trail",
        label="Donchian channel trail",
        description="Exit on a break of the opposite Donchian channel extreme.",
        parameters={
            "donchian_exit_period": 20,
        },
    ),
    ExitPreset(
        id="ratchet_target_atr_stop",
        label="Ratchet target + ATR stop",
        description="Volatility stop with a ratcheting profit floor once the move extends.",
        parameters={
            "target_ratchet_atr": 2.0,
            "stop_loss_atr": 2.0,
            "atr_period": 14,
        },
    ),
]
