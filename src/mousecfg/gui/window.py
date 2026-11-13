"""Главное окно: устройство, DPI, профили, кнопки.

Ключевая идея интерфейса: пользователь нигде не может нажать «записать в
мышь». Все переключатели описывают рантайм-состояние на хосте, а подпись
внизу окна постоянно напоминает, что настройки живут до отключения мыши.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from mousecfg.gui.client import DaemonClient
from mousecfg.gui.recorder import MacroRecordingDialog
from mousecfg.i18n import t

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 2
RATE_CHOICES: tuple[int, ...] = (1, 2, 4, 8)

# Эффекты подсветки, которые стоит предлагать в GUI (подмножество LIGHTING_EFFECTS).
LIGHTING_EFFECT_CHOICES: tuple[str, ...] = (
    "disabled",
    "static",
    "breathe",
    "cycle",
    "wave",
    "ripple",
    "decomposition",
)
# Подсветка есть не у каждого устройства; значение берём из status.
DEFAULT_LIGHTING_COLOR = "#00a0ff"

# Кнопки мыши, которые можно назначить в профиле, и их подписи.
# side1 — это «задняя боковая» кнопка (её часто зовут button4/Back),
# side2 — «передняя боковая» (button5/Forward).
MOUSE_BUTTON_CHOICES: tuple[str, ...] = ("left", "middle", "right", "side1", "side2", "dpi")
MOUSE_BUTTON_LABELS: dict[str, str] = {
    "left": t("left — левая", "left — left button"),
    "middle": t("middle — средняя (колесо)", "middle — middle button (wheel)"),
    "right": t("right — правая", "right — right button"),
    "side1": t("side1 — задняя боковая (button4/Back)", "side1 — rear side button (button4/Back)"),
    "side2": t("side2 — передняя боковая (button5/Forward)", "side2 — front side button (button5/Forward)"),
    "dpi": t("dpi — кнопка переключения DPI", "dpi — DPI switch button"),
}


def _describe_binding(action: object) -> str:
    """Человекочитаемое описание действия кнопки для списка в диалоге."""
    if isinstance(action, dict):
        if "macro" in action:
            return t(f"макрос {action['macro']}", f"macro {action['macro']}")
        if "key" in action:
            return t(f"клавиши {action['key']}", f"keys {action['key']}")
        return str(action)
    return str(action)


def _macro_id_for(profile_id: str, button: str) -> str:
    """Id макроса по профилю и кнопке: удобно и без пробелов."""
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in profile_id)
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-") or "profile"
    return f"{safe}-{button}"


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


def _hex_from_rgba(rgba: object) -> str:
    """Gdk.RGBA -> '#rrggbb' (для профиля и RPC)."""
    red = int(round(getattr(rgba, "red", 0.0) * 255))
    green = int(round(getattr(rgba, "green", 0.0) * 255))
    blue = int(round(getattr(rgba, "blue", 0.0) * 255))
    return f"#{red:02x}{green:02x}{blue:02x}"


def _rgba_from_hex(text: str):
    """'#rrggbb' -> Gdk.RGBA; при мусоре возвращаем чёрный."""
    from gi.repository import Gdk

    rgba = Gdk.RGBA()
    if not rgba.parse(text):
        rgba.parse("#000000")
    return rgba


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
            raise ValueError(t(f"назначение кнопки должно быть BUTTON=ACTION: {chunk!r}", f"a button binding must be BUTTON=ACTION: {chunk!r}"))
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
        self._record_macro_button: Gtk.Button | None = None

        self._build_ui()
        self._refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)

    # -- построение интерфейса ------------------------------------------
    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="mousecfg", subtitle=t("профили на хосте, без записи в мышь", "profiles on the host, nothing written to the mouse"))
        )

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=t("Обновить", "Refresh"))
        refresh_button.connect("clicked", lambda *_: self._refresh())
        header.pack_start(refresh_button)

        self.status_label = Gtk.Label(label=t("демон не найден", "daemon not found"), xalign=0)
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
            title=t("Мышь", "Mouse"),
            description=t("Демон применяет профиль при подключении и при смене активного приложения", "The daemon applies a profile on connect and when the focused application changes"),
        )
        self.mode_row = Adw.ActionRow(title=t("Режим устройства", "Device mode"), subtitle="—")
        self.mode_badge = Gtk.Label(label="—")
        self.mode_badge.add_css_class("dim-label")
        self.mode_row.add_suffix(self.mode_badge)
        self.devices_group.add(self.mode_row)

        self.dpi_row = Adw.SpinRow.new_with_range(100, 8000, 50)
        self.dpi_row.set_title(t("DPI (волатильно)", "DPI (volatile)"))
        self.dpi_row.set_subtitle(t("Меняется в оперативной памяти мыши; после отключения сбросится", "Changed in the mouse RAM; resets after disconnection"))
        self.dpi_row.connect("notify::value", self._on_dpi_changed)
        self.devices_group.add(self.dpi_row)

        self.rate_row = Adw.ComboRow(title=t("Частота опроса (волатильно)", "Report rate (volatile)"))
        self.rate_row.set_model(
            Gtk.StringList.new([t(f"{value} мс ({1000 // value} Гц)", f"{value} ms ({1000 // value} Hz)") for value in RATE_CHOICES])
        )
        self.rate_row.connect("notify::selected", self._on_rate_changed)
        self.devices_group.add(self.rate_row)

        self.profiles_group = Adw.PreferencesGroup(
            title=t("Профили", "Profiles"),
            description=t("Профили хранятся в ~/.config/mousecfg/profiles и не зависят от памяти мыши", "Profiles are stored in ~/.config/mousecfg/profiles and do not depend on mouse memory"),
        )
        add_profile_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=t("Добавить профиль", "Add profile"))
        add_profile_button.add_css_class("flat")
        add_profile_button.connect("clicked", lambda *_: self._open_profile_dialog(None))
        self.profiles_group.set_header_suffix(add_profile_button)

        self.lighting_group = Adw.PreferencesGroup(
            title=t("Подсветка", "Lighting"),
            description=t("Эффект применяется волатильно; во встроенную память мыши ничего не пишется", "The effect is applied volatilely; nothing is written to onboard memory"),
        )
        self.lighting_group.set_visible(False)

        self.lighting_effect_row = Adw.ComboRow(title=t("Эффект", "Effect"))
        self.lighting_effect_row.set_model(Gtk.StringList.new(list(LIGHTING_EFFECT_CHOICES)))
        self.lighting_effect_row.connect("notify::selected", self._on_lighting_changed)
        self.lighting_group.add(self.lighting_effect_row)

        self.lighting_color_row = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), tooltip_text=t("Цвет подсветки", "Lighting colour")
        )
        self.lighting_color_row.set_rgba(_rgba_from_hex(DEFAULT_LIGHTING_COLOR))
        self.lighting_color_row.connect("notify::rgba", self._on_lighting_changed)
        color_row = Adw.ActionRow(title=t("Цвет", "Colour"))
        color_row.add_suffix(self.lighting_color_row)
        self.lighting_group.add(color_row)

        self.lighting_brightness_row = Adw.SpinRow.new_with_range(0, 100, 5)
        self.lighting_brightness_row.set_title(t("Яркость, %", "Brightness, %"))
        self.lighting_brightness_row.set_value(100)
        self.lighting_brightness_row.connect("notify::value", self._on_lighting_changed)
        self.lighting_group.add(self.lighting_brightness_row)

        self.buttons_group = Adw.PreferencesGroup(
            title=t("Кнопки", "Buttons"),
            description=t("Ремап выполняет хост через evdev/uinput: мышь о нём не знает", "Remapping is done by the host via evdev/uinput: the mouse knows nothing about it"),
        )
        self.macros_group = Adw.PreferencesGroup(title=t("Макросы", "Macros"))

        page = Adw.PreferencesPage()
        page.add(self.devices_group)
        page.add(self.profiles_group)
        page.add(self.lighting_group)
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
            label=t("Настройки живут в оперативной памяти мыши и не пишутся во встроенную память.", "Settings live in the mouse RAM and are not written to onboard memory."),
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
        self.status_label.set_text(t(f"демон недоступен: {message}", f"daemon unavailable: {message}"))
        self._clear_group(self.profiles_group)
        self._clear_group(self.buttons_group)
        self._clear_group(self.macros_group)
        self.dpi_row.set_sensitive(False)
        self.rate_row.set_sensitive(False)

    def _on_status(self, status: object) -> None:
        data = dict(status)  # type: ignore[arg-type]
        self.devices = list(data.get("devices", []))
        watcher = data.get("focus_watcher") or t("нет", "none")
        focused = data.get("focused_app") or "—"
        self.status_label.set_text(t(f"активное приложение: {focused}  ·  источник: {watcher}", f"focused application: {focused}  ·  source: {watcher}"))

        if not self.devices:
            self._on_error(t("нет устройств под управлением", "no managed devices"))
            return

        device = self.devices[0]
        self.device_key = str(device.get("key") or "")
        capabilities = set(device.get("capabilities") or [])
        protocol = str(device.get("protocol") or "hidpp")
        self.dpi_row.set_sensitive("dpi" in capabilities)
        self.rate_row.set_sensitive("report_rate" in capabilities)

        mode = device.get("mode") or ("evdev" if protocol == "evdev" else t("неизвестен", "unknown"))
        self.mode_badge.set_text(mode)
        if protocol == "evdev":
            self.mode_row.set_subtitle(
                t("Модель без HID++: профили, кнопки и макросы работают, "
                "а DPI/частота/подсветка настраиваются только самой мышью",
                "Model without HID++: profiles, buttons and macros work, "
                "while DPI/report rate/lighting are set on the mouse itself")
            )
        elif mode == "onboard":
            self.mode_row.set_subtitle(
                t("On-Board: настройки с хоста могут игнорироваться. Переключите в host командой "
                "mousecfg mode host --allow-onboard-switch (однократная запись)",
                "On-Board: host settings may be ignored. Switch to host with "
                "mousecfg mode host --allow-onboard-switch (a one-time write)")
            )
        else:
            self.mode_row.set_subtitle(str(device.get("name") or ""))

        self._sync_settings(device)
        self.lighting_group.set_visible(bool(device.get("lighting_supported")))
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
                summary.append(t(f"{detail['report_rate_ms']} мс", f"{detail['report_rate_ms']} ms"))
            if detail.get("match_apps"):
                summary.append(t("приложения: ", "applications: ") + ", ".join(detail["match_apps"]))
            row = Adw.ActionRow(title=str(profile_id), subtitle=" · ".join(summary) or profiles_file)
            if profile_id == active:
                row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))

            apply_button = Gtk.Button(label=t("Применить", "Apply"))
            apply_button.add_css_class("flat")
            apply_button.connect("clicked", self._on_apply_clicked, str(profile_id))
            row.add_suffix(apply_button)

            edit_button = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text=t("Изменить", "Edit"))
            edit_button.add_css_class("flat")
            edit_button.connect("clicked", lambda _b, pid=profile_id: self._open_profile_dialog(pid))
            row.add_suffix(edit_button)

            remove_button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=t("Удалить", "Delete"))
            remove_button.add_css_class("flat")
            remove_button.connect("clicked", lambda _b, pid=profile_id: self._on_profile_remove(pid))
            row.add_suffix(remove_button)

            self._add_row(self.profiles_group, row)

        if not profile_ids:
            self._add_row(
                self.profiles_group,
                Adw.ActionRow(
                    title=t("Профили не загружены", "Profiles not loaded"),
                    subtitle=profiles_file or t("файл профилей пока не создан", "the profiles file has not been created yet"),
                ),
            )

        if device.get("last_error"):
            self._add_row(
                self.profiles_group,
                Adw.ActionRow(title=t("Последняя ошибка", "Last error"), subtitle=str(device["last_error"])),
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
                    title=t("Кнопки не переназначены", "Buttons are not remapped"),
                    subtitle=t("Добавьте их в профиль — ремап выполняется на хосте", "Add them to a profile — remapping is done on the host"),
                ),
            )
            return
        for name, action in plan.items():
            row = Adw.ActionRow(title=str(name), subtitle=str(action))
            row.add_prefix(Gtk.Image.new_from_icon_name("input-mouse-symbolic"))
            self._add_row(self.buttons_group, row)

    def _render_macros(self) -> None:
        if self._record_macro_button is None:
            self._record_macro_button = Gtk.Button(
                icon_name="media-record-symbolic", tooltip_text=t("Записать макрос", "Record macro")
            )
            self._record_macro_button.add_css_class("flat")
            self._record_macro_button.connect("clicked", lambda *_: self._open_macro_recorder())
            self.macros_group.set_header_suffix(self._record_macro_button)
        self.client.call_async("list-macros", on_done=self._on_macros, on_error=lambda _message: None)

    def _on_macros(self, payload: object) -> None:
        data = dict(payload)  # type: ignore[arg-type]
        macros = list(data.get("macros", []))
        self._clear_group(self.macros_group)
        if not macros:
            self._add_row(
                self.macros_group,
                Adw.ActionRow(
                    title=t("Макросов нет", "No macros"),
                    subtitle=t("Нажмите «Записать» и нажмите нужные клавиши", "Press “Record” and press the keys you need"),
                ),
            )
            return
        for macro in macros:
            title = f"{macro['id']} — {macro['name']}" if macro.get("name") else str(macro["id"])
            row = Adw.ActionRow(title=title, subtitle=str(macro.get("describe", "")))
            row.add_prefix(Gtk.Image.new_from_icon_name("input-keyboard-symbolic"))

            edit_button = Gtk.Button(icon_name="media-record-symbolic", tooltip_text=t("Перезаписать", "Re-record"))
            edit_button.add_css_class("flat")
            edit_button.connect(
                "clicked",
                lambda _b, m=macro: self._open_macro_recorder(str(m["id"]), str(m.get("name", ""))),
            )
            row.add_suffix(edit_button)

            remove_button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=t("Удалить", "Delete"))
            remove_button.add_css_class("flat")
            remove_button.connect("clicked", lambda _b, mid=str(macro["id"]): self._remove_macro(mid))
            row.add_suffix(remove_button)

            self._add_row(self.macros_group, row)

    # -- запись макроса ---------------------------------------------------
    def _open_macro_recorder(self, macro_id: str = "", name: str = "") -> None:
        if not self.device_key:
            self._toast(t("Устройство не выбрано", "No device selected"))
            return
        dialog = MacroRecordingDialog(
            self,
            on_save=self._save_recorded_macro,
            macro_id=macro_id,
            name=name,
        )
        dialog.present()

    def _record_macro_for_button(
        self,
        profile_id: str,
        button: str,
        buttons_row: Adw.EntryRow,
    ) -> None:
        """Записать макрос для выбранной кнопки профиля.

        Сначала выбирается кнопка мыши, затем открывается запись нажатий.
        Готовый макрос получает предсказуемый id (профиль-кнопка) и сразу
        прописывается в назначения кнопки — пользователю не нужно вручную
        вспоминать id макроса.
        """
        if not self.device_key:
            self._toast(t("Устройство не выбрано", "No device selected"))
            return
        profile_id = profile_id.strip()
        if not profile_id:
            self._toast(t("Сначала укажите id профиля", "Specify a profile id first"))
            return
        macro_id = _macro_id_for(profile_id, button)
        dialog = MacroRecordingDialog(
            self,
            macro_id=macro_id,
            name=f"{profile_id}: {button}",
            on_save=lambda mid, interval, steps: self._save_recorded_macro(
                mid, interval, steps, button=button, buttons_row=buttons_row
            ),
        )
        dialog.present()

    def _save_recorded_macro(
        self,
        macro_id: str,
        interval_ms: int,
        steps: list[dict],
        *,
        button: str = "",
        buttons_row: Adw.EntryRow | None = None,
    ) -> None:
        self.client.call_async(
            "macro-upsert",
            on_done=lambda _result: self._macro_saved(
                macro_id, button=button, buttons_row=buttons_row
            ),
            on_error=self._on_error,
            device=self.device_key,
            id=macro_id,
            interval_ms=interval_ms,
            steps=steps,
            replace=True,
        )

    def _macro_saved(
        self,
        macro_id: str,
        *,
        button: str = "",
        buttons_row: Adw.EntryRow | None = None,
    ) -> None:
        if button and buttons_row is not None:
            # Привязываем макрос к выбранной кнопке прямо в строке назначений.
            current = _parse_buttons_text(buttons_row.get_text())
            current[button] = {"macro": macro_id}
            buttons_row.set_text(_buttons_to_text(current))
            self._toast(t(f"Макрос {macro_id} записан и назначен на кнопку {button}", f"Macro {macro_id} recorded and bound to button {button}"))
        else:
            self._toast(t(f"Макрос {macro_id} сохранён", f"Macro {macro_id} saved"))
        self._refresh()

    def _remove_macro(self, macro_id: str) -> None:
        self.client.call_async(
            "macro-remove",
            on_done=lambda _result: self._macro_removed(macro_id),
            on_error=self._on_error,
            device=self.device_key,
            id=macro_id,
        )

    def _macro_removed(self, macro_id: str) -> None:
        self._toast(t(f"Макрос {macro_id} удалён", f"Macro {macro_id} deleted"))
        self._refresh()

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
            on_done=lambda _result: self._toast(t(f"Применено: {profile_id}", f"Applied: {profile_id}")),
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
            on_done=lambda _result: self._toast(t(f"DPI {value} (волатильно)", f"DPI {value} (volatile)")),
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
            on_done=lambda _result: self._toast(t(f"Частота опроса {milliseconds} мс", f"Report rate {milliseconds} ms")),
            on_error=self._on_error,
            device=self.device_key,
            value=milliseconds,
        )

    # -- подсветка --------------------------------------------------------
    def _on_lighting_changed(self, *_args: object) -> None:
        if self._suppress or not self.device_key:
            return
        effect = LIGHTING_EFFECT_CHOICES[int(self.lighting_effect_row.get_selected())]
        params: dict[str, object] = {
            "device": self.device_key,
            "effect": effect,
            "color": _hex_from_rgba(self.lighting_color_row.get_rgba()),
        }
        if effect == "disabled":
            # У «выключено» нет цвета/яркости: достаточно снять эффект.
            self.client.call_async("set-lighting", on_done=lambda _r: None, on_error=self._on_error, **params)
            return
        params["brightness"] = int(self.lighting_brightness_row.get_value())
        self.client.call_async(
            "set-lighting",
            on_done=lambda _result: self._toast(t(f"Подсветка: {effect} (волатильно)", f"Lighting: {effect} (volatile)")),
            on_error=self._on_error,
            **params,
        )

    # -- редактирование профилей ----------------------------------------
    def _open_profile_dialog(self, profile_id: str | None) -> None:
        if not self.device_key:
            self._toast(t("Устройство не выбрано", "No device selected"))
            return
        existing = self._profiles.get(profile_id or "", {})
        dialog = Adw.Window(
            transient_for=self,
            modal=True,
            title=t("Профиль", "Profile") if profile_id else t("Новый профиль", "New profile"),
            default_width=420,
            default_height=520,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=existing.get("name") or (profile_id or t("Новый профиль", "New profile"))))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=t("Основное", "General"))

        id_row = Adw.EntryRow(title=t("id (латиницей, без пробелов)", "id (latin letters, no spaces)"))
        id_row.set_text(profile_id or "")
        id_row.set_editable(not profile_id)
        group.add(id_row)

        name_row = Adw.EntryRow(title=t("Название", "Name"))
        name_row.set_text(str(existing.get("name") or ""))
        group.add(name_row)

        dpi_row = Adw.SpinRow.new_with_range(100, 32000, 50)
        dpi_row.set_title("DPI")
        dpi_row.set_value(float(existing.get("dpi") or self.dpi_row.get_value() or 1600))
        group.add(dpi_row)

        rate_row = Adw.ComboRow(title=t("Частота опроса", "Report rate"))
        rate_row.set_model(Gtk.StringList.new([t(f"{value} мс ({1000 // value} Гц)", f"{value} ms ({1000 // value} Hz)") for value in RATE_CHOICES]))
        current_rate = int(existing.get("report_rate_ms") or 0)
        rate_row.set_selected(RATE_CHOICES.index(current_rate) if current_rate in RATE_CHOICES else 0)
        group.add(rate_row)

        apps_row = Adw.EntryRow(title=t("match_apps (через запятую)", "match_apps (comma-separated)"))
        apps_row.set_text(", ".join(existing.get("match_apps") or []))
        group.add(apps_row)

        buttons_group = Adw.PreferencesGroup(
            title=t("Кнопки", "Buttons"),
            description="side1=copy, middle=macro:burst, right=ignore",
        )
        buttons_row = Adw.EntryRow(title=t("Назначения (через запятую)", "Bindings (comma-separated)"))
        buttons_row.set_text(_buttons_to_text(existing.get("buttons")))
        buttons_group.add(buttons_row)

        # Кнопка -> макрос: выбираем кнопку мыши, затем пишем макрос нажатиями.
        record_group = Adw.PreferencesGroup(
            title=t("Кнопка и макрос", "Button and macro"),
            description=(
                t("Выберите кнопку мыши, нажмите «Записать макрос» и нажмите нужные клавиши. "
                "Макрос исполнит хост; в мышь ничего не пишется.",
                "Choose a mouse button, press “Record macro” and press the keys you need. "
                "The host runs the macro; nothing is written to the mouse.")
            ),
        )
        button_choice = Adw.ComboRow(title=t("Кнопка мыши", "Mouse button"))
        button_choice.set_model(Gtk.StringList.new([MOUSE_BUTTON_LABELS[name] for name in MOUSE_BUTTON_CHOICES]))
        # По умолчанию подставляем боковую кнопку — её чаще всего и назначают.
        button_choice.set_selected(MOUSE_BUTTON_CHOICES.index("side1"))
        record_group.add(button_choice)

        record_macro_button = Gtk.Button(label=t("Записать макрос", "Record macro"))
        record_macro_button.add_css_class("suggested-action")
        record_macro_button.connect(
            "clicked",
            lambda *_: self._record_macro_for_button(
                profile_id or id_row.get_text(),
                MOUSE_BUTTON_CHOICES[int(button_choice.get_selected())],
                buttons_row,
            ),
        )
        bindings_row = Adw.ActionRow(title=t("Текущие назначения", "Current bindings"))
        bindings_row.set_subtitle(_buttons_to_text(existing.get("buttons")) or t("пока пусто", "empty for now"))
        bindings_row.add_suffix(record_macro_button)
        record_group.add(bindings_row)
        buttons_row.connect(
            "changed",
            lambda row: bindings_row.set_subtitle(row.get_text() or t("пока пусто", "empty for now")),
        )

        lighting_group = Adw.PreferencesGroup(
            title=t("Подсветка профиля", "Profile lighting"),
            description=t("Применяется вместе с профилем; ничего не пишется в память мыши", "Applied together with the profile; nothing is written to mouse memory"),
        )
        lighting = existing.get("lighting") or {}
        lighting_effect = Adw.ComboRow(title=t("Эффект", "Effect"))
        lighting_effect.set_model(Gtk.StringList.new(list(LIGHTING_EFFECT_CHOICES)))
        current_effect = str(lighting.get("effect") or "disabled")
        lighting_effect.set_selected(
            LIGHTING_EFFECT_CHOICES.index(current_effect)
            if current_effect in LIGHTING_EFFECT_CHOICES
            else 0
        )
        lighting_group.add(lighting_effect)

        lighting_color = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), tooltip_text=t("Цвет подсветки", "Lighting colour")
        )
        color_value = lighting.get("color")
        if isinstance(color_value, int):
            lighting_color.set_rgba(_rgba_from_hex(f"#{color_value:06x}"))
        else:
            lighting_color.set_rgba(_rgba_from_hex(DEFAULT_LIGHTING_COLOR))
        color_row = Adw.ActionRow(title=t("Цвет", "Colour"))
        color_row.add_suffix(lighting_color)
        lighting_group.add(color_row)

        lighting_brightness = Adw.SpinRow.new_with_range(0, 100, 5)
        lighting_brightness.set_title(t("Яркость, %", "Brightness, %"))
        lighting_brightness.set_value(float(lighting.get("brightness") or 100))
        lighting_group.add(lighting_brightness)

        page.add(group)
        page.add(buttons_group)
        page.add(record_group)
        page.add(lighting_group)
        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        cancel = Gtk.Button(label=t("Отмена", "Cancel"))
        cancel.connect("clicked", lambda *_: dialog.close())
        save = Gtk.Button(label=t("Сохранить", "Save"))
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
                self._collect_dialog_lighting(
                    lighting_effect, lighting_color, lighting_brightness
                ),
            ),
        )
        footer.append(cancel)
        footer.append(save)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(toolbar)
        root.append(footer)
        dialog.set_content(root)
        dialog.present()

    def _collect_dialog_lighting(
        self,
        effect_row: Adw.ComboRow,
        color_button: Gtk.ColorDialogButton,
        brightness_row: Adw.SpinRow,
    ) -> dict[str, object] | None:
        effect = LIGHTING_EFFECT_CHOICES[int(effect_row.get_selected())]
        if effect == "disabled":
            return None
        return {
            "effect": effect,
            "color": _hex_from_rgba(color_button.get_rgba()),
            "brightness": int(brightness_row.get_value()),
        }

    def _save_profile(
        self,
        dialog: Adw.Window,
        profile_id: str,
        name: str,
        dpi: int,
        rate_ms: int,
        apps_text: str,
        buttons_text: str,
        lighting: dict[str, object] | None = None,
    ) -> None:
        profile_id = profile_id.strip()
        if not profile_id:
            self._toast(t("Нужен id профиля", "A profile id is required"))
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
            lighting=lighting,
        )

    def _profile_saved(self, dialog: Adw.Window, result: object, profile_id: str) -> None:
        dialog.close()
        replaced = bool(dict(result).get("replaced")) if isinstance(result, dict) else False
        self._toast(t(f"Профиль {profile_id} {'изменён' if replaced else 'добавлен'}",
                          f"Profile {profile_id} {'updated' if replaced else 'added'}"))
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
        self._toast(t(f"Профиль {profile_id} удалён", f"Profile {profile_id} deleted"))
        self._refresh()

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=2))
