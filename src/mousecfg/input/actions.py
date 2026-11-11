"""Действия кнопок: что отправить в ответ на нажатие.

Действие описывается либо строкой ("browser-back", "ctrl+c", "f13"),
либо таблицей в TOML ({"macro": "name"} или {"key": "ctrl+c"}).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from evdev import ecodes

# Имена кнопок мыши в evdev. Используется и для ремапа, и для отчётов.
MOUSE_BUTTONS: dict[str, tuple[int, ...]] = {
    "left": (ecodes.BTN_LEFT,),
    "right": (ecodes.BTN_RIGHT,),
    "middle": (ecodes.BTN_MIDDLE,),
    "side1": (ecodes.BTN_SIDE, ecodes.BTN_BACK),
    "side2": (ecodes.BTN_EXTRA, ecodes.BTN_FORWARD),
    "task": (ecodes.BTN_TASK,),
}

_BUTTON_BY_CODE: dict[int, str] = {}
for _name, _codes in MOUSE_BUTTONS.items():
    for _code in _codes:
        _BUTTON_BY_CODE[_code] = _name


class ActionError(ValueError):
    pass


class ActionKind(enum.Enum):
    PASSTHROUGH = "passthrough"
    IGNORE = "ignore"
    KEYS = "keys"
    MACRO = "macro"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    keys: tuple[int, ...] = ()
    macro: str | None = None
    source: str = ""

    def describe(self) -> str:
        if self.kind is ActionKind.PASSTHROUGH:
            return "как есть"
        if self.kind is ActionKind.IGNORE:
            return "ничего"
        if self.kind is ActionKind.MACRO:
            return f"макрос {self.macro}"
        return "+".join(ecodes.KEY[code] if code in ecodes.KEY else str(code) for code in self.keys)


def button_name(code: int) -> str:
    return _BUTTON_BY_CODE.get(code, ecodes.KEY.get(code, str(code)))


def button_code(name: str) -> int:
    """Код evdev по имени кнопки из профиля (left/right/middle/side1/side2/dpi...)."""
    aliases = {
        "back": "side1",
        "forward": "side2",
    }
    key = aliases.get(name, name)
    codes = MOUSE_BUTTONS.get(key)
    if not codes:
        known = ", ".join(sorted(MOUSE_BUTTONS))
        raise ActionError(f"неизвестная кнопка {name!r}; известные: {known}")
    return codes[0]


# Готовые имена действий: имя -> комбинация клавиш.
KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "copy": ("ctrl+c",),
    "paste": ("ctrl+v",),
    "cut": ("ctrl+x",),
    "undo": ("ctrl+z",),
    "redo": ("ctrl+shift+z",),
    "select-all": ("ctrl+a",),
    "save": ("ctrl+s",),
    "close-window": ("alt+F4",),
    "next-tab": ("ctrl+PageDown",),
    "prev-tab": ("ctrl+PageUp",),
    "browser-back": ("alt+Left",),
    "browser-forward": ("alt+Right",),
    "browser-home": ("alt+Home",),
    "volume-up": ("KEY_VOLUMEUP",),
    "volume-down": ("KEY_VOLUMEDOWN",),
    "mute": ("KEY_MUTE",),
    "play-pause": ("KEY_PLAYPAUSE",),
    "next-track": ("KEY_NEXTSONG",),
    "prev-track": ("KEY_PREVIOUSSONG",),
    "enter": ("KEY_ENTER",),
    "escape": ("KEY_ESC",),
    "space": ("KEY_SPACE",),
    "tab": ("KEY_TAB",),
    "delete": ("KEY_DELETE",),
    "page-up": ("KEY_PAGEUP",),
    "page-down": ("KEY_PAGEDOWN",),
}

_MODIFIER_ALIASES = {
    "ctrl": "KEY_LEFTCTRL",
    "control": "KEY_LEFTCTRL",
    "alt": "KEY_LEFTALT",
    "shift": "KEY_LEFTSHIFT",
    "super": "KEY_LEFTMETA",
    "meta": "KEY_LEFTMETA",
    "win": "KEY_LEFTMETA",
}


def _resolve_key(token: str) -> int:
    token = token.strip()
    if not token:
        raise ActionError("пустое имя клавиши")

    upper = token.upper()
    if upper.startswith("KEY_"):
        code = ecodes.ecodes.get(upper)
        if code is None:
            raise ActionError(f"неизвестная клавиша {token!r}")
        return code

    if len(token) == 1:
        name = f"KEY_{upper}" if token.isalpha() or token.isdigit() else None
        if name and name in ecodes.ecodes:
            return ecodes.ecodes[name]

    if upper in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "F13", "F14", "F15"):
        return ecodes.ecodes[f"KEY_{upper}"]

    lowered = token.lower()
    if lowered in _MODIFIER_ALIASES:
        return ecodes.ecodes[_MODIFIER_ALIASES[lowered]]

    named = {
        "arrowup": "KEY_UP",
        "up": "KEY_UP",
        "arrowdown": "KEY_DOWN",
        "down": "KEY_DOWN",
        "arrowleft": "KEY_LEFT",
        "left": "KEY_LEFT",
        "arrowright": "KEY_RIGHT",
        "right": "KEY_RIGHT",
        "home": "KEY_HOME",
        "end": "KEY_END",
        "insert": "KEY_INSERT",
        "esc": "KEY_ESC",
        "escape": "KEY_ESC",
        "enter": "KEY_ENTER",
        "space": "KEY_SPACE",
        "tab": "KEY_TAB",
        "backspace": "KEY_BACKSPACE",
        "delete": "KEY_DELETE",
        "pgup": "KEY_PAGEUP",
        "pgdn": "KEY_PAGEDOWN",
        "pageup": "KEY_PAGEUP",
        "pagedown": "KEY_PAGEDOWN",
        "playpause": "KEY_PLAYPAUSE",
        "volumeup": "KEY_VOLUMEUP",
        "volumedown": "KEY_VOLUMEDOWN",
        "mute": "KEY_MUTE",
        "nextsong": "KEY_NEXTSONG",
        "previoussong": "KEY_PREVIOUSSONG",
    }
    if lowered in named:
        return ecodes.ecodes[named[lowered]]

    raise ActionError(f"неизвестная клавиша {token!r}")


def parse_keys(spec: str) -> tuple[int, ...]:
    """Превратить 'ctrl+shift+t' в кортеж кодов evdev в порядке нажатия."""
    tokens = [part for part in spec.replace("+", " ").split() if part]
    if not tokens:
        raise ActionError(f"пустая комбинация {spec!r}")

    if len(tokens) == 1 and tokens[0].lower() in KEY_ALIASES:
        codes: list[int] = []
        for step in KEY_ALIASES[tokens[0].lower()]:
            codes.extend(parse_keys(step))
        return tuple(codes)

    return tuple(_resolve_key(token) for token in tokens)


def parse_action(spec: Any) -> Action:
    """Разобрать действие из профиля или CLI."""
    if isinstance(spec, dict):
        if "macro" in spec:
            return Action(ActionKind.MACRO, macro=str(spec["macro"]), source=str(spec))
        if "key" in spec:
            return Action(ActionKind.KEYS, keys=parse_keys(str(spec["key"])), source=str(spec))
        if "action" in spec:
            return parse_action(spec["action"])
        raise ActionError(f"непонятное действие: {spec!r}")

    if not isinstance(spec, str):
        raise ActionError(f"действие должно быть строкой или таблицей, получено {type(spec).__name__}")

    text = spec.strip()
    lowered = text.lower()
    if lowered in ("passthrough", "как есть", "default"):
        return Action(ActionKind.PASSTHROUGH, source=text)
    if lowered in ("ignore", "none", "ничего", "disable"):
        return Action(ActionKind.IGNORE, source=text)
    if lowered in KEY_ALIASES:
        return Action(ActionKind.KEYS, keys=parse_keys(lowered), source=text)
    return Action(ActionKind.KEYS, keys=parse_keys(text), source=text)


def keys_in_action(action: Action) -> tuple[int, ...]:
    return action.keys


def modifier_codes(codes: tuple[int, ...]) -> list[int]:
    """Коды, которые надо удерживать (modifier'ы)."""
    return [code for code in codes if code in {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTMETA}]


def non_modifier_codes(codes: tuple[int, ...]) -> list[int]:
    modifiers = set(modifier_codes(codes))
    return [code for code in codes if code not in modifiers]


__all__ = [
    "Action",
    "ActionError",
    "ActionKind",
    "MOUSE_BUTTONS",
    "button_code",
    "button_name",
    "modifier_codes",
    "non_modifier_codes",
    "parse_action",
    "parse_keys",
]

