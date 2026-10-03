import pandas as pd

from q_backend.ml_filters.features import build_entry_features


def test_build_entry_features_uses_signal_bar_and_preserves_feature_order():
    frame = pd.DataFrame(
        {
            "open": [10.0, 900.0],
            "high": [11.0, 999.0],
            "low": [9.0, 1.0],
            "close": [10.5, 950.0],
            "tick_volume": [12, 9999],
            "e0_ma_short": [10.2, 940.0],
            "e0_ma_long": [10.1, 930.0],
        },
        index=pd.to_datetime(["2026-01-02T12:00:00Z", "2026-01-02T12:05:00Z"]),
    )

    actual = build_entry_features(frame.iloc[:1], [1], ["close", "ma_short", "side"])

    assert list(actual.columns) == ["close", "ma_short", "side"]
    assert actual.iloc[0].to_dict() == {"close": 10.5, "ma_short": 10.2, "side": 1}


def test_build_entry_features_is_prefix_invariant_and_handles_short_sides():
    frame = pd.DataFrame({"close": [1.0, 2.0, 999.0]})
    prefix = build_entry_features(frame.iloc[:2], [1, -1], ["close", "side"])
    extended = build_entry_features(frame, [1, -1, 1], ["close", "side"])

    pd.testing.assert_frame_equal(prefix, extended.iloc[:2])
    assert extended["side"].tolist() == [1, -1, 1]


def test_build_entry_features_reports_missing_allowlisted_input():
    frame = pd.DataFrame({"close": [1.0]})

    try:
        build_entry_features(frame, [1], ["real_volume", "side"])
    except ValueError as exc:
        assert "real_volume" in str(exc)
    else:
        raise AssertionError("missing selected feature must be rejected")
