"""Тесты транспорта и канала отправки — без реального устройства.

Используется фиктивный hidraw: обычный os.pipe через monkeypatch
select/os.read/os.write.
"""

from __future__ import annotations

import os

import pytest

from mousecfg.hidpp import protocol
from mousecfg.hidpp.guard import DeniedCommand
from mousecfg.hidpp.safeplane import Journal, SafePlane
from mousecfg.hidpp.transport import HidrawTransport


class FakeTransport:
    def __init__(self, replies: list[bytes] | None = None, *, raise_timeout: bool = False) -> None:
        self.sent: list[bytes] = []
        self.replies = list(replies or [])
        self.raise_timeout = raise_timeout

    def drain(self) -> None:
        pass

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def expect_reply(self, timeout: float = 1.0) -> bytes:  # noqa: ARG002
        if self.replies:
            return self.replies.pop(0)
        if self.raise_timeout:
            raise TimeoutError("устройство молчит")
        raise TimeoutError("нет ответов")


def test_safeplane_blocks_persistent_before_send():
    transport = FakeTransport()
    plane = SafePlane(transport)  # type: ignore[arg-type]
    frame = protocol.build_frame(0xFF, 0x00, 0x60, b"\x00")

    with pytest.raises(DeniedCommand):
        plane.send(protocol.FeatureId.ONBOARD_PROFILES, 0x60, b"\x00", frame)
    assert transport.sent == []


def test_safeplane_allows_volatile_and_journals(tmp_path):
    transport = FakeTransport()
    journal = Journal(tmp_path / "hid.log")
    plane = SafePlane(transport, journal=journal)  # type: ignore[arg-type]
    frame = protocol.build_frame(0xFF, 0x0A, 0x30, b"\x00")

    plane.send(protocol.FeatureId.ADJUSTABLE_DPI, 0x30, b"\x00", frame)
    assert transport.sent == [frame]
    assert len(journal.entries) == 1
    assert journal.entries[0].classification == "volatile"
    assert (tmp_path / "hid.log").read_text(encoding="utf-8").strip()


def test_safeplane_consent_required_for_mode_switch():
    transport = FakeTransport()
    frame = protocol.build_frame(0xFF, 0x00, 0x10, b"\x02")

    strict = SafePlane(transport)  # type: ignore[arg-type]
    with pytest.raises(DeniedCommand):
        strict.send(protocol.FeatureId.ONBOARD_PROFILES, 0x10, b"\x02", frame)
    assert transport.sent == []

    agreed = SafePlane(transport, allow_onboard_switch=True)  # type: ignore[arg-type]
    agreed.send(protocol.FeatureId.ONBOARD_PROFILES, 0x10, b"\x02", frame)
    assert transport.sent == [frame]
    assert agreed.journal.persistent_count() == 1


def test_dry_run_does_not_write():
    transport = FakeTransport()
    plane = SafePlane(transport, dry_run=True)  # type: ignore[arg-type]
    frame = protocol.build_frame(0xFF, 0x0A, 0x30, b"\x00")
    plane.send(protocol.FeatureId.ADJUSTABLE_DPI, 0x30, b"\x00", frame)
    assert transport.sent == []
    assert plane.journal.entries  # в журнале команда всё равно видна


def test_transport_skips_mouse_input_reports(monkeypatch):
    """Обычные input-отчёты мыши (0x01) не должны считаться ответом."""
    read_fd, write_fd = os.pipe()
    try:
        transport = HidrawTransport("/dev/null")
        object.__setattr__(transport, "_fd", read_fd)  # тест транспортного слоя, без hidraw

        os.write(write_fd, bytes((0x01, 0, 0, 0, 0, 0, 0)))
        os.write(write_fd, bytes(20))

        # Ответ приходит асинхронно: пишем его после того, как клиент начал ждать.
        expected = bytes((0x11, 0xFF, 0x0A, 0x30 | protocol.SOFTWARE_ID)) + bytes(16)

        def send_when_waiting() -> None:
            os.write(write_fd, expected)

        import threading

        threading.Timer(0.1, send_when_waiting).start()
        reply = transport.expect_reply(timeout=2.0)
        assert reply == expected
    finally:
        os.close(read_fd)
        os.close(write_fd)
