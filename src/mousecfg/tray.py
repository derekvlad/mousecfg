"""Трей mousecfg: отдельный процесс GTK3 + AppIndicator3.

Почему отдельно от GUI: AppIndicator3 построен на GTK3, а главное окно — на
GTK4/libadwaita, и в одном процессе их не совместить. Трей — маленький
процесс, который общается с демоном по тому же UNIX-сокету и заодно держит
сессию глобальных хоткеев портала (портал требует запуск в
`app-io.github.mousecfg.MouseCfg-<суффикс>.scope`, см. run_in_scope()).

Никакой логики работы с устройством здесь нет: только IPC-вызовы.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import gi

from mousecfg.i18n import t

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")

from gi.repository import AppIndicator3, GLib, Gtk  # noqa: E402

from mousecfg import hotkeys, ipc  # noqa: E402

logger = logging.getLogger(__name__)

INDICATOR_ID = "mousecfg"
ICON_NAME = "input-mouse"
APP_ID = "io.github.mousecfg.MouseCfg"

REFRESH_SECONDS = 3

# Имя transient-scope, в котором должен жить процесс, чтобы xdg-desktop-portal
# вывел app id из cgroup. Регулярное выражение портала ждёт суффикс из
# алфанум-символов, поэтому «-tray» подходит, а просто «mousecfg» — нет.
SCOPE_SUFFIX = "tray"
SCOPE_UNIT = f"app-{APP_ID}-{SCOPE_SUFFIX}.scope"
_SCOPE_RE = re.compile(rf"app-{re.escape(APP_ID)}-[A-Za-z0-9]+\.scope$")


def _in_app_scope() -> bool:
    """Запущены ли мы уже в правильном scope (тогда перезапуск не нужен)."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(_SCOPE_RE.search(line) for line in cgroup.splitlines())


def reexec_in_scope(argv: list[str]) -> None:
    """Перезапустить себя в `SCOPE_UNIT`, если это ещё не сделано.

    Портал GlobalShortcuts отказывает («An app id is required»), если процесс
    не живёт в юните вида `app-<app-id>-<суффикс>.scope`: app id он берёт из
    имени cgroup. Поэтому трей сам заходит в нужный scope через
    `systemd-run --user --scope`. Если systemd-run недоступен (или выставлен
    MOUSECFG_NO_SCOPE=1), работаем как есть — хоткеи просто не поднимутся.
    """
    if _in_app_scope() or os.environ.get("MOUSECFG_NO_SCOPE"):
        return
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None or not os.environ.get("XDG_RUNTIME_DIR"):
        logger.debug(t(
            "systemd-run недоступен: хоткеи портала, вероятно, не заработают",
            "systemd-run is unavailable: portal shortcuts will probably not work",
        ))
        return
    command = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={SCOPE_UNIT}",
        sys.executable,
        "-m",
        "mousecfg.tray",
        *argv,
    ]
    logger.info(t("перезапуск в scope %s", "restarting in scope %s"), SCOPE_UNIT)
    os.execv(systemd_run, command)


