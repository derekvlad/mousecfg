"""Диалог записи макроса для GTK4.

Запись идёт нажатиями прямо в окне: на модальном диалоге висит
`EventControllerKey`, и пока он открыт, все нажатия достаются нам. Это не
требует ни udev-правил, ни группы input — в отличие от чтения evdev-узла
(см. input/recorder.py), который нужен для «сырой» записи сквозь хоткеи
оконного менеджера.

Клавиши распознаются по `keycode`, а не по `keyval`. `keyval` зависит от
раскладки и Shift: при русской раскладке `a` приходит как «ф», а `Shift+1` —
как `exclam`; на такой раскладке запись по keyval теряла почти все клавиши
и путала символы. GTK отдаёт `keycode` в X11-нумерации, то есть evdev-код
плюс 8 — он одинаков независимо от раскладки и Shift.

Модификаторы (Ctrl/Alt/Shift/Super) не становятся отдельными шагами: они
приклеиваются к следующей клавише, поэтому Ctrl+Alt+T — это один шаг.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Adw, Gtk

from mousecfg.i18n import t
from mousecfg.input.actions import ActionError, parse_keys
from mousecfg.input.recorder import MODIFIER_CODES, _key_token

logger = logging.getLogger(__name__)

# GTK (и Wayland, и X11) нумерует клавиши как X11: evdev-код + 8.
X11_KEYCODE_OFFSET = 8

# Модификаторы, которые в записи придерживаются к следующей клавише.
_MODIFIER_KEYCODES = frozenset(code + X11_KEYCODE_OFFSET for code in MODIFIER_CODES)


def keycode_to_token(keycode: int) -> str:
    """keycode GTK -> токен, который понимает parse_keys. '' — клавиша не годится."""
    code = keycode - X11_KEYCODE_OFFSET
    if code < 0:
        return ""
    token = _key_token(code)
    if not token:
        return ""
    try:
        # Токен должен реально исполняться: так отсеиваются клавиши без имени
        # (мультимедиа, «сырые» коды), которые parse_keys не знает.
        parse_keys(token)
    except ActionError:
        return ""
    return token


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
            title=t("Запись макроса", "Record macro"),
            default_width=460,
            default_height=560,
        )
        self._on_save = on_save
        self._steps: list[dict] = []
        self._held: list[int] = []
        self._recording = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=t("Запись макроса", "Record macro"), subtitle=t("нажатия ловятся в этом окне", "key presses are captured in this window")))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        meta = Adw.PreferencesGroup(title=t("Макрос", "Macro"))

        self.id_row = Adw.EntryRow(title=t("id (латиницей, без пробелов)", "id (latin letters, no spaces)"))
        self.id_row.set_text(macro_id)
        self.id_row.set_editable(not macro_id)
        meta.add(self.id_row)

        self.name_row = Adw.EntryRow(title=t("Название", "Name"))
        self.name_row.set_text(name)
        meta.add(self.name_row)

        self.interval_row = Adw.SpinRow.new_with_range(1, 500, 5)
        self.interval_row.set_title(t("Пауза между шагами, мс", "Delay between steps, ms"))
        self.interval_row.set_value(float(interval_ms))
        meta.add(self.interval_row)

        steps_group = Adw.PreferencesGroup(
            title=t("Шаги", "Steps"),
            description=t("Включите запись и нажмите нужные клавиши. Пробел в записи — отдельный шаг.", "Turn on recording and press the keys you need. Space is recorded as a separate step."),
        )
        self.record_button = Gtk.ToggleButton(label=t("Записать", "Record"))
        self.record_button.add_css_class("suggested-action")
        self.record_button.connect("toggled", self._on_record_toggled)
        clear_button = Gtk.Button(label=t("Очистить", "Clear"))
        clear_button.connect("clicked", lambda *_: self._clear())
        # libadwaita 1.x знает только set_header_suffix, поэтому обе кнопки
        # кладём в один контейнер (footer_suffix появился позже).
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header_box.append(clear_button)
        header_box.append(self.record_button)
        steps_group.set_header_suffix(header_box)

        self.steps_list = Gtk.ListBox()
        self.steps_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.steps_list.add_css_class("boxed-list")
        steps_group.add(self.steps_list)

        page.add(meta)
        page.add(steps_group)
        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        cancel = Gtk.Button(label=t("Отмена", "Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label=t("Сохранить", "Save"))
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
        self.record_button.set_label(t("Остановить", "Stop") if self._recording else t("Записать", "Record"))
        self._held.clear()
        if self._recording:
            self.grab_focus()

    def _on_key_pressed(self, _c: Gtk.EventControllerKey, _keyval: int, keycode: int, _state: int) -> bool:
        if not self._recording:
            return False
        if keycode in self._held:
            # Автоповтор удерживаемой клавиши — не отдельный шаг.
            return True
        self._held.append(keycode)
        if keycode in _MODIFIER_KEYCODES:
            return True
        spec = self._combo()
        if spec:
            self._steps.append({"tap": spec})
            self._render_steps()
        return True  # не даём акселераторам окна перехватить нажатие

    def _on_key_released(self, _c: Gtk.EventControllerKey, _keyval: int, keycode: int, _state: int) -> bool:
        if not self._recording:
            return False
        if keycode in self._held:
            self._held.remove(keycode)
        return True

    def _combo(self) -> str:
        modifiers = [keycode_to_token(k) for k in self._held if k in _MODIFIER_KEYCODES]
        keys = [keycode_to_token(k) for k in self._held if k not in _MODIFIER_KEYCODES]
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
            row.set_child(Gtk.Label(label=t("пока пусто — включите «Записать»", "empty for now — turn on “Record”"), xalign=0))
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
            self._toast(t("Нужен id макроса", "A macro id is required"))
            return
        if not self._steps:
            self._toast(t("Сначала запишите хотя бы одно нажатие", "Record at least one key press first"))
            return
        self._on_save(macro_id, int(self.interval_row.get_value()), list(self._steps))
        self.close()

    def _toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))
