"""Главное окно: устройство, DPI, профили, кнопки.

Ключевая идея интерфейса: пользователь нигде не может нажать «записать в
мышь». Все переключатели описывают рантайм-состояние на хосте, а подпись
внизу окна постоянно напоминает, что настройки живут до отключения мыши.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from mousecfg.gui.client import DaemonClient

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 2
RATE_CHOICES: tuple[int, ...] = (1, 2, 4, 8)


def _buttons_to_text(buttons: object) -> str:
    """Словарь кнопок -> строка 'side1=copy, middle=macro:burst'."""
    if not isinstance(buttons, dict):
        return ""
    parts = []
    for name, action in buttons.items():
        if isinstance(action, dict):
            if "macro" in action:
                parts.append(f"{name}=macro:{action['macro']}")
            elif "key" in action:
                parts.append(f"{name}=key:{action['key']}")
            else:
                parts.append(f"{name}={action}")
        else:
            parts.append(f"{name}={action}")
    return ", ".join(parts)


def _parse_buttons_text(text: str) -> dict[str, object]:
    """Обратный разбор строки с назначениями кнопок."""
    buttons: dict[str, object] = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, action = chunk.partition("=")
        name = name.strip()
        action = action.strip()
        if not name or not action:
            raise ValueError(f"назначение кнопки должно быть BUTTON=ACTION: {chunk!r}")
        if action.startswith("macro:"):
            buttons[name] = {"macro": action[len("macro:") :].strip()}
        elif action.startswith("key:"):
            buttons[name] = {"key": action[len("key:") :].strip()}
        else:
            buttons[name] = action
    return buttons


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        self.set_default_size(880, 620)

        self.client = DaemonClient()
        self.devices: list[dict] = []
        self.device_key = ""
        self._profiles: dict[str, dict] = {}

        # Строки, которые мы сами добавили в каждую группу: libadwaita кладёт
        # их внутрь своей раскладки, поэтому удалять нужно именно наши виджеты.
        self._group_rows: dict[Adw.PreferencesGroup, list[Gtk.Widget]] = {}
        self._suppress = False

        self._build_ui()
        self._refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)

    # -- построение интерфейса ------------------------------------------
    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="mousecfg", subtitle="профили на хосте, без записи в мышь")
        )

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        refresh_button.connect("clicked", lambda *_: self._refresh())
        header.pack_start(refresh_button)

        self.status_label = Gtk.Label(label="демон не найден", xalign=0)
        self.status_label.add_css_class("dim-label")
        self.status_label.set_margin_top(6)
        self.status_label.set_margin_bottom(6)
        self.status_label.set_margin_start(12)
        self.status_label.set_margin_end(12)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.status_label)
        content.append(self._build_content())
        toolbar.set_content(content)

        self.toast_overlay = Adw.ToastOverlay(child=toolbar)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.toast_overlay)
        root.append(self._build_footer())
        self.set_content(root)

    def _build_content(self) -> Gtk.Widget:
        self.devices_group = Adw.PreferencesGroup(
            title="Мышь",
            description="Демон применяет профиль при подключении и при смене активного приложения",
        )
        self.mode_row = Adw.ActionRow(title="Режим устройства", subtitle="—")
        self.mode_badge = Gtk.Label(label="—")
        self.mode_badge.add_css_class("dim-label")
        self.mode_row.add_suffix(self.mode_badge)
        self.devices_group.add(self.mode_row)

        self.dpi_row = Adw.SpinRow.new_with_range(100, 8000, 50)
        self.dpi_row.set_title("DPI (волатильно)")
        self.dpi_row.set_subtitle("Меняется в оперативной памяти мыши; после отключения сбросится")
        self.dpi_row.connect("notify::value", self._on_dpi_changed)
        self.devices_group.add(self.dpi_row)

        self.rate_row = Adw.ComboRow(title="Частота опроса (волатильно)")
        self.rate_row.set_model(
            Gtk.StringList.new([f"{value} мс ({1000 // value} Гц)" for value in RATE_CHOICES])
        )
        self.rate_row.connect("notify::selected", self._on_rate_changed)
        self.devices_group.add(self.rate_row)

        self.profiles_group = Adw.PreferencesGroup(
            title="Профили",
            description="Профили хранятся в ~/.config/mousecfg/profiles и не зависят от памяти мыши",
        )
        add_profile_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Добавить профиль")
        add_profile_button.add_css_class("flat")
        add_profile_button.connect("clicked", lambda *_: self._open_profile_dialog(None))
        self.profiles_group.set_header_suffix(add_profile_button)
        self.buttons_group = Adw.PreferencesGroup(
            title="Кнопки",
            description="Ремап выполняет хост через evdev/uinput: мышь о нём не знает",
        )
        self.macros_group = Adw.PreferencesGroup(title="Макросы")

        page = Adw.PreferencesPage()
        page.add(self.devices_group)
        page.add(self.profiles_group)
        page.add(self.buttons_group)
        page.add(self.macros_group)
        return page

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(6)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        label = Gtk.Label(
            label="Настройки живут в оперативной памяти мыши и не пишутся во встроенную память.",
            xalign=0,
        )
        label.add_css_class("dim-label")
        footer.append(label)
        return footer

    # -- обновление ------------------------------------------------------
    def _on_timer(self) -> bool:
        self._refresh()
        return True

    def _refresh(self) -> None:
        self.client.call_async("status", on_done=self._on_status, on_error=self._on_error)

    def _on_error(self, message: str) -> None:
        self.status_label.set_text(f"демон недоступен: {message}")
        self._clear_group(self.profiles_group)
        self._clear_group(self.buttons_group)
        self._clear_group(self.macros_group)
        self.dpi_row.set_sensitive(False)
        self.rate_row.set_sensitive(False)

    def _on_status(self, status: object) -> None:
        data = dict(status)  # type: ignore[arg-type]
        self.devices = list(data.get("devices", []))
        watcher = data.get("focus_watcher") or "нет"
        focused = data.get("focused_app") or "—"
        self.status_label.set_text(f"активное приложение: {focused}  ·  источник: {watcher}")

        if not self.devices:
            self._on_error("нет устройств под управлением")
            return

        device = self.devices[0]
        self.device_key = str(device.get("key") or "")
        self.dpi_row.set_sensitive(True)
        self.rate_row.set_sensitive(True)

        mode = device.get("mode") or "неизвестен"
        self.mode_badge.set_text(mode)
        if mode == "onboard":
            self.mode_row.set_subtitle(
                "On-Board: настройки с хоста могут игнорироваться. Переключите в host командой "
                "mousecfg mode host --allow-onboard-switch (однократная запись)"
            )
        else:
            self.mode_row.set_subtitle(str(device.get("name") or ""))

        self._sync_settings(device)
        self._render_profiles(device)
        self._render_buttons(device)
        self._render_macros()

    def _sync_settings(self, device: dict) -> None:
        """Показать текущие DPI/rate, не отправив случайную команду."""
        dpi = device.get("dpi")
        rate = device.get("report_rate_ms")
        if dpi is None and rate is None:
            return

        self._suppress = True
        try:
            if dpi is not None:
                self.dpi_row.set_value(float(dpi))
            if rate is not None and int(rate) in RATE_CHOICES:
                self.rate_row.set_selected(RATE_CHOICES.index(int(rate)))
        finally:
            self._suppress = False

    def _render_profiles(self, device: dict) -> None:
        self._clear_group(self.profiles_group)
        active = str(device.get("active_profile") or "")
        profiles_file = str(device.get("profiles_file") or "")
        profile_ids = list(device.get("profiles") or [])
        self._profiles = {str(item["id"]): item for item in self._profile_details(device)}

        for profile_id in profile_ids:
            detail = self._profiles.get(profile_id, {})
            summary = []
            if detail.get("dpi"):
                summary.append(f"DPI {detail['dpi']}")
            if detail.get("report_rate_ms"):
                summary.append(f"{detail['report_rate_ms']} мс")
            if detail.get("match_apps"):
                summary.append("приложения: " + ", ".join(detail["match_apps"]))
            row = Adw.ActionRow(title=str(profile_id), subtitle=" · ".join(summary) or profiles_file)
            if profile_id == active:
                row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))

            apply_button = Gtk.Button(label="Применить")
            apply_button.add_css_class("flat")
            apply_button.connect("clicked", self._on_apply_clicked, str(profile_id))
            row.add_suffix(apply_button)

            edit_button = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Изменить")
            edit_button.add_css_class("flat")
            edit_button.connect("clicked", lambda _b, pid=profile_id: self._open_profile_dialog(pid))
            row.add_suffix(edit_button)

            remove_button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Удалить")
            remove_button.add_css_class("flat")
            remove_button.connect("clicked", lambda _b, pid=profile_id: self._on_profile_remove(pid))
            row.add_suffix(remove_button)

            self._add_row(self.profiles_group, row)

        if not profile_ids:
            self._add_row(
                self.profiles_group,
                Adw.ActionRow(
                    title="Профили не загружены",
                    subtitle=profiles_file or "файл профилей пока не создан",
                ),
            )

        if device.get("last_error"):
            self._add_row(
                self.profiles_group,
                Adw.ActionRow(title="Последняя ошибка", subtitle=str(device["last_error"])),
            )

    def _profile_details(self, device: dict) -> list[dict]:
        """Детали профилей, если демон их прислал (иначе — пусто)."""
        details = device.get("profile_details")
        if isinstance(details, list):
            return [dict(item) for item in details]
        return []

    def _render_buttons(self, device: dict) -> None:
        self._clear_group(self.buttons_group)
        plan = device.get("buttons_plan") or {}
        if not plan:
            self._add_row(
                self.buttons_group,
                Adw.ActionRow(
                    title="Кнопки не переназначены",
                    subtitle="Добавьте их в профиль — ремап выполняется на хосте",
                ),
            )
            return
        for name, action in plan.items():
            row = Adw.ActionRow(title=str(name), subtitle=str(action))
            row.add_prefix(Gtk.Image.new_from_icon_name("input-mouse-symbolic"))
            self._add_row(self.buttons_group, row)

    def _render_macros(self) -> None:
        self.client.call_async("list-macros", on_done=self._on_macros, on_error=lambda _message: None)

    def _on_macros(self, payload: object) -> None:
        data = dict(payload)  # type: ignore[arg-type]
        macros = list(data.get("macros", []))
        self._clear_group(self.macros_group)
        if not macros:
            self._add_row(
                self.macros_group,
                Adw.ActionRow(title="Макросов нет", subtitle="Опишите их в том же TOML-файле"),
            )
            return
        for macro in macros:
            title = f"{macro['id']} — {macro['name']}" if macro.get("name") else str(macro["id"])
            row = Adw.ActionRow(title=title, subtitle=str(macro.get("describe", "")))
            row.add_prefix(Gtk.Image.new_from_icon_name("input-keyboard-symbolic"))
            self._add_row(self.macros_group, row)

    # -- вспомогательное -------------------------------------------------
    def _add_row(self, group: Adw.PreferencesGroup, row: Gtk.Widget) -> None:
        group.add(row)
        self._group_rows.setdefault(group, []).append(row)

    def _clear_group(self, group: Adw.PreferencesGroup) -> None:
        for row in self._group_rows.pop(group, []):
            group.remove(row)

    # -- действия --------------------------------------------------------
    def _on_apply_clicked(self, _button: Gtk.Button, profile_id: str) -> None:
        self.client.call_async(
            "apply-profile",
            on_done=lambda _result: self._toast(f"Применено: {profile_id}"),
            on_error=self._on_error,
            device=self.device_key,
            profile=profile_id,
        )
        GLib.timeout_add(300, self._refresh_done)

    def _refresh_done(self) -> bool:
        self._refresh()
        return False

    def _on_dpi_changed(self, row: Adw.SpinRow, _pspec: object) -> None:
        if self._suppress:
            return
        value = int(row.get_value())
        self.client.call_async(
            "set-dpi",
            on_done=lambda _result: self._toast(f"DPI {value} (волатильно)"),
            on_error=self._on_error,
            device=self.device_key,
            value=value,
        )

    def _on_rate_changed(self, row: Adw.ComboRow, _pspec: object) -> None:
        if self._suppress:
            return
        index = int(row.get_selected())
        if not 0 <= index < len(RATE_CHOICES):
            return
        milliseconds = RATE_CHOICES[index]
        self.client.call_async(
            "set-rate",
            on_done=lambda _result: self._toast(f"Частота опроса {milliseconds} мс"),
            on_error=self._on_error,
            device=self.device_key,
            value=milliseconds,
        )

    # -- редактирование профилей ----------------------------------------
    def _open_profile_dialog(self, profile_id: str | None) -> None:
        if not self.device_key:
            self._toast("Устройство не выбрано")
            return
        existing = self._profiles.get(profile_id or "", {})
        dialog = Adw.Window(
            transient_for=self,
            modal=True,
            title="Профиль" if profile_id else "Новый профиль",
            default_width=420,
            default_height=520,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=existing.get("name") or (profile_id or "Новый профиль")))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Основное")

        id_row = Adw.EntryRow(title="id (латиницей, без пробелов)")
        id_row.set_text(profile_id or "")
        id_row.set_editable(not profile_id)
        group.add(id_row)

        name_row = Adw.EntryRow(title="Название")
        name_row.set_text(str(existing.get("name") or ""))
        group.add(name_row)

        dpi_row = Adw.SpinRow.new_with_range(100, 32000, 50)
        dpi_row.set_title("DPI")
        dpi_row.set_value(float(existing.get("dpi") or self.dpi_row.get_value() or 1600))
        group.add(dpi_row)

        rate_row = Adw.ComboRow(title="Частота опроса")
        rate_row.set_model(Gtk.StringList.new([f"{value} мс ({1000 // value} Гц)" for value in RATE_CHOICES]))
        current_rate = int(existing.get("report_rate_ms") or 0)
        rate_row.set_selected(RATE_CHOICES.index(current_rate) if current_rate in RATE_CHOICES else 0)
        group.add(rate_row)

        apps_row = Adw.EntryRow(title="match_apps (через запятую)")
        apps_row.set_text(", ".join(existing.get("match_apps") or []))
        group.add(apps_row)

        buttons_group = Adw.PreferencesGroup(
            title="Кнопки",
            description="side1=copy, middle=macro:burst, right=ignore",
        )
        buttons_row = Adw.EntryRow(title="Назначения (через запятую)")
        buttons_row.set_text(_buttons_to_text(existing.get("buttons")))
        buttons_group.add(buttons_row)

        page.add(group)
        page.add(buttons_group)
        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda *_: dialog.close())
        save = Gtk.Button(label="Сохранить")
        save.add_css_class("suggested-action")
        save.connect(
            "clicked",
            lambda *_: self._save_profile(
                dialog,
                profile_id or id_row.get_text(),
                name_row.get_text(),
                int(dpi_row.get_value()),
                RATE_CHOICES[int(rate_row.get_selected())],
                apps_row.get_text(),
                buttons_row.get_text(),
            ),
        )
        footer.append(cancel)
        footer.append(save)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(toolbar)
        root.append(footer)
        dialog.set_content(root)
        dialog.present()

    def _save_profile(
        self,
        dialog: Adw.Window,
        profile_id: str,
        name: str,
        dpi: int,
        rate_ms: int,
        apps_text: str,
        buttons_text: str,
    ) -> None:
        profile_id = profile_id.strip()
        if not profile_id:
            self._toast("Нужен id профиля")
            return
        try:
            buttons = _parse_buttons_text(buttons_text)
        except ValueError as exc:
            self._toast(str(exc))
            return

        apps = [item.strip() for item in apps_text.split(",") if item.strip()]
        self.client.call_async(
            "profile-upsert",
            on_done=lambda result: self._profile_saved(dialog, result, profile_id),
            on_error=self._on_error,
            device=self.device_key,
            id=profile_id,
            name=name.strip(),
            dpi=dpi,
            report_rate=str(rate_ms),
            buttons=buttons,
            match_apps=apps,
        )

    def _profile_saved(self, dialog: Adw.Window, result: object, profile_id: str) -> None:
        dialog.close()
        replaced = bool(dict(result).get("replaced")) if isinstance(result, dict) else False
        self._toast(f"Профиль {profile_id} {'изменён' if replaced else 'добавлен'}")
        self._refresh()

    def _on_profile_remove(self, profile_id: str) -> None:
        self.client.call_async(
            "profile-remove",
            on_done=lambda _result: self._profile_removed(profile_id),
            on_error=self._on_error,
            device=self.device_key,
            id=profile_id,
        )

    def _profile_removed(self, profile_id: str) -> None:
        self._toast(f"Профиль {profile_id} удалён")
        self._refresh()

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=2))