class Tray:
    def __init__(self) -> None:
        self.client = ipc.Client()
        self.devices: list[dict] = []
        self.device_key = ""
        self.remap_enabled = True
        self._shortcuts: hotkeys.GlobalShortcuts | None = None

        self.indicator = AppIndicator3.Indicator.new(
            INDICATOR_ID, ICON_NAME, AppIndicator3.IndicatorCategory.HARDWARE
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("mousecfg")
        self.indicator.set_menu(self._build_menu())

        self._refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)

    # -- меню ------------------------------------------------------------
    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        self.header_item = Gtk.MenuItem(label="mousecfg")
        self.header_item.set_sensitive(False)
        menu.append(self.header_item)

        self.device_item = Gtk.MenuItem(label=t("устройство: —", "device: —"))
        self.device_item.set_sensitive(False)
        menu.append(self.device_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.profile_section = Gtk.MenuItem(label=t("Профиль", "Profile"))
        self.profile_menu = Gtk.Menu()
        self.profile_section.set_submenu(self.profile_menu)
        menu.append(self.profile_section)

        next_item = Gtk.MenuItem(label=t("Следующий профиль", "Next profile"))
        next_item.connect("activate", lambda *_: self._call("profile-next"))
        menu.append(next_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.remap_item = Gtk.MenuItem(label=t("Кнопки: выключены", "Buttons: disabled"))
        self.remap_item.connect("activate", lambda *_: self._toggle_remap())
        menu.append(self.remap_item)

        gui_item = Gtk.MenuItem(label=t("Открыть окно", "Open window"))
        gui_item.connect("activate", lambda *_: self._open_gui())
        menu.append(gui_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label=t("Выход", "Quit"))
        # В GTK3 у MenuItem сигнал "activate", а не "clicked".
        quit_item.connect("activate", lambda *_: self._quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    # -- данные демона ---------------------------------------------------
    def _refresh(self) -> None:
        threading.Thread(target=self._refresh_worker, daemon=True, name="tray-refresh").start()

    def _refresh_worker(self) -> None:
        try:
            status = self.client.call("status")
        except ipc.IpcError:
            GLib.idle_add(self._apply_status, None)
            return
        GLib.idle_add(self._apply_status, status)

    def _apply_status(self, status: object) -> bool:
        if not isinstance(status, dict) or not status.get("devices"):
            self.header_item.set_label(t("mousecfg — демон недоступен", "mousecfg — daemon unavailable"))
            self._set_profile_items([])
            return False

        self.devices = list(status["devices"])
        device = self.devices[0]
        self.device_key = str(device.get("key") or "")
        self.remap_enabled = bool(device.get("remap_enabled", True))
        active = str(device.get("active_profile") or "—")
        self.header_item.set_label(f"mousecfg: {active}")
        self.device_item.set_label(str(device.get("name") or device.get("key") or "—"))
        self.remap_item.set_label(t(f"Кнопки: {'включены' if self.remap_enabled else 'выключены'}", f"Buttons: {'enabled' if self.remap_enabled else 'disabled'}"))
        self._set_profile_items(list(device.get("profiles") or []), active=active)
        return False

    def _set_profile_items(self, profile_ids: list[str], *, active: str = "") -> None:
        for child in self.profile_menu.get_children():
            self.profile_menu.remove(child)
        if not profile_ids:
            empty = Gtk.MenuItem(label=t("профилей нет", "no profiles"))
            empty.set_sensitive(False)
            self.profile_menu.append(empty)
        for profile_id in profile_ids:
            label = f"● {profile_id}" if profile_id == active else profile_id
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _w, pid=profile_id: self._call("profile-activate", profile=pid))
            self.profile_menu.append(item)
        self.profile_menu.show_all()

    # -- действия --------------------------------------------------------
    def _call(self, method: str, **params) -> None:
        payload = {"device": self.device_key, **params}
        threading.Thread(
            target=self._call_worker, args=(method, payload), daemon=True, name=f"tray-{method}"
        ).start()

    def _call_worker(self, method: str, params: dict) -> None:
        try:
            self.client.call(method, **params)
        except ipc.IpcError as exc:
            logger.warning("%s: %s", method, exc)
        GLib.idle_add(lambda: (self._refresh(), False)[1])

    def _toggle_remap(self) -> None:
        self._call("set-remap", enabled=not self.remap_enabled)

    def _open_gui(self) -> None:
        try:
            subprocess.Popen(["mousecfg-gui"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            subprocess.Popen(
                [sys.executable, "-m", "mousecfg.gui.app"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _quit(self) -> None:
        if self._shortcuts is not None:
            self._shortcuts.stop()
        Gtk.main_quit()

    def _on_timer(self) -> bool:
        self._refresh()
        return True

    # -- хоткеи ----------------------------------------------------------
    def start_shortcuts(self) -> None:
        """Подписаться на глобальные хоткеи через портал (если возможно).

        На niri/wlroots портала GlobalShortcuts нет: попытка его поднять не
        только бесполезна, но и плодит зависшие окна «Add Keyboard Shortcuts».
        Тогда хоткей вешает сам композитор — см. `mousecfg keybinds`.
        """
        if not hotkeys.portal_supported():
            logger.info(
                t("портал GlobalShortcuts не поддерживается на %s: хоткеи вешаются в конфиге "
                "композитора (mousecfg keybinds)",
                "the GlobalShortcuts portal is not supported on %s: shortcuts are set in the "
                "compositor config (mousecfg keybinds)"),
                hotkeys.current_compositor() or t("этом рабочем столе", "this desktop"),
            )
            return
        self._shortcuts = hotkeys.GlobalShortcuts(
            on_activated=self._on_shortcut, bus=None
        )
        try:
            self._shortcuts.start()
        except hotkeys.ShortcutError as exc:
            logger.warning(t("хоткеи недоступны: %s", "shortcuts are unavailable: %s"), exc)
            self._shortcuts = None

    def _on_shortcut(self, shortcut_id: str) -> None:
        if shortcut_id == "next-profile":
            self._call("profile-next")
        elif shortcut_id == "prev-profile":
            self._call("profile-prev")
        else:
            logger.debug(t("неизвестный хоткей %s", "unknown shortcut %s"), shortcut_id)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    no_shortcuts = "--no-shortcuts" in args
    args = [arg for arg in args if arg != "--no-shortcuts"]

    # Портал хоткеев определяет app id по имени systemd-юнита. Заходим в
    # правильный scope сами, чтобы `mousecfg-tray` работал из любого места.
    # На niri портала нет — перезапуск в scope бессмысленен, поэтому пропускаем.
    if not no_shortcuts and hotkeys.portal_supported():
        reexec_in_scope(args)

    if not ipc.Client().is_available():
        print(t("mousecfg: демон не запущен, трей всё равно будет ждать его.", "mousecfg: the daemon is not running; the tray will wait for it anyway."), file=sys.stderr)

    tray = Tray()
    if not no_shortcuts:
        tray.start_shortcuts()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        tray._quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
