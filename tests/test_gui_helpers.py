"""Тесты чистых помощников GUI: разбор и рендер назначений кнопок.

`_parse_buttons_text` и `_buttons_to_text` — обратные друг другу: диалог
профиля показывает назначения строкой и разбирает её обратно. Если они
разойдутся, макрос, записанный для кнопки, потеряется при сохранении. Тесты
не требуют дисплея: используются только строковые функции.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - нет libadwaita
    pytest.skip("libadwaita недоступна", allow_module_level=True)

from mousecfg.gui.window import (  # noqa: E402
    _buttons_to_text,
    _describe_binding,
    _macro_id_for,
    _parse_buttons_text,
)


def test_parse_buttons_text_reads_macro_and_key():
    parsed = _parse_buttons_text("side1=macro:burst, side2=key:ctrl+c, middle=ignore")
    assert parsed == {
        "side1": {"macro": "burst"},
        "side2": {"key": "ctrl+c"},
        "middle": "ignore",
    }


def test_parse_buttons_text_rejects_broken_chunk():
    with pytest.raises(ValueError):
        _parse_buttons_text("side1")


def test_buttons_round_trip():
    original = {"side1": {"macro": "burst"}, "side2": "copy"}
    assert _parse_buttons_text(_buttons_to_text(original)) == original


def test_macro_id_is_filesystem_and_toml_safe():
    assert _macro_id_for("my profile!", "side1") == "my-profile-side1"
    assert _macro_id_for("default", "side2") == "default-side2"
    # Профиль без букв/цифр всё равно даёт осмысленный id.
    assert _macro_id_for("***", "side1") == "profile-side1"


def test_describe_binding_covers_all_shapes():
    assert _describe_binding({"macro": "b"}) == "макрос b"
    assert _describe_binding({"key": "ctrl+c"}) == "клавиши ctrl+c"
    assert _describe_binding("ignore") == "ignore"


def test_onboard_group_is_added_to_the_page() -> None:
    """Группа «Память мыши» должна быть на странице, а не только собрана.

    Регрессия: группу создавали, но забыли `page.add(...)`, поэтому в окне
    её не было видно.
    """
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from mousecfg.gui.window import MainWindow

    app = Adw.Application(application_id="io.github.mousecfg.TestOnboard")
    if not Gtk.init_check():
        pytest.skip("нет дисплея")
    window = MainWindow(app)

    assert window.onboard_group.get_visible() is False  # появляется только для vendor-мыши
    # Группа действительно в дереве страницы, а не «висит» отдельно.
    page = window.onboard_group.get_ancestor(Adw.PreferencesPage) or _find_page(window)
    assert page is not None
    assert window.onboard_group in [child for child in _all_groups(page)]


def _find_page(root):
    from gi.repository import Adw

    stack = [root]
    while stack:
        widget = stack.pop()
        if isinstance(widget, Adw.PreferencesPage):
            return widget
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return None


def _all_groups(page) -> list:
    groups = []
    stack = [page]
    while stack:
        widget = stack.pop()
        if type(widget).__name__ == "PreferencesGroup":
            groups.append(widget)
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return groups


def test_onboard_selection_survives_status_refresh() -> None:
    """Выбор уровня и правка DPI не сбрасываются фоновым обновлением.

    Регрессия: каждые 2 секунды приходил status/onboard-info и возвращал
    уровень и значение к прочитанным с мыши, поэтому записать уровень 1..5
    было нельзя — всё время возвращалось на активный (последний).
    """
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from mousecfg.gui.window import MainWindow

    app = Adw.Application(application_id="io.github.mousecfg.TestSel")
    if not Gtk.init_check():
        pytest.skip("нет дисплея")
    window = MainWindow(app)
    window.onboard_changes = {
        "dpi_levels": [400, 800, 1200, 1600, 2400, 2900],
        "dpi_index": 5,
    }
    window.onboard_editing = False
    window._sync_onboard_values(window.onboard_changes)  # noqa: SLF001

    # Пользователь выбирает уровень 1 и задаёт значение.
    window.onboard_level_row.set_selected(0)
    window.onboard_dpi_row.set_value(900.0)
    assert window.onboard_editing is True

    # Приходит фоновое обновление — оно не должно тронуть выбор.
    window._on_onboard_info(  # noqa: SLF001
        {"consent": True, "changes": {"dpi_levels": [400, 800, 1200, 1600, 2400, 2900], "dpi_index": 5}}
    )

    assert window.onboard_level_row.get_selected() == 0
    assert int(window.onboard_dpi_row.get_value()) == 900
    assert window.onboard_write_button.get_sensitive() is True


def test_onboard_inactive_level_write_is_allowed() -> None:
    """Можно писать в неактивный уровень: кнопка активна при отличии значения."""
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from mousecfg.gui.window import MainWindow

    app = Adw.Application(application_id="io.github.mousecfg.TestLevel")
    if not Gtk.init_check():
        pytest.skip("нет дисплея")
    window = MainWindow(app)
    window.onboard_changes = {"dpi_levels": [400, 800, 1200, 1600, 2400, 2900], "dpi_index": 5}
    window.onboard_editing = False
    window._sync_onboard_values(window.onboard_changes)  # noqa: SLF001

    window.onboard_level_row.set_selected(0)  # уровень 1, сейчас 400
    window.onboard_dpi_row.set_value(900.0)

    assert window.onboard_write_button.get_sensitive() is True


# -- список устройств по производителям (референс: две панели) -----------


def _device(usb_id, name, key, **extra):
    base = {"key": key, "usb_id": usb_id, "name": name, "protocol": "evdev"}
    return {**base, **extra}


def test_vendor_of_known_and_unknown():
    from mousecfg.gui.window import vendor_of

    assert vendor_of(_device("046d:c092", "G102 LIGHTSYNC", "serial:1")) == "Logitech"
    assert vendor_of(_device("a8a5:2255", "YJX-CHIP AJAZZ 2.4G", "serial:2")) == "AJAZZ"
    assert vendor_of(_device("a8a4:2255", "AJAZZ 2.4G", "serial:3")) == "AJAZZ"
    assert vendor_of(_device("1234:5678", "Some Mouse", "serial:4")) == "Другие устройства"
    # Неизвестный vid, но в имени есть бренд.
    assert vendor_of(_device("0000:0000", "Razer DeathAdder", "serial:5")) == "Razer"


def test_group_devices_orders_vendors_and_keeps_members():
    from mousecfg.gui.window import group_devices

    devices = [
        _device("a8a5:2255", "AJAZZ 2.4G", "serial:ajazz"),
        _device("046d:c092", "G102", "serial:logi"),
        _device("046d:c092", "G502", "serial:logi2"),
        _device("9999:0001", "No-name", "serial:other"),
    ]

    groups = group_devices(devices)
    names = [vendor for vendor, _ in groups]

    assert names[0] == "Logitech"          # известный бренд раньше «Других»
    assert "AJAZZ" in names
    assert names[-1] == "Другие устройства"  # неизвестные — в конце
    logitech = dict(groups)["Logitech"]
    assert [item["key"] for item in logitech] == ["serial:logi", "serial:logi2"]


def test_group_devices_empty():
    from mousecfg.gui.window import group_devices

    assert group_devices([]) == []


def test_device_subtitle_reports_protocol_battery_profile():
    from mousecfg.gui.window import device_subtitle

    line = device_subtitle(_device("a8a5:2255", "AJAZZ 2.4G", "serial:x", battery_percent=78, active_profile="default"))

    assert "a8a5:2255" in line
    assert "evdev" in line
    assert "78" in line
    assert "default" in line


def test_device_subtitle_without_battery():
    from mousecfg.gui.window import device_subtitle

    line = device_subtitle(_device("046d:c092", "G102", "serial:y", protocol="hidpp"))

    assert "HID++" in line
    assert "заряд" not in line
