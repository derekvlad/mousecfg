"""Хоткеи смены профиля через портал org.freedesktop.portal.GlobalShortcuts.

Портал — правильный способ повесить глобальный хоткей на Wayland, но у него
есть жёсткое требование: приложение должно быть запущено в systemd-юните с
именем `app-<app-id>-<суффикс>.scope`. Тогда xdg-desktop-portal определяет
app id по cgroup; иначе CreateSession падает с «An app id is required».

Портал асинхронный: `CreateSession` и `BindShortcuts` возвращают объект
запроса, а настоящий ответ приходит сигналом `Response` на
`org.freedesktop.portal.Request`. Нажатия приходят сигналом `Activated`.

Здесь только D-Bus, без GTK: модуль можно использовать и из CLI, и из трея.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

# Хоткеи, которые просит проект. id -> описание для диалога портала.
DEFAULT_SHORTCUTS: dict[str, str] = {
    "next-profile": "Следующий профиль mousecfg",
    "prev-profile": "Предыдущий профиль mousecfg",
}


class ShortcutError(RuntimeError):
    pass


class GlobalShortcuts:
    """Сессия портала с одним или несколькими глобальными хоткеями.

    `on_activated(id)` вызывается при нажатии хоткея (в главном цикле GLib).
    """

    def __init__(
        self,
        shortcuts: dict[str, str] | None = None,
        *,
        on_activated: Callable[[str], None] | None = None,
        bus: Gio.DBusConnection | None = None,
    ) -> None:
        self.shortcuts = dict(shortcuts or DEFAULT_SHORTCUTS)
        self.on_activated = on_activated
        self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session_handle: str | None = None
        self._active = False
        self._token = "mousecfg_hs1"

    # -- создание сессии -------------------------------------------------
    def start(self) -> str:
        """Создать сессию портала. Ответ приходит сигналом Response."""
        try:
            self.bus.signal_subscribe(
                None, REQUEST_IFACE, "Response", None, None, Gio.DBusSignalFlags.NONE, self._on_response
            )
            self.bus.signal_subscribe(
                None, IFACE, "Activated", None, None, Gio.DBusSignalFlags.NONE, self._on_activated
            )
            options = {
                "handle_token": GLib.Variant("s", self._token),
                "session_handle_token": GLib.Variant("s", "mousecfg_session"),
            }
            reply = self.bus.call_sync(
                PORTAL,
                PORTAL_PATH,
                IFACE,
                "CreateSession",
                GLib.Variant("(a{sv})", (options,)),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            request, = reply.unpack()
            self._token = str(request).rsplit("/", 1)[-1]
        except GLib.Error as exc:
            raise ShortcutError(
                "портал GlobalShortcuts недоступен. Запустите mousecfg в scope "
                f"app-io.github.mousecfg.MouseCfg-<суффикс>.scope. Причина: {exc.message}"
            ) from exc
        return str(request)

    def _on_response(self, _conn, _sender, path, _iface, _signal, params) -> None:
        code, results = params.unpack()
        if str(path).rsplit("/", 1)[-1] != self._token:
            return
        if code != 0:
            logger.warning("портал отказал в создании сессии хоткеев (код %d)", code)
            return
        handle = results.get("session_handle")
        if not handle:
            return
        self.session_handle = str(handle)
        logger.info("сессия хоткеев создана: %s", self.session_handle)
        self._bind()

    def _bind(self) -> None:
        assert self.session_handle is not None
        if self._active:
            return
        self._active = True
        entries = [
            (shortcut_id, {"description": GLib.Variant("s", description)})
            for shortcut_id, description in self.shortcuts.items()
        ]
        try:
            self.bus.call_sync(
                PORTAL,
                PORTAL_PATH,
                IFACE,
                "BindShortcuts",
                GLib.Variant(
                    "(oa(sa{sv})sa{sv})",
                    (self.session_handle, entries, "", {}),
                ),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            logger.warning("не удалось привязать хоткеи: %s", exc.message)
            return
        logger.info("хоткеи запрошены у портала: %s", ", ".join(self.shortcuts))

    def _on_activated(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        session, shortcut_id, _timestamp, _options = params.unpack()
        if self.session_handle is None or str(session) != self.session_handle:
            return
        logger.info("хоткей нажат: %s", shortcut_id)
        if self.on_activated is not None:
            self.on_activated(str(shortcut_id))

    # -- завершение ------------------------------------------------------
    def stop(self) -> None:
        if self.session_handle is None:
            return
        try:
            self.bus.call_sync(
                PORTAL,
                self.session_handle,
                SESSION_IFACE,
                "Close",
                None,
                GLib.VariantType("()"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error as exc:
            logger.debug("закрытие сессии хоткеев: %s", exc.message)
        finally:
            self.session_handle = None
            self._active = False


def portal_available() -> bool:
    """Грубая проверка, что портал вообще есть на шине."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bool(
            bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (PORTAL,)),
                GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            ).unpack()[0]
        )
    except GLib.Error:
        return False


def describe_shortcuts(shortcuts: Iterable[str]) -> str:
    return ", ".join(shortcuts)
