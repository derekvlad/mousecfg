"""Вендорский канал YJX: кадры, политика «только чтение» и разбор ответов.

Железо в тестах не нужно: кадры проверяются побайтно, ответы — на
фикстурах. Политика важнее всего: ни один кадр записи не должен дойти до
транспорта.
"""

from __future__ import annotations

import argparse

import pytest

from mousecfg import discovery
from mousecfg.vendor import device as device_module
from mousecfg.vendor import framing, guard, parsers
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
    # 0x03 — диалект, затем идентификация, конфигурация и заряд.
    assert [frame[:2] for frame in transport.sent] == [
        bytes([0x55, 0x03]),
        bytes([0x55, 0x03]),
        bytes([0x55, 0x0E]),
        bytes([0x55, 0x30]),
    ]
    assert probe.device is not None


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
        0x30: _mkespn_reply(0x30, 0xA5, b"\x51" + bytes(9)),
    }
    device, transport = _device_with(replies)

    probe = device.probe(deep=True)

    commands = [frame[1] for frame in transport.sent]
    # 0x03 — диалект; затем идентификация, конфигурация, заряд и deep-дампы.
    assert commands == [0x03, 0x03, 0x0E, 0x30, 0xFA, 0x08, 0x08, 0x08]
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


# -- разбор живого ответа ------------------------------------------------

# Кадр, снятый с реальной мыши AJAZZ 2.4G (a8a5:2255), диалект mkespn.
REAL_IDENT = "YJX.co.mouse271220Nov 12 202518:49:27Feb 20 202420:18:19"
REAL_CONFIG = bytes.fromhex(
    "00 00 04 06 05 90 01 20 03 b0 04 40 06 60 09 80 0c"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 ff 01 0a 0a ff 4d 00 00"
)


def _reply_with_data(cmd: int, sub: int, data: bytes) -> framing.MkespnReply:
    raw = _mkespn_reply(cmd, sub, data)
    reply = framing.parse_mkespn_reply(raw)
    assert reply is not None
    return reply


def test_parse_identification_splits_model_code_and_dates() -> None:
    info = parsers.parse_identification(REAL_IDENT)

    assert info.model == "YJX.co.mouse"
    assert info.code == "271220"
    assert info.firmware_dates == ["Nov 12 202518:49:27", "Feb 20 202420:18:19"]
    assert "YJX.co.mouse" in info.describe()


def test_parse_dpi_config_matches_the_live_mouse() -> None:
    config = parsers.parse_dpi_config(_reply_with_data(0x0E, 0xA5, REAL_CONFIG))

    assert config.levels == [400, 800, 1200, 1600, 2400, 3200]
    assert config.current_dpi == 2400
    assert config.poll_rate_hz == 1000
    assert config.dpi_count == 6
    assert config.tail.startswith("01 0a")


def test_parse_dpi_config_survives_short_reply() -> None:
    config = parsers.parse_dpi_config(_reply_with_data(0x0E, 0xA5, b"\x00\x00"))

    assert config.levels == []
    assert config.current_dpi == 0
    assert config.describe()


def test_parse_session_state_keeps_raw_bytes() -> None:
    state = parsers.parse_session_state(_reply_with_data(0x30, 0xA5, b"\x4d\x00\x00"))

    assert state.data == "4d 00 00"
    assert state.first_byte == 0x4D


# -- CLI: карточка мыши без HID++ ---------------------------------------


def _evdev_mouse() -> discovery.MouseInfo:
    return discovery.MouseInfo(
        key="serial:1",
        name="YJX-CHIP AJAZZ 2.4G Mouse",
        serial="1",
        usb_id="a8a5:2255",
        usb_address="1-2",
        vendor_id=0xA8A5,
        product_id=0x2255,
        protocol="evdev",
        family="ajazz-2.4g",
        hidraw_path="/dev/hidraw4",
        input_path="/dev/input/event20",
        interface_number=0,
        capabilities=discovery.EVDEV_CAPABILITIES | {discovery.VENDOR_READ_CAPABILITY},
        vendor_path="/dev/hidraw6",
    )


class _FakeCliDevice:
    def __init__(self, _path: str) -> None:
        self.probe_result = parsers.DeviceIdentification(raw="")  # placeholder

    def __enter__(self) -> _FakeCliDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def probe(self, *, deep: bool = False):  # noqa: ARG002
        probe = device_module.VendorProbe(path="/dev/hidraw6", dialect="mkespn")
        probe.device = parsers.parse_identification("YJX.co.mouse271220Nov 12 202518:49:27")
        probe.dpi = parsers.DpiConfig(levels=[400, 800, 1600], current_level=2, poll_rate_hz=1000, dpi_count=3)
        probe.session = parsers.SessionState(data="51 00 00 00 00 00 00 00 00 00")
        return probe


def test_get_for_evdev_mouse_reports_vendor_dpi(monkeypatch, capsys) -> None:
    from mousecfg import cli

    monkeypatch.setattr(cli.discovery, "mouse_for_hint", lambda _hint: _evdev_mouse())
    monkeypatch.setattr(device_module, "VendorDevice", _FakeCliDevice)
    args = argparse.Namespace(device=None, json=False, dry_run=False, allow_onboard_switch=False, verbose=0)

    assert cli.cmd_get(args) == 0

    out = capsys.readouterr().out
    assert "dpi: 1600" in out
    assert "report_rate_hz: 1000" in out
    assert "battery_percent: 81" in out
    assert "только чтение" in out


