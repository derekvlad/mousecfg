"""Определение активного приложения: граница между профилями.

niri (нынешняя сессия), Hyprland, X11 — по одному плагину на источник.
GNOME пока не поддержан: там нужен свой Shell-расширение, и это отдельная
работа. Отсутствие источника не ломает демон — профили просто не
переключаются по приложению.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT = 2.0


@dataclass(frozen=True)
class FocusedApp:
    app_id: str
    title: str = ""
    pid: int | None = None

    def describe(self) -> str:
        return f"{self.app_id} ({self.title})" if self.title else self.app_id


class Watcher(Protocol):
    name: str

    def available(self) -> bool: ...

    def focused(self) -> FocusedApp | None: ...


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("%s не сработал: %s", command[0], exc)
        return None
    if completed.returncode != 0:
        logger.debug("%s вернул код %d: %s", command[0], completed.returncode, completed.stderr.strip())
        return None
    return completed.stdout


class NiriWatcher:
    name = "niri"

    def available(self) -> bool:
        return bool(os.environ.get("NIRI_SOCKET")) or shutil.which("niri") is not None

    def focused(self) -> FocusedApp | None:
        output = _run(["niri", "msg", "--json", "focused-window"])
        if not output:
            return None
        try:
            data = json.loads(output)
        except ValueError:
            return None
        if not data or not data.get("is_focused", True):
            return None
        app_id = str(data.get("app_id") or "")
        if not app_id:
            return None
        return FocusedApp(app_id=app_id, title=str(data.get("title") or ""), pid=data.get("pid"))


class HyprlandWatcher:
    name = "hyprland"

    def available(self) -> bool:
        return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None

    def focused(self) -> FocusedApp | None:
        output = _run(["hyprctl", "-j", "activewindow"])
        if not output:
            return None
        try:
            data = json.loads(output)
        except ValueError:
            return None
        app_id = str(data.get("class") or data.get("initialClass") or "")
        if not app_id:
            return None
        return FocusedApp(app_id=app_id, title=str(data.get("title") or ""), pid=data.get("pid"))


class SwayWatcher:
    name = "sway"

    def available(self) -> bool:
        return bool(os.environ.get("SWAYSOCK")) and shutil.which("swaymsg") is not None

    def focused(self) -> FocusedApp | None:
        output = _run(["swaymsg", "-t", "get_tree"])
        if not output:
            return None
        try:
            tree = json.loads(output)
        except ValueError:
            return None
        node = self._find_focused(tree)
        if node is None:
            return None
        props = node.get("app_id") or node.get("window_properties", {}).get("class")
        if not props:
            return None
        return FocusedApp(app_id=str(props), title=str(node.get("name") or ""), pid=node.get("pid"))

    def _find_focused(self, node: dict) -> dict | None:
        if node.get("focused"):
            return node
        for child in list(node.get("nodes", [])) + list(node.get("floating_nodes", [])):
            found = self._find_focused(child)
            if found is not None:
                return found
        return None


class X11Watcher:
    name = "x11"

    def available(self) -> bool:
        return bool(os.environ.get("DISPLAY")) and shutil.which("xprop") is not None

    def focused(self) -> FocusedApp | None:
        output = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if not output or "0x" not in output:
            return None
        window_id = output.split("#")[-1].strip() if "#" in output else output.split()[-1]
        props = _run(["xprop", "-id", window_id, "WM_CLASS", "_NET_WM_NAME"])
        if not props:
            return None
        app_id = ""
        title = ""
        for line in props.splitlines():
            if line.startswith("WM_CLASS"):
                parts = [item.strip().strip('"') for item in line.split("=", 1)[-1].split(",")]
                app_id = parts[-1] if parts else ""
            elif line.startswith("_NET_WM_NAME"):
                title = line.split("=", 1)[-1].strip().strip('"')
        if not app_id:
            return None
        return FocusedApp(app_id=app_id, title=title)


WATCHERS: tuple[Watcher, ...] = (HyprlandWatcher(), SwayWatcher(), NiriWatcher(), X11Watcher())


def detect_watcher() -> Watcher | None:
    for watcher in WATCHERS:
        try:
            if watcher.available():
                logger.info("источник активного окна: %s", watcher.name)
                return watcher
        except Exception as exc:  # noqa: BLE001 — источник фокуса не должен ронять демон
            logger.debug("проверка %s упала: %s", watcher.name, exc)
    logger.info("источник активного окна не найден: переключение по приложению выключено")
    return None
