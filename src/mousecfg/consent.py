"""Сохранённое согласие на однократный переход On-Board → Host.

Переход `0x8100/0x10` — единственная персистентная операция проекта
(`persistent-consented`). Он нужен один раз: без Host mode прошивка мыши
игнорирует настройки с хоста, которые применяет демон.

Проблема: прошивка не всегда помнит Host mode между сессиями (например, G102
возвращается в On-Board после перезагрузки). Тогда демон при старте видит
`onboard` и не может ничего применить — а спрашивать согласие он не умеет.

Решение: когда пользователь один раз явно согласился
(`mousecfg mode host --allow-onboard-switch`), запоминаем это по стабильному
ключу устройства. Демон при подключении мыши, оказавшейся в On-Board, сам
повторяет переход — но только если согласие уже было дано. Без записи
ничего не происходит.

Файл отдельный от `state.json`, который постоянно пишет демон: иначе команда
и демон гонялись бы за один и тот же файл.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import config, registry

logger = logging.getLogger(__name__)

CONSENT_VERSION = 1


def _path() -> Path:
    return config.consent_file()


def load() -> set[str]:
    """Ключи устройств, на переход которых пользователь дал согласие."""
    path = _path()
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("не удалось прочитать %s, согласие считается невыданным", path)
        return set()
    if raw.get("version") != CONSENT_VERSION:
        return set()
    return {str(item) for item in raw.get("devices", [])}


def _save(keys: set[str]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CONSENT_VERSION, "devices": sorted(keys)}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def record(key: str) -> None:
    """Запомнить согласие для устройства (ключ из `registry.device_key`)."""
    if not key or key == "unknown":
        return
    keys = load()
    if key in keys:
        return
    keys.add(key)
    _save(keys)
    logger.info("запомнено согласие на переход On-Board → Host: %s", key)


def granted(key: str) -> bool:
    """Давал ли пользователь согласие на переход для этого устройства."""
    if not key or key == "unknown":
        return False
    return key in load()


def forget(key: str = "") -> None:
    """Снять согласие: для одного устройства или для всех (`key` пуст)."""
    if not key:
        _save(set())
        return
    keys = load()
    if key in keys:
        keys.discard(key)
        _save(keys)


def key_for_node(node: object) -> str:
    """Стабильный ключ устройства из узла discovery (та же логика, что в registry)."""
    return registry.device_key(
        serial=getattr(node, "serial", "") or "",
        usb_id=getattr(node, "usb_id", "") or "",
        usb_address=getattr(node, "usb_address", "") or "",
    )
