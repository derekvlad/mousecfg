"""Открытие устройства по подсказке и общие для CLI хелперы."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config, discovery
from .hidpp.device import HidppDevice
from .hidpp.safeplane import Journal
from .hidpp.transport import HidrawTransport
from .i18n import t

logger = logging.getLogger(__name__)


class DeviceNotFound(LookupError):
    pass


class UnsupportedProtocol(LookupError):
    """Устройство найдено, но HID++ на нём нет — команды протокола недоступны."""


@dataclass
class OpenDevice:
    node: discovery.HidrawNode
    device: HidppDevice
    journal: Journal

    def close(self) -> None:
        self.device.transport.close()

    def __enter__(self) -> OpenDevice:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def unsupported_message(mouse: discovery.MouseInfo) -> str:
    """Почему для этой мыши нельзя выполнить команду HID++."""
    return t(
        f"{mouse.name}: у этой модели нет HID++, поэтому DPI, частота опроса и подсветка "
        "недоступны. Профили, кнопки и макросы работают через evdev — см. mousecfg list. "
        "Если у мыши есть вендорский канал, идентификацию можно прочитать: mousecfg vendor.",
        f"{mouse.name}: this model has no HID++, so DPI, report rate and lighting are "
        "unavailable. Profiles, buttons and macros work through evdev — see mousecfg list. "
        "If the mouse has a vendor channel, its identification can be read: mousecfg vendor.",
    )


def _hidpp_node(hint: str | None) -> discovery.HidrawNode:
    if hint:
        candidates = discovery.find_by_hint(hint)
        hidpp = [node for node in candidates if node.is_hidpp_candidate]
        if hidpp:
            return hidpp[0]
        mouse = discovery.mouse_for_hint(hint)
        if mouse is not None and not mouse.supports_hidpp:
            raise UnsupportedProtocol(unsupported_message(mouse))
        if candidates:
            return candidates[0]
        raise DeviceNotFound(
            t(
                f"по подсказке {hint!r} ничего не найдено. Проверьте: lsusb, а также доступ к /dev/hidraw*",
                f"nothing matched the hint {hint!r}. Check lsusb and access to /dev/hidraw*",
            )
        )

    mice = discovery.find_mice()
    for mouse in mice:
        if not mouse.supports_hidpp:
            continue
        node = discovery.node_by_path(mouse.hidraw_path)
        if node is not None:
            return node

    evdev_only = [mouse for mouse in mice if not mouse.supports_hidpp]
    if evdev_only:
        names = ", ".join(mouse.name for mouse in evdev_only)
        raise DeviceNotFound(
            t(
                "HID++ мышь не найдена. Подключены мыши без HID++ ("
                f"{names}) — для них работают профили, кнопки и макросы, "
                "но не DPI/частота/подсветка. Список: mousecfg list; "
                "вендорский канал (если есть) читается командой mousecfg vendor",
                "no HID++ mouse found. Connected mice lack HID++ ("
                f"{names}) — profiles, buttons and macros work for them, "
                "but DPI/report rate/lighting do not. List them with: mousecfg list; "
                "the vendor channel (if present) is read with mousecfg vendor",
            )
        )
    raise DeviceNotFound(
        t("не найдено подходящего HID++ устройства. Проверьте: lsusb, а также доступ к /dev/hidraw*",
          "no suitable HID++ device found. Check lsusb and access to /dev/hidraw*")
    )


def open_device(
    hint: str | None = None,
    *,
    allow_onboard_switch: bool = False,
    dry_run: bool = False,
    keep_journal: bool = True,
    device_index: int | None = None,
) -> OpenDevice:
    """Открыть первое подходящее HID++-устройство.

    `hint` — путь к hidraw, 'vid:pid', серийник или часть имени.
    """
    node = _hidpp_node(hint)
    logger.debug(t("открываю %s", "opening %s"), node.describe())

    transport = HidrawTransport(
        node.path, device_index=0xFF if device_index is None else device_index
    )
    transport.open()
    journal = Journal(config.journal_file() if keep_journal else None)
    device = HidppDevice(
        transport,
        journal=journal,
        allow_onboard_switch=allow_onboard_switch,
        dry_run=dry_run,
        serial=node.serial,
    )
    # Сразу после открытия на узле может висеть хвост старых кадров
    # (в том числе input-отчёты мыши) — читаем их дальше, а не считаем ответом.
    device.discover_features()
    transport.drain()
    return OpenDevice(node=node, device=device, journal=journal)
