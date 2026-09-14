import json
import struct

import pytest

from q_backend.streaming.ws.frames import FrameError, binary_frame, parse_client_frame


def test_binary_frame_has_q009_little_endian_header_and_raw_payload():
    """Removing the length prefix or base64-encoding payloads breaks binary delivery."""
    header = {"topic": "quotes", "seq": 7, "payload_kind": "arrow_ipc"}
    payload = b"0123456789"

    frame = binary_frame(header, payload)
    header_len = struct.unpack("<I", frame[:4])[0]

    assert header_len == len(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    assert json.loads(frame[4 : 4 + header_len]) == header
    assert frame[4 + header_len :] == payload


def test_parse_client_frame_rejects_an_empty_subscription():
    """Allowing an empty subscription admits an unusable client session."""
    with pytest.raises(FrameError, match="topics"):
        parse_client_frame('{"topics": []}')


def test_parse_client_frame_preserves_resume_cursors():
    """Dropping cursors would silently turn a requested resume into live-only delivery."""
    frame = parse_client_frame('{"topics":["quotes"],"cursors":{"quotes":"171-0"}}')

    assert frame.topics == ["quotes"]
    assert frame.cursors == {"quotes": "171-0"}
