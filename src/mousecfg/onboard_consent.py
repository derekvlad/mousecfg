"""Согласие на запись в onboard-память мыши.

Это единственная операция проекта, которая оставляет след в самой мыши.
Поэтому согласие отдельное от перехода On-Board → Host, привязано к
устройству, и снимается только вручную. Демон его не использует: запись
доступна лишь по явной команде `mousecfg onboard ...`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mousecfg import config
from mousecfg.i18n import t

logger = logging.getLogger(__name__)

CONSENT_VERSION = 1


def _path() -> Path:
    return config.onboard_consent_file()


def load() -> set[str]:
    path = _path()
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning(t("не удалось прочитать %s, согласие на запись считается невыданным", "failed to read %s, onboard-write consent is treated as not given"), path)
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


def granted(key: str) -> bool:
    if not key or key == "unknown":
        return False
    return key in load()


def record(key: str) -> None:
    if not key or key == "unknown":
        return
    keys = load()
    if key in keys:
        return
    keys.add(key)
    _save(keys)
    logger.info(t("запомнено согласие на запись в память мыши: %s", "recorded consent for onboard-memory writes: %s"), key)


def forget(key: str = "") -> None:
    if not key:
        _save(set())
        return
    keys = load()
    if key in keys:
        keys.discard(key)
        _save(keys)


WARNING = t(
    """
ВНИМАНИЕ: запись в энергонезависимую память мыши (onboard flash).

  • Эта операция сохраняется в самой мыши, а не на хосте: переживает
    перезагрузку, отключение и перенос на другой компьютер.
  • Прошивка обновляет память сразу с каждым кадром. Частично записанный
    кадр может повредить раскладку кнопок или настройки.
  • Гарантий нет: протокол закрытый, разобран сообществом, а не вендором.

Что делает проект, чтобы это было безопасно:

  • перед каждой записью снимается полный снимок текущего конфигурации
    (backups в ~/.config/mousecfg) — им можно вернуть прежнее состояние;
  • пишется только известный минимум: DPI, частота опроса, подсветка,
    сенсор/сон; команда 0d/00 (стирание памяти) не отправляется никогда;
  • значение пишется поверх живого чтения — служебные и неизвестные байты
    не изменяются.

Пути восстановления, если что-то пойдёт не так:

  1. `mousecfg onboard restore` — записать последний снимок назад;
  2. веб-конфигуратор AJAZZ (вкладка Onboard configuration) — Import файла
     снимка или Reset to default;
  3. штатное ПО/веб-конфигуратор с «Reset to default» на самой мыши.
""",
    """
WARNING: this writes to the mouse non-volatile memory (onboard flash).

  • The change lives in the mouse, not on the host: it survives reboots,
    disconnection and moving the mouse to another computer.
  • The firmware updates memory with every frame. A partially written frame
    can damage the button mapping or the settings.
  • There are no guarantees: the protocol is closed and reverse-engineered by
    the community, not documented by the vendor.

What the project does to keep this safe:

  • before every write it takes a full snapshot of the current configuration
    (backups in ~/.config/mousecfg) — the previous state can be restored;
  • it writes only the known minimum: DPI, report rate, lighting,
    sensor/sleep; command 0d/00 (memory erase) is never sent;
  • values are written on top of a live read, so unknown and service bytes
    are left untouched.

Recovery paths if something goes wrong:

  1. `mousecfg onboard restore` — write the latest snapshot back;
  2. the AJAZZ web configurator (Onboard configuration tab) — Import the
     snapshot file or Reset to default;
  3. the vendor software / web configurator with "Reset to default".
""",
)

__all__ = ["WARNING", "forget", "granted", "load", "record"]
