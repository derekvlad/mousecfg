"""Матрица мышь → конфигурация.

Профили хранятся по устройствам, поэтому нужен стабильный ключ устройства.
Используем серийник (он есть у Logitech в `HID_UNIQ`), а при его отсутствии —
путь подключения в USB: он не меняется, если мышь воткнута в тот же порт.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class DeviceState:
    key: str
    serial: str = ""
    name: str = ""
    usb_id: str = ""
    usb_address: str = ""
    path: str = ""
    input_path: str = ""
    profiles_file: str = ""
    active_profile: str = ""
    # Профиль, выбранный пользователем вручную (CLI/трей/GUI). Демон
    # удерживает его при смене фокуса, пока не совпадёт профиль по
    # приложению (`match_apps`). Пусто — обычный автоматический выбор.
    pinned_profile: str = ""
    mode: str = ""
    lighting_supported: bool = False
    seen_at: float = 0.0
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        bits = [self.name or self.usb_id or "неизвестная мышь"]
        if self.active_profile:
            bits.append(f"профиль: {self.active_profile}")
        if self.mode:
            bits.append(f"режим: {self.mode}")
        if self.input_path:
            bits.append(f"ввод: {self.input_path}")
        return " | ".join(bits)


def device_key(*, serial: str = "", usb_id: str = "", usb_address: str = "") -> str:
    if serial:
        return f"serial:{serial}"
    if usb_id:
        return f"usb:{usb_id}:{usb_address or 'any'}"
    return "unknown"


def _state_path() -> Path:
    return config.state_file()


def load_state() -> dict[str, DeviceState]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        logger.warning("не удалось разобрать %s, начинаю с чистого состояния", path)
        return {}

    if raw.get("version") != STATE_VERSION:
        logger.debug("версия состояния %s устарела", raw.get("version"))
        return {}

    devices: dict[str, DeviceState] = {}
    for key, payload in raw.get("devices", {}).items():
        known = {name: payload[name] for name in DeviceState.__dataclass_fields__ if name in payload}
        devices[key] = DeviceState(**known)
    return devices


def save_state(devices: dict[str, DeviceState]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "devices": {key: asdict(state) for key, state in devices.items()},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
