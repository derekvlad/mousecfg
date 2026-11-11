"""Поток ввода: ремап кнопок и макросы на стороне хоста.

Во встроенную память мыши такие вещи не попадают в принципе: кнопки
перехватываются на уровне evdev, а макросы отправляются через uinput.
"""

from __future__ import annotations

from .actions import Action, ActionError, parse_action
from .detect import InputDeviceInfo, find_pointer_devices, require_pointer_device
from .emitter import Emitter
from .macros import Macro, MacroRunner, parse_macros
from .remapper import Remapper, RemapStats

__all__ = [
    "Action",
    "ActionError",
    "Emitter",
    "InputDeviceInfo",
    "Macro",
    "MacroRunner",
    "RemapStats",
    "Remapper",
    "find_pointer_devices",
    "parse_action",
    "parse_macros",
    "require_pointer_device",
]
