"""Вендорский канал YJX: кадры, политика «только чтение» и разбор ответов.

Железо в тестах не нужно: кадры проверяются побайтно, ответы — на
фикстурах. Политика важнее всего: ни один кадр записи не должен дойти до
транспорта.
"""

from __future__ import annotations

import pytest

from mousecfg.vendor import framing, guard
from mousecfg.vendor.device import VendorDevice

# -- кадры ---------------------------------------------------------------


def test_mkespn_identify_frame_is_64_bytes() -> None:
    frame = framing.mkespn_frame(0x03, 0x00)

    assert len(frame) == 64
    assert frame[:8] == bytes([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    assert frame[8:] == bytes(56)


def test_mkespn_frame_carries_length_offset_and_page() -> None:
    frame = framing.mkespn_frame(0x08, 0xA5, length=0x30, offset=0x3C, page=2)

    assert frame[1:8] == bytes([0x08, 0xA5, 0x00, 0x30, 0x3C, 0x00, 0x02])


def test_mkespn_frame_rejects_oversized_payload() -> None:
    with pytest.raises(framing.FrameError):
        framing.mkespn_frame(0x0D, 0x00, data=bytes(57))


def test_parse_mkespn_reply_reads_length_and_data() -> None:
    raw = bytearray(64)
    raw[0] = 0xAA
    raw[1] = 0x0E
    raw[2] = 0xA5
    raw[4] = 3
    raw[8:11] = b"\x01\x02\x03"

    reply = framing.parse_mkespn_reply(bytes(raw))

    assert reply is not None
    assert (reply.cmd, reply.sub) == (0x0E, 0xA5)
    assert reply.data == b"\x01\x02\x03"


def test_parse_mkespn_reply_ignores_foreign_frames() -> None:
    assert framing.parse_mkespn_reply(b"\x55\x03\x00" + bytes(61)) is None


def test_yjx_addressed_packet_layout_and_checksum() -> None:
    packet = framing.yjx_addressed_packet(0x08, 0x0BA5, 0x2C)

    assert packet[0] == 0xF0
    assert packet[1] == 0x08
    assert packet[2] == framing.YJX_MOUSE
    assert packet[4:8] == bytes([0x2C, 0xA5, 0x0B, 0x00])
    assert packet[3] == sum(packet[4:]) & 0xFF


def test_normalize_yjx_reply_strips_marker() -> None:
    assert framing.normalize_yjx_reply(b"\xf0\x04abc") == b"\x04abc"
    assert framing.normalize_yjx_reply(b"\x04abc") == b"\x04abc"


def test_printable_text_keeps_visible_ascii_only() -> None:
    assert framing.printable_text(b"YJX.co.mouse\x00\x02" + bytes([0x80]) + b"2.3") == "YJX.co.mouse2.3"


# -- политика ------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "sub"),
    [(0x03, 0x00), (0x08, 0xA5), (0x0E, 0xA5), (0x30, 0xA5), (0xFA, 0xAE)],
)
def test_mkespn_read_commands_are_allowed(cmd: int, sub: int) -> None:
    assert guard.ensure_read(guard.Dialect.MKESPN, cmd, sub)


@pytest.mark.parametrize("cmd", [0x09, 0x0F, 0x10, 0x22, 0x23])
def test_mkespn_write_commands_are_refused(cmd: int) -> None:
    with pytest.raises(guard.VendorRefused) as excinfo:
        guard.ensure_read(guard.Dialect.MKESPN, cmd, 0xA5)

    assert "память" in str(excinfo.value) or "memory" in str(excinfo.value)


def test_mkespn_memory_clear_command_is_refused_with_reason() -> None:
    with pytest.raises(guard.VendorRefused) as excinfo:
        guard.ensure_read(guard.Dialect.MKESPN, 0x0D, 0x00)

    message = str(excinfo.value)
    assert "0d/00" in message
    assert "onboard" in message or "памяти" in message


@pytest.mark.parametrize("cmd", [0x04, 0x08, 0x0E, 0x30, 0xED])
def test_yjx_read_commands_are_allowed(cmd: int) -> None:
    assert guard.ensure_read(guard.Dialect.YJX, cmd)


@pytest.mark.parametrize("cmd", [0x09, 0x0D, 0x0F, 0x10, 0x52])
def test_yjx_write_commands_are_refused(cmd: int) -> None:
    with pytest.raises(guard.VendorRefused):
        guard.ensure_read(guard.Dialect.YJX, cmd)


def test_unknown_command_is_refused() -> None:
    with pytest.raises(guard.VendorRefused):
        guard.ensure_read(guard.Dialect.MKESPN, 0x7F, 0x00)


# -- устройство ----------------------------------------------------------


class _FakeTransport:
    """Транспорт-заглушка: отдаёт заранее заданные ответы по команде."""

    def __init__(self, replies: dict[int, bytes]) -> None:
        self.replies = replies
        self.sent: list[bytes] = []
        self.path = "/dev/hidraw-test"
        self.drained = 0

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def drain(self) -> None:
        self.drained += 1

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def read(self, timeout: float | None = None) -> bytes | None:  # noqa: ARG002
        return None

    def exchange(self, frame: bytes, accept):  # noqa: ANN001, ANN201
        self.drain()
        self.send(frame)
        reply = self.replies.get(frame[1])
        if reply is None:
            return None
        return accept(reply)


def _mkespn_reply(cmd: int, sub: int, data: bytes, *, length: int | None = None) -> bytes:
    raw = bytearray(64)
    raw[0] = 0xAA
    raw[1] = cmd
    raw[2] = sub
    raw[4] = len(data) if length is None else length
    raw[8 : 8 + len(data)] = data
    return bytes(raw)


def _device_with(replies: dict[int, bytes]) -> tuple[VendorDevice, _FakeTransport]:
    transport = _FakeTransport(replies)
    device = VendorDevice("/dev/hidraw-test")
    device.transport = transport  # type: ignore[assignment]
    return device, transport


def test_probe_detects_mkespn_and_parses_identification() -> None:
    identify = _mkespn_reply(0x03, 0x00, b"YJX.co.mouse 2.3 213", length=0)
    device, transport = _device_with({0x03: identify})

    probe = device.probe()

    assert probe.dialect == "mkespn"
    assert probe.identification.startswith("YJX.co.mouse")
    assert probe.version == "2.3"
    # Первый кадр — определение диалекта, второй — сама идентификация.
    assert [frame[:2] for frame in transport.sent] == [bytes([0x55, 0x03]), bytes([0x55, 0x03])]


def test_probe_falls_back_to_yjx_dialect() -> None:
    ping = bytearray(64)
    ping[0] = 0xF0
    ping[1] = 0x04
    ping[2:7] = b"1.2.3"
    device, transport = _device_with({0x04: bytes(ping)})

    probe = device.probe()

    assert probe.dialect == "yjx"
    assert probe.version == "1.2.3"
    # Первым уходит пробный кадр MKESPN и не получает ответа, затем ping YJX.
    assert transport.sent[0][0] == 0x55
    assert transport.sent[1][0] == 0xF0
    assert transport.sent[1][1] == 0x04


def test_probe_reports_unknown_dialect() -> None:
    device, _transport = _device_with({})

    probe = device.probe()

    assert probe.dialect == "unknown"
    assert probe.identification == ""
    assert probe.notes


def test_deep_probe_sends_only_read_commands() -> None:
    replies = {
        0x03: _mkespn_reply(0x03, 0x00, b"YJX.co.mouse"),
        0xFA: _mkespn_reply(0xFA, 0xAE, bytes(range(8))),
        0x0E: _mkespn_reply(0x0E, 0xA5, bytes(range(0x30))),
        0x08: _mkespn_reply(0x08, 0xA5, bytes(range(0x30))),
        0x30: _mkespn_reply(0x30, 0xA5, bytes(range(8))),
    }
    device, transport = _device_with(replies)

    probe = device.probe(deep=True)

    commands = [frame[1] for frame in transport.sent]
    # Первый 0x03 — определение диалекта, затем идентификация и дампы.
    assert commands == [0x03, 0x03, 0xFA, 0x0E, 0x08, 0x30]
    assert all(cmd not in guard.MKESPN_WRITES for cmd in commands)
    labels = [response.label for response in probe.responses]
    assert any("DPI" in label for label in labels)


def test_deep_probe_yjx_stays_read_only() -> None:
    ping = bytearray(64)
    ping[0] = 0xF0
    ping[1] = 0x04
    battery = bytearray(64)
    battery[0] = 0xF0
    battery[1] = 0x30
    battery[7] = 77
    battery[8] = 1
    device, transport = _device_with({0x04: bytes(ping), 0x30: bytes(battery)})

    probe = device.probe(deep=True)

    yjx_frames = [frame for frame in transport.sent if frame[0] == 0xF0]
    assert yjx_frames[0][1] == 0x04
    assert all(frame[1] in guard.YJX_READS for frame in yjx_frames)
    assert all(frame[1] not in guard.YJX_WRITES for frame in yjx_frames)
    assert probe.dialect == "yjx"


def test_write_frame_never_reaches_transport() -> None:
    """Даже если кто-то попробует отправить запись — guard не пропустит."""
    device, transport = _device_with({})

    with pytest.raises(guard.VendorRefused):
        device._mkespn_query(0x0D, 0x00)  # noqa: SLF001

    assert transport.sent == []
