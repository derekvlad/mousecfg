"""Поиск Logitech HID++ устройств через sysfs.

pyudev не обязателен: нужные данные (vid/pid, интерфейс, serial) лежат в
sysfs, и это работает даже в контейнерах, где udev-база недоступна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

LOGITECH_VENDOR_ID = 0x046D
HIDRAW_CLASS = Path("/sys/class/hidraw")
SUPPORTED_HID_CLASSES = {
    0x0003: "usb",  # HID_ID=0003:vid:pid — USB
    0x0005: "bluetooth",
}


@dataclass(frozen=True)
class HidrawNode:
    path: str
    name: str
    vendor_id: int
    product_id: int
    bus: str
    serial: str
    interface_number: int | None
    hid_id: str
    usb_address: str = ""

    @property
    def usb_id(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    @property
    def model_id(self) -> str:
        return f"{self.bus}:{self.vendor_id:04x}:{self.product_id:04x}:{self.interface_number or 0}"

    @property
    def is_hidpp_candidate(self) -> bool:
        """HID++ 2.0 живёт на интерфейсе 1 у проводных мышей Logitech."""
        return self.bus == "usb" and self.interface_number == 1

    def describe(self) -> str:
        return f"{self.path}  {self.name}  [{self.usb_id} if{self.interface_number}]  serial={self.serial or '-'}"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _parse_uevent(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _usb_address(device_path: Path) -> str:
    """Стабильный путь подключения, например 1-2."""
    for parent in device_path.parents:
        if parent.parent.name == "usb" or parent.parent.name in {"usb1", "usb2", "usb3", "usb4"}:
            name = parent.name
            if "-" in name:
                return name
    return ""


def discover(*, include_all: bool = False) -> list[HidrawNode]:
    """Вернуть hidraw-узлы Logitech. `include_all` — включая входные интерфейсы."""
    nodes: list[HidrawNode] = []
    if not HIDRAW_CLASS.exists():
        return nodes

    for entry in sorted(HIDRAW_CLASS.glob("hidraw*")):
        device_link = entry / "device"
        uevent_path = device_link / "uevent"
        if not uevent_path.exists():
            continue
        uevent = _parse_uevent(_read_text(uevent_path))

        raw_id = uevent.get("HID_ID", "")
        parts = raw_id.split(":")
        if len(parts) != 3:
            continue
        try:
            bus_code = int(parts[0], 16)
            vendor_id = int(parts[1], 16)
            product_id = int(parts[2], 16)
        except ValueError:
            continue

        if not include_all and vendor_id != LOGITECH_VENDOR_ID:
            continue

        resolved = device_link.resolve()
        interface_number = None
        for parent in resolved.parents:
            candidate = parent / "bInterfaceNumber"
            if candidate.exists():
                interface_number = int(_read_text(candidate), 16)
                break

        node = HidrawNode(
            path=f"/dev/{entry.name}",
            name=uevent.get("HID_NAME", ""),
            vendor_id=vendor_id,
            product_id=product_id,
            bus=SUPPORTED_HID_CLASSES.get(bus_code, f"bus{bus_code:04x}"),
            serial=uevent.get("HID_UNIQ", ""),
            interface_number=interface_number,
            hid_id=raw_id,
            usb_address=_usb_address(resolved),
        )
        nodes.append(node)

    return nodes


def logitech_hidpp_nodes() -> list[HidrawNode]:
    """Кандидаты на HID++ 2.0, самые вероятные в начале списка."""
    return [node for node in discover() if node.is_hidpp_candidate]


def find_by_hint(hint: str) -> list[HidrawNode]:
    """Найти узлы по подсказке: путь, 'vid:pid', serial или часть имени."""
    hint_lower = hint.lower()
    result = []
    for node in discover(include_all=True):
        if (
            hint_lower in node.path.lower()
            or hint_lower == node.usb_id
            or hint_lower in node.serial.lower()
            or hint_lower in node.name.lower()
        ):
            result.append(node)
    return result
