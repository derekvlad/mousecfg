"""Поиск мышей через sysfs: HID++ и устройства, доступные только через evdev.

pyudev не обязателен: нужные данные (vid/pid, интерфейс, serial) лежат в
sysfs, и это работает даже в контейнерах, где udev-база недоступна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import registry
from .i18n import t

logger = logging.getLogger(__name__)

LOGITECH_VENDOR_ID = 0x046D
HIDRAW_CLASS = Path("/sys/class/hidraw")
SUPPORTED_HID_CLASSES = {
    0x0003: "usb",  # HID_ID=0003:vid:pid — USB
    0x0005: "bluetooth",
}

# Известные семейства мышей без HID++. Для них работают профили на хосте,
# ремап кнопок и макросы (evdev/uinput), но не DPI/частота/подсветка:
# у этих моделей закрытый вендорский протокол, которого мы не касаемся.
VENDOR_FAMILIES: dict[tuple[int, int], str] = {
    (0xA8A5, 0x2255): "ajazz-2.4g",
}

# Что мы умеем на уровне протокола. Для HID++ возможности уточняются при
# подключении (наличие фич DPI/RGB/onboard), здесь — грубая картина.
HIDPP_CAPABILITIES = frozenset({"buttons", "dpi", "report_rate", "lighting", "mode"})
EVDEV_CAPABILITIES = frozenset({"buttons"})


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
    # evdev-узлы этого интерфейса и указатель на «мышиный» из них.
    input_paths: tuple[str, ...] = ()
    pointer_path: str = ""
    pointer_name: str = ""

    @property
    def usb_id(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    @property
    def model_id(self) -> str:
        return f"{self.bus}:{self.vendor_id:04x}:{self.product_id:04x}:{self.interface_number or 0}"

    @property
    def is_hidpp_candidate(self) -> bool:
        """HID++ 2.0 живёт на интерфейсе 1 у проводных мышей Logitech."""
        return (
            self.vendor_id == LOGITECH_VENDOR_ID
            and self.bus == "usb"
            and self.interface_number == 1
        )

    @property
    def family(self) -> str:
        return VENDOR_FAMILIES.get((self.vendor_id, self.product_id), "")

    def describe(self) -> str:
        return f"{self.path}  {self.name}  [{self.usb_id} if{self.interface_number}]  serial={self.serial or '-'}"


@dataclass(frozen=True)
class MouseInfo:
    """Мышь целиком: hidraw-узел протокола и evdev-узел для кнопок."""

    key: str
    name: str
    serial: str
    usb_id: str
    usb_address: str
    vendor_id: int
    product_id: int
    protocol: str  # "hidpp" | "evdev"
    family: str
    hidraw_path: str
    input_path: str
    interface_number: int | None
    capabilities: frozenset[str]

    @property
    def supports_hidpp(self) -> bool:
        return self.protocol == "hidpp"

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def describe(self) -> str:
        protocol = "HID++" if self.supports_hidpp else t("evdev (без HID++)", "evdev (no HID++)")
        bits = [self.name or self.usb_id, f"[{self.usb_id}]", t(f"протокол: {protocol}", f"protocol: {protocol}")]
        if self.input_path:
            bits.append(t(f"ввод: {self.input_path}", f"input: {self.input_path}"))
        if self.serial:
            bits.append(f"serial={self.serial}")
        return "  ".join(bits)


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


def _read_bitmask(path: Path) -> set[int]:
    """Разобрать `capabilities/*` из sysfs: hex-слова, старшее слово первым."""
    bits: set[int] = set()
    words = _read_text(path).split()
    words.reverse()  # ядро печатает битмап от старшего слова к младшему
    for index, word in enumerate(words):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        while value:
            lowest = value & -value
            bits.add(index * 64 + lowest.bit_length() - 1)
            value ^= lowest
    return bits


def _looks_like_pointer(input_dir: Path) -> bool:
    """Мышь — это BTN_LEFT + относительные оси и не тачпад (без ABS_X/Y)."""
    keys = _read_bitmask(input_dir / "capabilities" / "key")
    rels = _read_bitmask(input_dir / "capabilities" / "rel")
    absolutes = _read_bitmask(input_dir / "capabilities" / "abs")
    return 272 in keys and {0, 1} <= rels and not ({0, 1} & absolutes)


def _input_nodes(device_link: Path) -> tuple[tuple[str, ...], str, str]:
    """evdev-узлы HID-интерфейса: (все пути, путь мыши, имя мыши)."""
    paths: list[str] = []
    pointer_path = ""
    pointer_name = ""
    input_root = device_link / "input"
    if not input_root.exists():
        return (), "", ""
    for input_dir in sorted(input_root.glob("input*")):
        event = next(iter(sorted(input_dir.glob("event*"))), None)
        if event is None:
            continue
        paths.append(f"/dev/input/{event.name}")
        if not pointer_path and _looks_like_pointer(input_dir):
            pointer_path = f"/dev/input/{event.name}"
            pointer_name = _read_text(input_dir / "name")
    return tuple(paths), pointer_path, pointer_name


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

        input_paths, pointer_path, pointer_name = _input_nodes(device_link)

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
            input_paths=input_paths,
            pointer_path=pointer_path,
            pointer_name=pointer_name,
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


def _group_key(node: HidrawNode) -> tuple[str, str]:
    """Один физический девайс — один ключ, даже если у него 3 интерфейса."""
    return (node.usb_address or node.serial or node.path, node.usb_id)


def find_mice() -> list[MouseInfo]:
    """Все мыши: сначала HID++ (их умеем настраивать), затем evdev-only."""
    groups: dict[tuple[str, str], list[HidrawNode]] = {}
    for node in discover(include_all=True):
        if node.bus not in {"usb", "bluetooth"}:
            continue
        groups.setdefault(_group_key(node), []).append(node)

    mice: list[MouseInfo] = []
    for nodes in groups.values():
        mouse = _mouse_from_group(nodes)
        if mouse is not None:
            mice.append(mouse)
    mice.sort(key=lambda item: (item.protocol != "hidpp", item.name.lower(), item.key))
    return mice


def _mouse_from_group(nodes: list[HidrawNode]) -> MouseInfo | None:
    hidpp = next((node for node in nodes if node.is_hidpp_candidate), None)
    pointer = next((node for node in nodes if node.pointer_path), None)
    if hidpp is None and pointer is None:
        return None

    primary = hidpp or pointer or nodes[0]
    name = (pointer.pointer_name if pointer else "") or primary.name
    # Виртуальная мышь проекта не должна попадать в список управляемых.
    if name.strip().lower() == "mousecfg virtual input":
        return None
    # Комбинированные клавиатуры с «мышиным» интерфейсом (мультимедийные
    # клавиши, колесо) — не наша цель, даже если у них есть BTN_LEFT.
    if "keyboard" in name.lower():
        return None

    key = registry.device_key(
        serial=primary.serial, usb_id=primary.usb_id, usb_address=primary.usb_address
    )
    protocol = "hidpp" if hidpp is not None else "evdev"
    return MouseInfo(
        key=key,
        name=name,
        serial=primary.serial,
        usb_id=primary.usb_id,
        usb_address=primary.usb_address,
        vendor_id=primary.vendor_id,
        product_id=primary.product_id,
        protocol=protocol,
        family=primary.family,
        hidraw_path=primary.path,
        input_path=(pointer.pointer_path if pointer else ""),
        interface_number=primary.interface_number,
        capabilities=HIDPP_CAPABILITIES if protocol == "hidpp" else EVDEV_CAPABILITIES,
    )


def mouse_for_hint(hint: str) -> MouseInfo | None:
    """Мышь по подсказке (путь hidraw/evdev, vid:pid, serial или имя)."""
    hint_lower = hint.lower()
    for mouse in find_mice():
        if (
            hint_lower in mouse.hidraw_path.lower()
            or hint_lower in mouse.input_path.lower()
            or hint_lower == mouse.usb_id
            or hint_lower in mouse.serial.lower()
            or hint_lower in mouse.name.lower()
        ):
            return mouse
    return None


def node_by_path(path: str) -> HidrawNode | None:
    return next((node for node in discover(include_all=True) if node.path == path), None)

