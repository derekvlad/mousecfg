"""Тесты диалога записи макроса.

Регрессия: конструктор `MacroRecordingDialog` падал на
`Adw.PreferencesGroup.set_footer_suffix` (в libadwaita 1.x его нет), из-за
чего кнопка «Записать макрос» вообще не открывала окно. Здесь мы собираем
диалог вживую и проверяем, что нажатия клавиш превращаются в шаги.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("gi")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - нет GTK/libadwaita
    pytest.skip("GTK4/libadwaita недоступны", allow_module_level=True)

if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):  # pragma: no cover
    pytest.skip("нет графического дисплея", allow_module_level=True)

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from mousecfg.gui.recorder import MacroRecordingDialog  # noqa: E402


def _run_with_app(callback) -> None:  # noqa: ANN001
    """Выполнить callback(app) внутри активированного Adw.Application."""
    app = Adw.Application(application_id="io.github.mousecfg.RecorderTest")
    errors: list[BaseException] = []

    def on_activate(_app: Adw.Application) -> None:
        try:
            callback(app)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            app.quit()

    app.connect("activate", on_activate)
    app.run([])
    if errors:
        raise errors[0]


def test_dialog_builds_and_records_keys():
    captured: dict[str, object] = {}

    def scenario(app: Adw.Application) -> None:
        parent = Gtk.Window()
        dialog = MacroRecordingDialog(
            parent,
            macro_id="probe",
            on_save=lambda mid, _interval, steps: captured.update(id=mid, steps=steps),
        )
        dialog.record_button.set_active(True)

        def tap(name: str) -> None:
            keyval = Gdk.keyval_from_name(name)
            dialog._controller.emit("key-pressed", keyval, 0, 0)
            dialog._controller.emit("key-released", keyval, 0, 0)

        tap("a")
        dialog._controller.emit("key-pressed", Gdk.keyval_from_name("Control_L"), 0, 0)
        dialog._controller.emit("key-pressed", Gdk.keyval_from_name("Shift_L"), 0, 0)
        tap("t")
        dialog._controller.emit("key-released", Gdk.keyval_from_name("Shift_L"), 0, 0)
        dialog._controller.emit("key-released", Gdk.keyval_from_name("Control_L"), 0, 0)
        tap("F4")

        dialog._save()

    _run_with_app(scenario)

    assert captured["id"] == "probe"
    assert captured["steps"] == [{"tap": "a"}, {"tap": "ctrl+shift+t"}, {"tap": "F4"}]
