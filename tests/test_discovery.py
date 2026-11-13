"""Discovery: HID++-мыши и мыши, доступные только через evdev.

Собираем фальшивое sysfs-дерево: у одной физической мыши несколько
интерфейсов hidraw, у каждого — свой набор evdev-узлов. Проверяем, что
группировка идёт по устройству, а протокол определяется по интерфейсу 1
только у Logitech.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mousecfg import discovery

# Значения capabilities/* ровно в том виде, в каком их печатает ядро:
# старшее слово первым. 0xffff0000 в слове 4 — это BTN_LEFT..BTN_TASK.
POINTER_KEYS = "ffff0000 0 0 0 0"
POINTER_RELS = "1943"  # REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, *_HI_RES
NO_ABS = "0"

# Вендорский интерфейс AJAZZ: usage page 0xFF01, 64-байтные in/out.
VENDOR_DESCRIPTOR = bytes.fromhex(
    "06 01 ff 09 10 a1 01 15 00 26 ff 00 19 01 29 08"
    "95 40 75 08 81 02 19 01 29 08 91 02 c0"
)
# Обычная мышь тоже держит вендорскую страницу, но другую — 0xFFA5.
MOUSE_DESCRIPTOR = bytes.fromhex("05 01 09 02 a1 01 85 02 09 01 a1 00 06 a5 ff 09 88 a1 01")


def _interface(
    root: Path,
    *,
    hidraw: str,
    vid: int,
    pid: int,
    interface: int,
    hid_name: str,
    serial: str = "",
    usb: str = "1-2",
    pointer: bool = False,
    pointer_name: str = "",
    pointer_keys: str = POINTER_KEYS,
    event: str = "",
    vendor: bool = False,
) -> None:
    usb_dir = root / "sys" / "usb1" / usb / f"{usb}:1.{interface}"
    usb_dir.mkdir(parents=True)
    (usb_dir / "bInterfaceNumber").write_text(f"{interface:02x}", encoding="utf-8")

    uevent = [
        "DRIVER=hid-generic",
        f"HID_ID=0003:{vid:04X}:{pid:04X}",
        f"HID_NAME={hid_name}",
        f"HID_PHYS=usb-0000:00:14.0-1/{usb}",
    ]
    if serial:
        uevent.append(f"HID_UNIQ={serial}")
    (usb_dir / "uevent").write_text("\n".join(uevent) + "\n", encoding="utf-8")

    # Как в sysfs: /sys/class/hidraw/hidrawN/device — HID-узел внутри интерфейса,
    # поэтому bInterfaceNumber находится среди его родителей.
    hid_dir = usb_dir / "hid"
    hid_dir.mkdir()
    (hid_dir / "uevent").write_text("\n".join(uevent) + "\n", encoding="utf-8")

    if pointer:
        assert event, "для мышиного интерфейса нужен event-узел"
        input_dir = hid_dir / "input" / "input10"
        (input_dir / "capabilities").mkdir(parents=True)
        (input_dir / "name").write_text(pointer_name, encoding="utf-8")
        (input_dir / "capabilities" / "key").write_text(pointer_keys, encoding="utf-8")
        (input_dir / "capabilities" / "rel").write_text(POINTER_RELS, encoding="utf-8")
        (input_dir / "capabilities" / "abs").write_text(NO_ABS, encoding="utf-8")
        (input_dir / event).write_text("", encoding="utf-8")

    descriptor = VENDOR_DESCRIPTOR if vendor else MOUSE_DESCRIPTOR
    (hid_dir / "report_descriptor").write_bytes(descriptor)

    node = root / "hidraw" / hidraw
    node.mkdir(parents=True)
    (node / "device").symlink_to(hid_dir)


@pytest.fixture
def fake_sysfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(discovery, "HIDRAW_CLASS", tmp_path / "hidraw")
    (tmp_path / "hidraw").mkdir()
    return tmp_path


def _logitech_g102(root: Path) -> None:
    _interface(
        root,
        hidraw="hidraw1",
        vid=0x046D,
        pid=0xC092,
        interface=0,
        hid_name="Logitech G102 LIGHTSYNC Gaming Mouse",
        serial="205C35585247",
        pointer=True,
        pointer_name="Logitech G102 LIGHTSYNC Gaming Mouse",
        event="event3",
    )
    _interface(
        root,
        hidraw="hidraw2",
        vid=0x046D,
        pid=0xC092,
        interface=1,
        hid_name="Logitech G102 LIGHTSYNC Gaming Mouse",
        serial="205C35585247",
    )


def test_logitech_uses_hidpp_interface_and_evdev_pointer(fake_sysfs: Path) -> None:
    _logitech_g102(fake_sysfs)

    mice = discovery.find_mice()

    assert len(mice) == 1
    mouse = mice[0]
    assert mouse.protocol == "hidpp"
    assert mouse.supports_hidpp
    assert mouse.hidraw_path == "/dev/hidraw2"  # interface 1
    assert mouse.input_path == "/dev/input/event3"  # interface 0
    assert mouse.serial == "205C35585247"
    assert {"dpi", "report_rate", "lighting"} <= mouse.capabilities


def test_non_logitech_mouse_is_evdev_only(fake_sysfs: Path) -> None:
    """AJAZZ: вендорский протокол, HID++ нет — только evdev."""
    _interface(
        fake_sysfs,
        hidraw="hidraw4",
        vid=0xA8A5,
        pid=0x2255,
        interface=0,
        hid_name="YJX-CHIP AJAZZ 2.4G",
        serial="D1K_251010b271_A72V1",
        pointer=True,
        pointer_name="YJX-CHIP AJAZZ 2.4G Mouse",
        event="event20",
    )
    _interface(
        fake_sysfs,
        hidraw="hidraw5",
        vid=0xA8A5,
        pid=0x2255,
        interface=1,
        hid_name="YJX-CHIP AJAZZ 2.4G",
        serial="D1K_251010b271_A72V1",
    )
    _interface(
        fake_sysfs,
        hidraw="hidraw6",
        vid=0xA8A5,
        pid=0x2255,
        interface=2,
        hid_name="YJX-CHIP AJAZZ 2.4G",
        serial="D1K_251010b271_A72V1",
        vendor=True,
    )

    mice = discovery.find_mice()

    assert len(mice) == 1
    mouse = mice[0]
    assert mouse.protocol == "evdev"
    assert not mouse.supports_hidpp
    assert mouse.family == "ajazz-2.4g"
    assert mouse.hidraw_path == "/dev/hidraw4"
    assert mouse.input_path == "/dev/input/event20"
    assert mouse.vendor_path == "/dev/hidraw6"
    assert mouse.capabilities == frozenset({"buttons", "vendor-read"})
    assert mouse.supports("vendor-read")
    assert mouse.key == "serial:D1K_251010b271_A72V1"


def test_keyboard_with_pointer_interface_is_skipped(fake_sysfs: Path) -> None:
    """У мультимедийной клавиатуры тоже есть BTN_LEFT — её не берём."""
    _interface(
        fake_sysfs,
        hidraw="hidraw2",
        vid=0x258A,
        pid=0x010C,
        interface=0,
        hid_name="BY Tech Gaming Keyboard",
        serial="",
        pointer=True,
        pointer_name="BY Tech Gaming Keyboard Mouse",
        event="event7",
    )

    assert discovery.find_mice() == []


def test_mouse_without_keyboard_interface_is_found(fake_sysfs: Path) -> None:
    """Проводная мышь без HID++ и без «клавиатурного» имени."""
    _interface(
        fake_sysfs,
        hidraw="hidraw0",
        vid=0x1234,
        pid=0x5678,
        interface=0,
        hid_name="Some Mouse",
        serial="ABC123",
        pointer=True,
        pointer_name="Some Mouse",
        event="event9",
    )

    mice = discovery.find_mice()

    assert [mouse.protocol for mouse in mice] == ["evdev"]
    assert mice[0].vendor_path == ""
    assert mice[0].capabilities == frozenset({"buttons"})
    assert discovery.mouse_for_hint("ABC123") == mice[0]
    assert discovery.mouse_for_hint("/dev/input/event9") == mice[0]


def test_touchpad_interface_is_not_a_mouse(fake_sysfs: Path) -> None:
    """Абсолютные оси — это тачпад, а не мышь."""
    _interface(
        fake_sysfs,
        hidraw="hidraw3",
        vid=0x1234,
        pid=0x9999,
        interface=0,
        hid_name="Touchpad",
        pointer=True,
        pointer_name="Touchpad",
        event="event4",
    )
    (fake_sysfs / "sys" / "usb1" / "1-2" / "1-2:1.0" / "hid" / "input" / "input10" / "capabilities" / "abs").write_text(
        "3", encoding="utf-8"
    )

    assert discovery.find_mice() == []
