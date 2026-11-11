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

from evdev import ecodes  # noqa: E402
from gi.repository import Adw, Gtk  # noqa: E402

from mousecfg.gui.recorder import MacroRecordingDialog, keycode_to_token  # noqa: E402

# GTK отдаёт keycode в X11-нумерации: evdev-код + 8.
X11_OFFSET = 8


def _keycode(name: str) -> int:
    return ecodes.ecodes[name] + X11_OFFSET


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


def test_keycode_to_token_ignores_layout():
    """Токен берётся из keycode: раскладка и Shift не важны."""
    # Однобуквенные — как в «сыром» evdev-рекордере (заглавная: Shift идёт
    # отдельным модификатором, а KEY_A всё равно даёт 'a').
    assert keycode_to_token(_keycode("KEY_A")) == "A"
    assert keycode_to_token(_keycode("KEY_1")) == "1"
    # Shift+1 приходит тем же keycode, что и «1», — токен остаётся '1',
    # а не '!'. Модификатор shift добавит отдельный шаг.
    assert keycode_to_token(_keycode("KEY_MINUS")) == "KEY_MINUS"
    assert keycode_to_token(_keycode("KEY_COMMA")) == "KEY_COMMA"
    assert keycode_to_token(_keycode("KEY_F4")) == "F4"
    assert keycode_to_token(_keycode("KEY_LEFTCTRL")) == "ctrl"
    assert keycode_to_token(_keycode("KEY_ENTER")) == "enter"
    # Клавиша без понятного parse_keys имени не должна попадать в шаг.
    assert keycode_to_token(0) == ""


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
            code = _keycode(name)
            dialog._controller.emit("key-pressed", 0, code, 0)
            dialog._controller.emit("key-released", 0, code, 0)

        tap("KEY_A")
        dialog._controller.emit("key-pressed", 0, _keycode("KEY_LEFTCTRL"), 0)
        dialog._controller.emit("key-pressed", 0, _keycode("KEY_LEFTSHIFT"), 0)
        tap("KEY_T")
        dialog._controller.emit("key-released", 0, _keycode("KEY_LEFTSHIFT"), 0)
        dialog._controller.emit("key-released", 0, _keycode("KEY_LEFTCTRL"), 0)
        tap("KEY_F4")
        # Shift+1: тот же keycode, что у '1'; должен дать shift+1, а не '!'.
        dialog._controller.emit("key-pressed", 0, _keycode("KEY_LEFTSHIFT"), 0)
        tap("KEY_1")
        dialog._controller.emit("key-released", 0, _keycode("KEY_LEFTSHIFT"), 0)

        dialog._save()

    _run_with_app(scenario)

    assert captured["id"] == "probe"
    assert captured["steps"] == [
        {"tap": "A"},
        {"tap": "ctrl+shift+T"},
        {"tap": "F4"},
        {"tap": "shift+1"},
    ]
