import pandas as pd

from q_backend.backtesting import MACrossoverStrategy, serialize_chart_data


def _sample_ohlcv_df(length: int = 20) -> pd.DataFrame:
    rows = []
    for i in range(length):
        close = 100.0 + i
        rows.append(
            {
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "tick_volume": 100 + i,
            }
        )
    index = pd.date_range("2024-01-01", periods=length, freq="D")
    return pd.DataFrame(rows, index=index)


def test_macrossover_get_chart_indicators():
    strategy = MACrossoverStrategy(
        short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT"
    )
    specs = strategy.get_chart_indicators()

    assert len(specs) == 3
    keys = {spec.key for spec in specs}
    assert keys == {"ma_short", "ma_long", "delta"}

    price_specs = [spec for spec in specs if spec.pane == "price"]
    assert len(price_specs) == 2
    assert any("SMA Short (2)" in spec.label for spec in price_specs)
    assert any("SMA Long (4)" in spec.label for spec in price_specs)


def test_serialize_chart_data_shape_and_alignment():
    strategy = MACrossoverStrategy(
        short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT"
    )
    df = _sample_ohlcv_df(20)
    df_with_indicators = strategy.compute_indicators(df)
    chart_data = serialize_chart_data(df_with_indicators, strategy)

    bars = chart_data["bars"]
    indicators = chart_data["indicators"]

    assert len(bars) == 20
    assert len(indicators) == 3

    for bar in bars:
        assert "timestamp" in bar
        assert {"open", "high", "low", "close", "volume"} <= set(bar.keys())

    indicator_keys = {ind["key"] for ind in indicators}
    assert indicator_keys == {"ma_short", "ma_long", "delta"}

    for ind in indicators:
        assert len(ind["values"]) == len(bars)
        assert ind["pane"] in ("price", "oscillator")

    ma_short = next(ind for ind in indicators if ind["key"] == "ma_short")
    assert ma_short["values"][0] is None
    assert ma_short["values"][1] is not None
