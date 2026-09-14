import json
import struct

import pytest

from q_backend.streaming.ws.frames import FrameError, binary_frame, control_frame, parse_client_frame


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


@pytest.mark.parametrize("cursor", ["garbage", "1-2-3", "-1", "", 17])
def test_parse_client_frame_rejects_a_malformed_cursor(cursor):
    """A malformed cursor reaching XREAD fails the read and closes the socket as a Redis outage."""
    with pytest.raises(FrameError, match="cursor"):
        parse_client_frame(json.dumps({"topics": ["quotes"], "cursors": {"quotes": cursor}}))


def test_parse_client_frame_accepts_a_millisecond_only_cursor():
    assert parse_client_frame('{"topics":["quotes"],"cursors":{"quotes":"171"}}').cursors == {"quotes": "171"}


def test_control_frame_carries_the_contract_type_discriminator():
    """Clients classify text frames by `type`; a frame without it would be read as an envelope."""
    assert control_frame("lagging", topic="bars.completed", from_seq=3) == {
        "type": "lagging",
        "topic": "bars.completed",
        "from_seq": 3,
    }
    assert control_frame("epoch_changed", topic="quotes", new_epoch="e2", previous_epoch=None) == {
        "type": "epoch_changed",
        "topic": "quotes",
        "new_epoch": "e2",
    }


def test_control_frame_rejects_fields_outside_the_contract():
    with pytest.raises(TypeError):
        control_frame("lagging", topic="quotes", from_seq=1, extra=True)