def test_get_for_evdev_mouse_survives_missing_udev_access(monkeypatch, capsys) -> None:
    from mousecfg import cli

    class _Denied:
        def __init__(self, _path: str) -> None:
            raise PermissionError("нет доступа")

    monkeypatch.setattr(cli.discovery, "mouse_for_hint", lambda _hint: _evdev_mouse())
    monkeypatch.setattr(device_module, "VendorDevice", _Denied)
    args = argparse.Namespace(device=None, json=False, dry_run=False, allow_onboard_switch=False, verbose=0)

    assert cli.cmd_get(args) == 0

    out = capsys.readouterr().out
    assert "udev" in out  # подсказка про правило, а не падение


def test_judge_lists_the_vendor_read_only_policy(capsys) -> None:
    from mousecfg import cli

    args = argparse.Namespace(json=False)
    assert cli.cmd_judge(args) == 0

    out = capsys.readouterr().out
    assert "только чтение" in out
    assert "03/00" in out
    assert "0d/*" in out


def test_parse_session_state_reports_battery_within_range() -> None:
    state = parsers.parse_session_state(_reply_with_data(0x30, 0xA5, b"\x51" + bytes(9)))

    assert state.percent == 0x51 == 81
    assert state.first_byte == 81


def test_parse_session_state_rejects_out_of_range_value() -> None:
    state = parsers.parse_session_state(_reply_with_data(0x30, 0xA5, b"\xff" + bytes(9)))

    assert state.percent is None
    assert state.first_byte == 0xFF


def test_parse_info_block_joins_pages_and_finds_timestamp() -> None:
    page5 = bytes.fromhex(
        "ff ff ff ff ff ff ff 01"
        " 00 00 00 32 30 32 35 30 39 30 33 31 37 30 31 30 35"
        " 2d 33 32 39 39 2d 33 33 31 31 00 00 ff ff ff"
    )
    page6 = bytes.fromhex("ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff") * 2

    info = parsers.parse_info_block({5: page5, 6: page6})

    assert "20250903170105-3299-3311" in info.text
    assert info.timestamp == "2025-09-03 17:01:05"


def test_read_battery_percent_uses_only_read_commands(monkeypatch) -> None:
    identify = _mkespn_reply(0x03, 0x00, b"YJX.co.mouse")
    battery = _mkespn_reply(0x30, 0xA5, b"\x51" + bytes(9))
    device, transport = _device_with({0x03: identify, 0x30: battery})
    monkeypatch.setattr(device_module, "VendorDevice", lambda *_a, **_k: _as_device(device))

    percent = device_module.read_battery_percent("/dev/hidraw-test")

    assert percent == 81
    assert all(cmd not in guard.MKESPN_WRITES for cmd in (frame[1] for frame in transport.sent))
    assert [frame[1] for frame in transport.sent] == [0x03, 0x30]


def _as_device(device: VendorDevice) -> VendorDevice:
    """Заглушка-контекст: настоящий `VendorDevice` с подменённым транспортом."""

    class _Ctx:
        def __enter__(self) -> VendorDevice:
            return device

        def __exit__(self, *exc: object) -> None:
            return None

    return _Ctx()  # type: ignore[return-value]


def test_battery_ignores_small_noise() -> None:
    from mousecfg.daemon import _smooth_battery

    # Шум ±2% не должен дёргать показания.
    assert _smooth_battery(None, 80) == 80
    assert _smooth_battery(80, 81) == 80
    assert _smooth_battery(80, 78) == 80
    assert _smooth_battery(80, 82) == 80


def test_battery_accepts_real_change_without_delay() -> None:
    from mousecfg.daemon import BATTERY_NOISE, _smooth_battery

    # Заметное изменение показывается сразу, а не через минуты (как было с
    # медианой из 5 измерений — из-за неё GUI/трей отставали).
    assert _smooth_battery(75, 100) == 100
    assert _smooth_battery(100, 60) == 60
    assert _smooth_battery(50, 50 + BATTERY_NOISE + 1) == 50 + BATTERY_NOISE + 1


def test_battery_change_of_exactly_the_noise_threshold_is_held() -> None:
    from mousecfg.daemon import BATTERY_NOISE, _smooth_battery

    assert _smooth_battery(90, 90 - BATTERY_NOISE) == 90


def test_vendor_command_shows_battery_and_reads_only(monkeypatch, capsys) -> None:
    from mousecfg import cli

    monkeypatch.setattr(cli.discovery, "mouse_for_hint", lambda _hint: _evdev_mouse())
    monkeypatch.setattr(device_module, "VendorDevice", _FakeCliDevice)
    args = argparse.Namespace(device=None, json=False, probe=False, dry_run=False, allow_onboard_switch=False, verbose=0)

    assert cli.cmd_vendor(args) == 0

    out = capsys.readouterr().out
    assert "DPI: 1600" in out
    assert "Заряд: 81%" in out
    assert "только чтение" in out
