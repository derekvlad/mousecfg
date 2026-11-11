"""Поиск evdev-узлов мыши.

Проект не грабит весь ввод: кнопка перехватывается только если она реально
попала в профиль. Но для этого нужно уметь находить нужный event-узел.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import evdev
from evdev import ecodes

logger = logging.getLogger(__name__)

BUTTON_CAPABILITIES = (ecodes.EV_KEY,)


@dataclass(frozen=True)
class InputDeviceInfo:
    path: str
    name: str
    phys: str
    uniq: str
    buttons: tuple[int, ...]

    def describe(self) -> str:
        names = ", ".join(button_name(code) for code in self.buttons)
        return f"{self.path}  {self.name}  [{self.phys}]  кнопки: {names or '-'}"


def button_name(code: int) -> str:
    """Имя кнопки по коду.

    `ecodes.BTN` для кодов с псевдонимами (BTN_LEFT/BTN_MOUSE) отдаёт кортеж,
    поэтому берём первое имя: для человека оно и нужно.
    """
    name = ecodes.BTN.get(code)
    if isinstance(name, tuple):
        return str(name[0])
    if isinstance(name, str):
        return name
    from .actions import button_name as _action_name

    try:
        return _action_name(code)
    except Exception:  # noqa: BLE001
        return str(code)


def _buttons_of(device: evdev.InputDevice) -> tuple[int, ...]:
    keys = device.capabilities().get(ecodes.EV_KEY, [])
    return tuple(sorted(code for code in keys if code in ecodes.BTN))


def _is_pointer(info: InputDeviceInfo) -> bool:
    """Мышь — это устройство, у которого есть BTN_LEFT."""
    return ecodes.BTN_LEFT in info.buttons


def iter_input_devices() -> list[InputDeviceInfo]:
    result: list[InputDeviceInfo] = []
    for path in sorted(Path("/dev/input").glob("event*")):
        try:
            device = evdev.InputDevice(str(path))
        except PermissionError:
            logger.debug("нет доступа к %s", path)
            continue
        except OSError:
            continue
        try:
            result.append(
                InputDeviceInfo(
                    path=str(path),
                    name=device.name,
                    phys=device.phys,
                    uniq=device.uniq or "",
                    buttons=_buttons_of(device),
                )
            )
        finally:
            device.close()
    return result


def find_pointer_devices(*, name_hint: str = "", serial: str = "") -> list[InputDeviceInfo]:
    """Найти event-узлы мышей. Фильтр по имени/серийнику — если он задан."""
    candidates = [info for info in iter_input_devices() if _is_pointer(info)]
    if name_hint:
        hint = name_hint.lower()
        candidates = [info for info in candidates if hint in info.name.lower()]
    if serial:
        candidates = [info for info in candidates if serial.lower() == info.uniq.lower()]
    return candidates


def require_pointer_device(*, name_hint: str = "", serial: str = "") -> InputDeviceInfo:
    candidates = find_pointer_devices(name_hint=name_hint, serial=serial)
    if not candidates:
        scope = ", ".join(filter(None, (f"имя ~ {name_hint!r}" if name_hint else "", f"serial={serial}" if serial else "")))
        raise LookupError(
            "не найден event-узел мыши"
            + (f" ({scope})" if scope else "")
            + ". Проверьте права на /dev/input/event* (нужно правило udev или группа input)."
        )
    if len(candidates) > 1:
        logger.warning(
            "найдено несколько мышей, беру первую: %s",
            "; ".join(info.path for info in candidates),
        )
    return candidates[0]
