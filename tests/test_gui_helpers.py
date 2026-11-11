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
