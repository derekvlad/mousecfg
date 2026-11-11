"""Тесты сборки и разбора кадров HID++ 2.0."""

from __future__ import annotations

import pytest

from mousecfg.hidpp import protocol


def test_short_frame_layout():
    frame = protocol.build_frame(0xFF, 0x0A, 0x20, b"\x00")
    assert frame[0] == protocol.SHORT_REPORT_ID
    assert frame[1] == 0xFF
    assert frame[2] == 0x0A
    assert frame[3] == 0x20 | protocol.SOFTWARE_ID
    assert frame[4] == 0x00
    assert len(frame) == protocol.SHORT_FRAME_SIZE


def test_long_frame_when_params_too_big():
    frame = protocol.build_frame(0xFF, 0x0A, 0x30, bytes(range(10)))
    assert frame[0] == protocol.LONG_REPORT_ID
    assert len(frame) == protocol.LONG_FRAME_SIZE


def test_ping_frame_matches_expected_layout():
    frame = protocol.build_ping_frame(0xFF, mark=0x5A)
    assert frame == bytes(
        (0x10, 0xFF, 0x00, 0x10 | protocol.SOFTWARE_ID, 0x00, 0x00, 0x5A)
    )


def test_parse_frame_roundtrip():
    frame = protocol.build_frame(0xFF, 0x0A, 0x20, b"\x01\x02")
    reply = protocol.parse_frame(frame)
    assert reply.device_index == 0xFF
    assert reply.feature_index == 0x0A
    assert reply.function_index == 0x02
    assert not reply.is_error


def test_parse_error_frame():
    raw = bytes((0x11, 0xFF, 0xFF, 0x08)) + bytes(16)
    reply = protocol.parse_frame(raw)
    assert reply.is_error
    assert reply.error_code == 0x08


def test_build_frame_rejects_bad_function():
    with pytest.raises(ValueError):
        protocol.build_frame(0xFF, 0x0A, 0x25)


def test_parse_frame_rejects_wrong_length():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_frame(bytes((0x10, 0xFF, 0x00, 0x10)))


def test_notifications_do_not_count_as_replies():
    notification = bytes((0x11, 0xFF, 0x0A, 0x00)) + bytes(16)
    assert protocol.is_notification(notification)
    reply = bytes((0x11, 0xFF, 0x0A, 0x20 | protocol.SOFTWARE_ID)) + bytes(16)
    assert not protocol.is_notification(reply)
