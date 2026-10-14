import pandas as pd

from q_backend.backtesting import MACrossoverStrategy, serialize_chart_data
from q_backend.backtesting.strategy import ChartIndicatorSpec


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
    strategy = MACrossoverStrategy(short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT")
    specs = strategy.get_chart_indicators()

    assert len(specs) == 3
    keys = {spec.key for spec in specs}
    assert keys == {"ma_short", "ma_long", "delta"}

    price_specs = [spec for spec in specs if spec.pane == "price"]
    assert len(price_specs) == 2
    assert any("SMA Short (2)" in spec.label for spec in price_specs)
    assert any("SMA Long (4)" in spec.label for spec in price_specs)


def test_serialize_chart_data_shape_and_alignment():
    strategy = MACrossoverStrategy(short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT")
    df = _sample_ohlcv_df(20)
    df_with_indicators = strategy.compute_indicators(df)
    chart_data = serialize_chart_data(df_with_indicators, strategy)

    bars = chart_data["bars"]
    indicators = chart_data["indicators"]

    assert len(bars) == 20
    assert len(indicators) == 3

    for bar in bars:
        assert "timestamp" in bar
        assert bar["timestamp"].endswith("Z")
        assert {"open", "high", "low", "close", "volume"} <= set(bar.keys())

    indicator_keys = {ind["key"] for ind in indicators}
    assert indicator_keys == {"ma_short", "ma_long", "delta"}

    for ind in indicators:
        assert len(ind["values"]) == len(bars)
        assert ind["pane"] in ("price", "oscillator")

    for ind in indicators:
        assert ind["line_style"] is None
        assert ind["line_width"] is None

    ma_short = next(ind for ind in indicators if ind["key"] == "ma_short")
    assert ma_short["values"][0] is None
    assert ma_short["values"][1] is not None


class _SegmentStrategy(MACrossoverStrategy):
    """Declares one pivot segment and one marker series over a hand-set pivot column."""

    def __init__(self):
        super().__init__(short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT")

    def get_chart_indicators(self):
        return [
            ChartIndicatorSpec(key="pivot", label="Top", kind="segment", start_column="pivot_bar", color="#26a69a"),
            ChartIndicatorSpec(key="ma_delta", label="Delta", kind="markers", shape="cross", position="value"),
        ]


def test_segment_values_resolve_start_bar_positions_and_drop_unresolved_starts():
    df = _sample_ohlcv_df(10)
    df["pivot"] = float("nan")
    df["pivot_bar"] = None
    df.iloc[5, df.columns.get_loc("pivot")] = 101.0
    df.iloc[5, df.columns.get_loc("pivot_bar")] = df.index[2]
    df.iloc[7, df.columns.get_loc("pivot")] = 99.0
    df.iloc[7, df.columns.get_loc("pivot_bar")] = pd.Timestamp("2020-01-01")  # not in the frame

    chart = serialize_chart_data(df, _SegmentStrategy())
    segment = next(ind for ind in chart["indicators"] if ind["key"] == "pivot")

    assert segment["kind"] == "segment"
    assert segment["start_index"][5] == 2
    assert segment["values"][5] == 101.0
    assert segment["values"][7] is None
    assert segment["start_index"][7] is None


def test_marker_series_carry_kind_and_no_start_index():
    df = _sample_ohlcv_df(10)
    df["pivot"] = float("nan")
    df["pivot_bar"] = None
    df["ma_delta"] = df["close"].diff()

    chart = serialize_chart_data(df, _SegmentStrategy())
    markers = next(ind for ind in chart["indicators"] if ind["key"] == "ma_delta")

    assert markers["kind"] == "markers"
    assert markers["shape"] == "cross"
    assert "start_index" not in markers


def test_line_series_defaults_keep_existing_wire_shape():
    df = _sample_ohlcv_df(10)
    chart = serialize_chart_data(
        df.assign(ma=df["close"]), MACrossoverStrategy(short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT")
    )
    for indicator in chart["indicators"]:
        assert "kind" not in indicator
        assert "start_index" not in indicator
