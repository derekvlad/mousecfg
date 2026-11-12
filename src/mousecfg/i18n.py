"""Язык интерфейса: русский или английский.

Язык определяется по окружению в таком порядке:

1. ``MOUSECFG_LANG`` — явное указание (``ru`` / ``en``, можно ``ru_RU.UTF-8``);
2. ``LC_ALL``, ``LC_MESSAGES``, ``LANG`` — локаль системы.

Поддерживаются ``ru`` и ``en``; если локаль не распознана — русский.

В коде строки задаются парой::

    from .i18n import t
    print(t("профиль не найден", "profile not found"))

Так обе версии видны рядом в исходнике и не расходятся: не нужны ни
``.po``/``.mo``, ни шаг сборки. Подстановки оформляются f-строками внутри
каждого аргумента::

    t(f"профиль {name!r} не найден", f"profile {name!r} not found")
"""

from __future__ import annotations

import os

SUPPORTED = ("ru", "en")
DEFAULT = "ru"

# Переменные окружения, откуда берём язык, в порядке приоритета.
_ENV_VARS = ("MOUSECFG_LANG", "LC_ALL", "LC_MESSAGES", "LANG")

_current: str | None = None


def _normalize(value: str) -> str | None:
    """Привести ``ru_RU.UTF-8`` / ``en-US`` к ``ru`` / ``en``."""
    code = value.strip().lower().replace("-", "_").split(".")[0].split("_")[0]
    return code if code in SUPPORTED else None


def _detect() -> str:
    for name in _ENV_VARS:
        value = os.environ.get(name, "")
        if not value:
            continue
        code = _normalize(value)
        if code is not None:
            return code
    return DEFAULT


def language() -> str:
    """Текущий язык интерфейса: ``ru`` или ``en``."""
    global _current
    if _current is None:
        _current = _detect()
    return _current


def set_language(lang: str | None) -> str:
    """Задать язык принудительно; ``None`` — снова определять из окружения."""
    global _current
    if lang is None:
        _current = None
    else:
        code = _normalize(lang)
        if code is None:
            raise ValueError(
                f"unsupported language {lang!r}: expected one of {', '.join(SUPPORTED)}"
            )
        _current = code
    return language()


def t(ru: str, en: str) -> str:
    """Вернуть строку на текущем языке интерфейса."""
    return en if language() == "en" else ru
