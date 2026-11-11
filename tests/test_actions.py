"""Тесты разбора действий и макросов: это чистые данные, устройства не нужно.

Эти проверки важны для требования «макросы — на хосте»: в TOML попадает
только описание, а исполняет его evdev/uinput на стороне хоста.
"""

from __future__ import annotations

import pytest
from evdev import ecodes

from mousecfg.input import macros as macros_mod
from mousecfg.input.actions import (
    ActionError,
    ActionKind,
    button_code,
    button_name,
    parse_action,
    parse_keys,
)

# -- клавиши --------------------------------------------------------------


def test_parse_keys_named_chord():
    codes = parse_keys("ctrl+shift+t")
    assert codes == (ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_T)


def test_parse_keys_uses_evdev_names():
    assert parse_keys("KEY_F13") == (ecodes.KEY_F13,)
    assert parse_keys("enter") == (ecodes.KEY_ENTER,)


def test_parse_keys_rejects_gibberish():
    with pytest.raises(ActionError):
        parse_keys("не-клавиша")


def test_key_alias_expands():
    assert parse_action("copy").keys == (ecodes.KEY_LEFTCTRL, ecodes.KEY_C)
    assert parse_action("browser-back").keys == (ecodes.KEY_LEFTALT, ecodes.KEY_LEFT)


# -- действия -------------------------------------------------------------


def test_passthrough_and_ignore():
    assert parse_action("passthrough").kind is ActionKind.PASSTHROUGH
    assert parse_action("ignore").kind is ActionKind.IGNORE


def test_macro_action_from_table():
    action = parse_action({"macro": "burst"})
    assert action.kind is ActionKind.MACRO
    assert action.macro == "burst"


def test_key_action_from_table():
    action = parse_action({"key": "ctrl+v"})
    assert action.kind is ActionKind.KEYS
    assert action.keys == (ecodes.KEY_LEFTCTRL, ecodes.KEY_V)


def test_action_describe_is_readable():
    assert parse_action("ctrl+c").describe() == "KEY_LEFTCTRL+KEY_C"
    assert parse_action("ignore").describe() == "ничего"
    assert parse_action({"macro": "burst"}).describe() == "макрос burst"


def test_unknown_action_table_is_rejected():
    with pytest.raises(ActionError):
        parse_action({"something": 1})
    with pytest.raises(ActionError):
        parse_action(42)


# -- кнопки ---------------------------------------------------------------


def test_button_aliases():
    assert button_code("back") == button_code("side1")
    assert button_code("forward") == button_code("side2")
    assert button_code("middle") == ecodes.BTN_MIDDLE


def test_button_name_roundtrip():
    assert button_name(button_code("side2")) == "side2"


def test_button_code_rejects_unknown():
    with pytest.raises(ActionError):
        button_code("телепатия")


# -- макросы --------------------------------------------------------------


def test_parse_macros_builds_steps():
    parsed = macros_mod.parse_macros(
        [
            {
                "id": "burst",
                "name": "Очередь",
                "interval_ms": 25,
                "steps": [
                    {"tap": "ctrl+c", "delay_ms": 50},
                    {"wait": True, "delay_ms": 120},
                ],
            }
        ]
    )
    macro = parsed["burst"]
    assert macro.interval_ms == 25
    assert macro.steps[0].keys == (ecodes.KEY_LEFTCTRL, ecodes.KEY_C)
    assert macro.steps[0].delay_ms == 50
    assert macro.steps[1].keys == ()
    assert macro.steps[1].delay_ms == 120


def test_macro_without_steps_is_rejected():
    with pytest.raises(ActionError):
        macros_mod.parse_macros([{"id": "empty", "steps": []}])


def test_macro_without_id_is_rejected():
    with pytest.raises(ActionError):
        macros_mod.parse_macros([{"steps": [{"tap": "a"}]}])


def test_macro_step_without_action_is_rejected():
    with pytest.raises(ActionError):
        macros_mod.parse_macros([{"id": "bad", "steps": [{"note": "ничего"}]}])


def test_macro_delay_limit_is_enforced():
    with pytest.raises(ActionError):
        macros_mod.parse_macros([{"id": "long", "steps": [{"wait": True, "delay_ms": 99999}]}])


def test_macro_describe_mentions_keys_and_delays():
    macro = macros_mod.parse_macros(
        [{"id": "m", "steps": [{"tap": "ctrl+c", "delay_ms": 10}]}]
    )["m"]
    text = macro.describe()
    assert "KEY_LEFTCTRL" in text
    assert "10" in text


# -- профиль целиком ------------------------------------------------------


def test_profile_with_button_and_macro_validates():
    from mousecfg import profiles

    text = """
version = 1
[[profile]]
id = "desktop"
dpi = 1600
[profile.buttons]
"side2" = { macro = "burst" }
[[macro]]
id = "burst"
steps = [ { tap = "ctrl+c" } ]
"""
    parsed = profiles.parse_profiles(text)
    assert parsed.macros_raw[0]["id"] == "burst"


def test_profile_referencing_missing_macro_is_rejected():
    from mousecfg import profiles

    text = """
version = 1
[[profile]]
id = "desktop"
dpi = 1600
[profile.buttons]
"side2" = { macro = "nope" }
"""
    with pytest.raises(profiles.ProfileError, match="nope"):
        profiles.parse_profiles(text)
