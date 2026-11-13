"""Пути конфигурации и состояния проекта."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "mousecfg"


def _xdg_dir(variable: str, default: str) -> Path:
    base = os.environ.get(variable) or str(Path.home() / default)
    return Path(base)


def config_dir() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / APP_NAME


def state_dir() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state") / APP_NAME


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / APP_NAME


def data_dir() -> Path:
    """Данные проекта (udev-правило, unit-файл).

    При установке пакета данные лежат рядом с модулями, при запуске из
    репозитория — в его корне. Проверяем оба варианта.
    """
    package_data = Path(__file__).resolve().parent / "data"
    if package_data.exists():
        return package_data
    return Path(__file__).resolve().parent.parent.parent / "data"


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def state_file() -> Path:
    return state_dir() / "state.json"


def journal_file() -> Path:
    return state_dir() / "hid.log"


def backups_dir() -> Path:
    """Снимки onboard-конфигурации перед записью в память мыши."""
    return config_dir() / "backups"


def onboard_consent_file() -> Path:
    """Согласие на запись в onboard-память (первое подключение)."""
    return config_dir() / "onboard-consent.json"


def consent_file() -> Path:
    """Сохранённое согласие на однократный переход On-Board → Host.

    Хранится отдельно от `state.json`, который постоянно пишет демон: иначе
    команда `mousecfg mode host` и демон писали бы в один файл.
    """
    return state_dir() / "host-consent.json"


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"
