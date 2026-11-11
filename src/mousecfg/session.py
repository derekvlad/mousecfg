"""Открытие устройства по подсказке и общие для CLI хелперы."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config, discovery
from .hidpp.device import HidppDevice
from .hidpp.safeplane import Journal
from .hidpp.transport import HidrawTransport

logger = logging.getLogger(__name__)


class DeviceNotFound(LookupError):
    pass


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


def open_device(
    hint: str | None = None,
    *,
    allow_onboard_switch: bool = False,
    dry_run: bool = False,
    keep_journal: bool = True,
    device_index: int | None = None,
) -> OpenDevice:
    """Открыть первое подходящее устройство.

    `hint` — путь к hidraw, 'vid:pid', серийник или часть имени.
    """
    if hint:
        candidates = discovery.find_by_hint(hint)
        candidates = [node for node in candidates if node.is_hidpp_candidate] or candidates
    else:
        candidates = discovery.logitech_hidpp_nodes()
        if not candidates:
            candidates = [node for node in discovery.discover() if node.bus == "usb"]

    if not candidates:
        raise DeviceNotFound(
            "не найдено подходящего HID++ устройства. Проверьте: lsusb, а также доступ к /dev/hidraw*"
        )

    node = candidates[0]
    logger.debug("открываю %s", node.describe())

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
