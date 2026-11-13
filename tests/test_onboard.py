"""Запись в onboard-память: кадры, политика, снимки.

Мышь в тестах не нужна. Главное здесь — гарантии: без явного согласия
кадр записи не уходит, снимок снимается до записи, а команда стирания
памяти не отправляется никогда.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from mousecfg import discovery, onboard_consent
from mousecfg.vendor import device as device_module
from mousecfg.vendor import framing, guard, snapshot
from mousecfg.vendor.parsers import OnboardConfig

# Живой конфиг этой мыши (47 байт, как отдаёт чтение).
LIVE_CONFIG = bytes.fromhex(
    "000504060690012003b004400660098c"
    "0a000000000000000000000000000000"
    "000000000000000000ff010a0aff4f"
)
assert len(LIVE_CONFIG) == 47


# -- кадр записи ---------------------------------------------------------


def test_config_write_frame_is_48_bytes_and_matches_the_web_tool() -> None:
    frame = framing.mkespn_config_write_frame(LIVE_CONFIG)

    assert len(frame) == 64
    assert frame[:8] == bytes([0x55, 0x0F, 0xAE, 0x0B, 0x30, 0x01, 0x01, 0x01])
    # 47 байт данных + один дополняющий ноль до размера буфера 0x30.
    assert frame[8:8 + 48] == LIVE_CONFIG + b"\x00"


def test_config_write_frame_rejects_empty_input() -> None:
    with pytest.raises(framing.FrameError):
        framing.mkespn_config_write_frame(b"")


def test_keys_write_frame_header() -> None:
    keys = bytes([0x20, 0x01, 0x00, 0x00]) * 5

    frame = framing.mkespn_keys_write_frame(keys)

    assert frame[1] == 0x09
    assert frame[2] == 0xA5
    assert frame[4] == 0x22
    assert frame[9:29] == keys


# -- политика записи -----------------------------------------------------


@pytest.mark.parametrize("cmd", [0x0F, 0x09])
def test_consented_write_commands_are_allowed(cmd: int) -> None:
    assert guard.ensure_onboard_write(cmd, 0xAE if cmd == 0x0F else 0xA5)


def test_memory_erase_command_is_never_allowed() -> None:
    with pytest.raises(guard.VendorWriteRefused) as excinfo:
        guard.ensure_onboard_write(0x0D, 0x00)

    message = str(excinfo.value)
    assert "0d/00" in message
    assert "памят" in message or "memory" in message


def test_unknown_write_command_is_refused() -> None:
    with pytest.raises(guard.VendorWriteRefused):
        guard.ensure_onboard_write(0x10, 0xA5)


# -- конфигурация как объект --------------------------------------------


def test_onboard_config_reads_and_writes_fields() -> None:
    config = OnboardConfig.from_bytes(LIVE_CONFIG)

    assert config.dpi_levels == [400, 800, 1200, 1600, 2400, 2700]
    assert config.dpi_index == 5
    assert config.report_rate_code == 4
    assert config.light_mode == 5

    config.set_level(5, 2400)
    config.light_mode = 1
    assert config.dpi_levels[5] == 2400
    assert config.light_mode == 1


def test_onboard_config_set_level_validates() -> None:
    config = OnboardConfig.from_bytes(LIVE_CONFIG)

    with pytest.raises(ValueError):
        config.set_level(9, 800)
    with pytest.raises(ValueError):
        config.set_level(0, 10)


def test_onboard_config_pads_short_input() -> None:
    config = OnboardConfig.from_bytes(b"\x00\x01")

    assert len(config.to_bytes()) == 48


def test_changes_against_reports_only_real_differences() -> None:
    before = OnboardConfig.from_bytes(LIVE_CONFIG)
    after = OnboardConfig.from_bytes(LIVE_CONFIG)
    after.set_level(5, 2400)
    after.report_rate_code = 3

    changes = after.changes_against(before)

    assert "dpi_levels" in changes
    assert "report_rate" in changes
    assert "light_mode" not in changes


# -- снимки --------------------------------------------------------------


def _reply(data: bytes) -> framing.MkespnReply:
    raw = bytearray(64)
    raw[0] = 0xAA
    raw[1] = 0x0E
    raw[2] = 0xA5
    raw[4] = len(data)
    raw[8 : 8 + len(data)] = data
    reply = framing.parse_mkespn_reply(bytes(raw))
    assert reply is not None
    return reply


def test_snapshot_round_trips_through_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mousecfg.config.backups_dir", lambda: tmp_path)
    snap = snapshot.snapshot_from_reads(_reply(LIVE_CONFIG), device="Mouse", serial="S1", usb_id="a8a5:2255")

    path = snapshot.save(snap)
    loaded = snapshot.load_latest(usb_id="a8a5:2255")

    assert path.exists()
    assert loaded is not None
    assert loaded.config_bytes == LIVE_CONFIG
    assert loaded.decoded["dpi_levels"] == [400, 800, 1200, 1600, 2400, 2700]
    assert loaded.fingerprint() == snap.fingerprint()


def test_snapshot_fingerprint_changes_with_data() -> None:
    first = snapshot.snapshot_from_reads(_reply(LIVE_CONFIG))
    other = bytearray(LIVE_CONFIG)
    other[13] = 0xFF
    second = snapshot.snapshot_from_reads(_reply(bytes(other)))

    assert first.fingerprint() != second.fingerprint()


# -- CLI: согласие и бэкап ----------------------------------------------


def _evdev_mouse() -> discovery.MouseInfo:
    return discovery.MouseInfo(
        key="serial:S1",
        name="YJX-CHIP AJAZZ 2.4G Mouse",
        serial="S1",
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


class _FakeDevice:
    """Вендорский узел-заглушка: отдаёт конфиг и считает записанное."""

    def __init__(self, _path: str, **_kwargs: object) -> None:
        self.written: list[bytes] = []

    def __enter__(self) -> _FakeDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read_config(self):  # noqa: ANN201
        return _reply(LIVE_CONFIG)

    def read_keys(self):  # noqa: ANN201
        return None

    def write_config(self, config: bytes, *, dry_run: bool = False) -> bytes:  # noqa: ARG002
        self.written.append(bytes(config))
        frame = bytearray(64)
        frame[0] = 0xAA
        frame[1] = 0x0F
        return bytes(frame)


def test_apply_without_flag_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    from mousecfg import cli

    devices: list[_FakeDevice] = []

    def factory(path: str, **kwargs: object) -> _FakeDevice:
        device = _FakeDevice(path, **kwargs)
        devices.append(device)
        return device

    monkeypatch.setattr(cli.discovery, "find_mice", lambda: [_evdev_mouse()])
    monkeypatch.setattr(device_module, "VendorDevice", factory)
    monkeypatch.setattr("mousecfg.config.backups_dir", lambda: tmp_path / "backups")
    args = argparse.Namespace(
        device=None, json=False, verbose=0, dry_run=False, allow_onboard_switch=False,
        onboard_command="apply", dpi=2400, dpi_level=5, rate=None, light_mode=None,
        lod=None, sleep_light=None, key_respond=None, highspeed=None,
        allow_onboard_write=False, yes=True,
    )

    assert cli.cmd_onboard_apply(args) == 3
    assert all(device.written == [] for device in devices)
    assert "allow-onboard-write" in capsys.readouterr().err


def test_apply_writes_and_takes_a_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    from mousecfg import cli

    backups = tmp_path / "backups"
    devices: list[_FakeDevice] = []

    def factory(path: str, **kwargs: object) -> _FakeDevice:
        device = _FakeDevice(path, **kwargs)
        devices.append(device)
        return device

    monkeypatch.setattr(cli.discovery, "find_mice", lambda: [_evdev_mouse()])
    monkeypatch.setattr(device_module, "VendorDevice", factory)
    monkeypatch.setattr("mousecfg.config.backups_dir", lambda: backups)
    monkeypatch.setattr("mousecfg.onboard_consent.granted", lambda _key: True)
    args = argparse.Namespace(
        device=None, json=False, verbose=0, dry_run=False, allow_onboard_switch=False,
        onboard_command="apply", dpi=2400, dpi_level=5, rate=None, light_mode=None,
        lod=None, sleep_light=None, key_respond=None, highspeed=None,
        allow_onboard_write=True, yes=True,
    )

    assert cli.cmd_onboard_apply(args) == 0

    # Снимок снят до записи и содержит исходное значение.
    saved = list(backups.glob("onboard-*.json"))
    assert saved
    payload = json.loads(backups.joinpath("onboard-a8a5-2255-last.json").read_text(encoding="utf-8"))
    assert bytes.fromhex(payload["config_frame"]) == LIVE_CONFIG
    # В мышь ушёл буфер с новым уровнем (0-based индекс 5).
    written = next(device.written[0] for device in devices if device.written)
    assert len(written) == 48
    assert written[5 + 5 * 2] | (written[6 + 5 * 2] << 8) == 2400
    assert "2400" in capsys.readouterr().out


def test_consent_warning_shown_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr("mousecfg.config.onboard_consent_file", lambda: tmp_path / "onboard-consent.json")
    monkeypatch.setattr("mousecfg.onboard_consent.config.onboard_consent_file", lambda: tmp_path / "onboard-consent.json", raising=False)
    mouse = _evdev_mouse()
    onboard_consent.forget()

    from mousecfg import cli

    assert cli._onboard_confirm(mouse, allow_write=True, assume_yes=True) is True  # noqa: SLF001
    first = capsys.readouterr().out
    assert "ВНИМАНИЕ" in first or "WARNING" in first
    assert onboard_consent.granted(mouse.key) is True

    # Второй раз предупреждение не повторяется.
    assert cli._onboard_confirm(mouse, allow_write=True, assume_yes=True) is True  # noqa: SLF001
    assert capsys.readouterr().out.strip() == ""
