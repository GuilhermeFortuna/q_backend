from q_backend.streaming.ws.session import _stream_id_before


def test_stream_cursor_comparison_is_numeric_not_lexicographic():
    """A lexical comparison falsely treats Redis ID 10-0 as older than 9-0."""
    assert not _stream_id_before("10-0", "9-0")
    assert _stream_id_before("8-99", "9-0")
