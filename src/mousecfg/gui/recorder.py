"""Диалог записи макроса для GTK4.

Запись идёт нажатиями прямо в окне: на модальном диалоге висит
`EventControllerKey`, и пока он открыт, все нажатия достаются нам. Это не
требует ни udev-правил, ни группы input — в отличие от чтения evdev-узла
(см. input/recorder.py), который нужен для «сырой» записи сквозь хоткеи
оконного менеджера.

Модификаторы (Ctrl/Alt/Shift/Super) не становятся отдельными шагами: они
приклеиваются к следующей клавише, поэтому Ctrl+Alt+T — это один шаг.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Adw, Gdk, Gtk

logger = logging.getLogger(__name__)

# Клавиши-модификаторы по keyval -> токен, понятный parse_keys.
_MODIFIER_TOKENS: dict[int, str] = {
    Gdk.KEY_Control_L: "ctrl",
    Gdk.KEY_Control_R: "ctrl",
    Gdk.KEY_Alt_L: "alt",
    Gdk.KEY_Alt_R: "alt",
    Gdk.KEY_Shift_L: "shift",
    Gdk.KEY_Shift_R: "shift",
    Gdk.KEY_Super_L: "super",
    Gdk.KEY_Super_R: "super",
    Gdk.KEY_Meta_L: "super",
    Gdk.KEY_Meta_R: "super",
}

# Именованные клавиши: keyval -> токен (в нижнем регистре, как в actions._resolve_key).
_NAMED_TOKENS: dict[int, str] = {
    Gdk.KEY_Return: "enter",
    Gdk.KEY_KP_Enter: "enter",
    Gdk.KEY_Escape: "escape",
    Gdk.KEY_BackSpace: "backspace",
    Gdk.KEY_Tab: "tab",
    Gdk.KEY_Delete: "delete",
    Gdk.KEY_Insert: "insert",
    Gdk.KEY_Home: "home",
    Gdk.KEY_End: "end",
    Gdk.KEY_Page_Up: "pgup",
    Gdk.KEY_Page_Down: "pgdn",
    Gdk.KEY_Up: "up",
    Gdk.KEY_Down: "down",
    Gdk.KEY_Left: "left",
    Gdk.KEY_Right: "right",
    Gdk.KEY_space: "space",
    Gdk.KEY_Print: "print",
    Gdk.KEY_Scroll_Lock: "scrolllock",
    Gdk.KEY_Pause: "pause",
}

# Модификаторы, которые в записи придерживаются к следующей клавише.
_MODIFIER_KEYVALS = frozenset(_MODIFIER_TOKENS)


def keyval_to_token(keyval: int) -> str:
    """keyval GTK -> токен, который понимает parse_keys. '' — клавиша не годится."""
    if keyval in _MODIFIER_TOKENS:
        return _MODIFIER_TOKENS[keyval]
    if keyval in _NAMED_TOKENS:
        return _NAMED_TOKENS[keyval]
    # F1..F35.
    if Gdk.KEY_F1 <= keyval <= Gdk.KEY_F35:
        return f"F{keyval - Gdk.KEY_F1 + 1}"
    name = Gdk.keyval_name(keyval)
    if name and len(name) == 1 and name.isprintable():
        return name
    # Мультимедиа-клавиши GTK зовёт XF86AudioRaiseVolume и т. п.; в этот список
    # они не входят намеренно: parse_keys их не знает и запись бы не исполнилась.
    return ""


class MacroRecordingDialog(Adw.Window):
    """Модальное окно записи: пользователь жмёт клавиши, они копятся в списке."""

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        on_save: Callable[[str, int, list[dict]], None],
        macro_id: str = "",
        name: str = "",
        interval_ms: int = 40,
    ) -> None:
        super().__init__(
            transient_for=parent,
            modal=True,
            title="Запись макроса",
            default_width=460,
            default_height=560,
        )
        self._on_save = on_save
        self._steps: list[dict] = []
        self._held: list[int] = []
        self._recording = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Запись макроса", subtitle="нажатия ловятся в этом окне"))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        meta = Adw.PreferencesGroup(title="Макрос")

        self.id_row = Adw.EntryRow(title="id (латиницей, без пробелов)")
        self.id_row.set_text(macro_id)
        self.id_row.set_editable(not macro_id)
        meta.add(self.id_row)

        self.name_row = Adw.EntryRow(title="Название")
        self.name_row.set_text(name)
        meta.add(self.name_row)

        self.interval_row = Adw.SpinRow.new_with_range(1, 500, 5)
        self.interval_row.set_title("Пауза между шагами, мс")
        self.interval_row.set_value(float(interval_ms))
        meta.add(self.interval_row)

        steps_group = Adw.PreferencesGroup(
            title="Шаги",
            description="Включите запись и нажмите нужные клавиши. Пробел в записи — отдельный шаг.",
        )
        self.record_button = Gtk.ToggleButton(label="Записать")
        self.record_button.add_css_class("suggested-action")
        self.record_button.connect("toggled", self._on_record_toggled)
        steps_group.set_header_suffix(self.record_button)

        self.steps_list = Gtk.ListBox()
        self.steps_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.steps_list.add_css_class("boxed-list")
        steps_group.add(self.steps_list)

        clear_button = Gtk.Button(label="Очистить")
        clear_button.connect("clicked", lambda *_: self._clear())
        steps_group.set_footer_suffix(clear_button)

        page.add(meta)
        page.add(steps_group)
        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Сохранить")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        footer.append(cancel)
        footer.append(save)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(toolbar)
        root.append(footer)
        self._toast_overlay = Adw.ToastOverlay(child=root)
        self.set_content(self._toast_overlay)

        self._controller = Gtk.EventControllerKey()
        self._controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._controller.connect("key-pressed", self._on_key_pressed)
        self._controller.connect("key-released", self._on_key_released)
        self.add_controller(self._controller)

        self._render_steps()

    # -- запись ----------------------------------------------------------
    def _on_record_toggled(self, button: Gtk.ToggleButton) -> None:
        self._recording = button.get_active()
        self.record_button.set_label("Остановить" if self._recording else "Записать")
        self._held.clear()
        if self._recording:
            self.grab_focus()

    def _on_key_pressed(self, _c: Gtk.EventControllerKey, keyval: int, _code: int, _state: int) -> bool:
        if not self._recording:
            return False
        if keyval in _MODIFIER_KEYVALS:
            if keyval not in self._held:
                self._held.append(keyval)
            return True
        if keyval not in self._held:
            self._held.append(keyval)
        spec = self._combo()
        if spec:
            self._steps.append({"tap": spec})
            self._render_steps()
        return True  # не даём акселераторам окна перехватить нажатие

    def _on_key_released(self, _c: Gtk.EventControllerKey, keyval: int, _code: int, _state: int) -> bool:
        if not self._recording:
            return False
        if keyval in self._held:
            self._held.remove(keyval)
        return True

    def _combo(self) -> str:
        modifiers = [keyval_to_token(k) for k in self._held if k in _MODIFIER_KEYVALS]
        keys = [keyval_to_token(k) for k in self._held if k not in _MODIFIER_KEYVALS]
        tokens = [tok for tok in modifiers + keys if tok]
        return "+".join(tokens)

    def _clear(self) -> None:
        self._steps.clear()
        self._held.clear()
        self._render_steps()

    def _render_steps(self) -> None:
        for child in list(self.steps_list):
            self.steps_list.remove(child)
        if not self._steps:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(Gtk.Label(label="пока пусто — включите «Записать»", xalign=0))
            row.add_css_class("dim-label")
            self.steps_list.append(row)
            return
        for index, step in enumerate(self._steps, start=1):
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.append(Gtk.Label(label=f"{index}.", width_chars=3, xalign=0))
            box.append(Gtk.Label(label=str(step.get("tap", step)), xalign=0))
            row.set_child(box)
            self.steps_list.append(row)

    # -- сохранение ------------------------------------------------------
    def _save(self) -> None:
        macro_id = self.id_row.get_text().strip()
        if not macro_id:
            self._toast("Нужен id макроса")
            return
        if not self._steps:
            self._toast("Сначала запишите хотя бы одно нажатие")
            return
        self._on_save(macro_id, int(self.interval_row.get_value()), list(self._steps))
        self.close()

    def _toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))
