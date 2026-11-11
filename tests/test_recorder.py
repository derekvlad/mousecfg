"""Тесты записи макроса и разбора шагов (без реальных устройств ввода)."""

from __future__ import annotations

import pytest
from evdev import ecodes

from mousecfg.input.macros import parse_macros
from mousecfg.input.recorder import MacroRecorder, _key_token, steps_to_macro_steps


class FakeEvent:
    def __init__(self, type_: int, code: int, value: int) -> None:
        self.type = type_
        self.code = code
        self.value = value


def _key(code: int, value: int) -> FakeEvent:
    return FakeEvent(ecodes.EV_KEY, code, value)


def _tap(recorder: MacroRecorder, *codes: int) -> None:
    for code in codes:
        recorder._feed(_key(code, 1))
    for code in reversed(codes):
        recorder._feed(_key(code, 0))


def test_combo_becomes_single_step():
    recorder = MacroRecorder()
    _tap(recorder, ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_T)
    assert [step["tap"] for step in recorder.steps] == ["ctrl+shift+T"]


def test_autorepeat_is_ignored():
    recorder = MacroRecorder()
    recorder._feed(_key(ecodes.KEY_A, 1))
    recorder._feed(_key(ecodes.KEY_A, 2))
    recorder._feed(_key(ecodes.KEY_A, 2))
    recorder._feed(_key(ecodes.KEY_A, 0))
    assert [step["tap"] for step in recorder.steps] == ["A"]


def test_named_and_function_keys():
    recorder = MacroRecorder()
    _tap(recorder, ecodes.KEY_F5)
    _tap(recorder, ecodes.KEY_LEFT)
    _tap(recorder, ecodes.KEY_ENTER)
    assert [step["tap"] for step in recorder.steps] == ["F5", "left", "enter"]


def test_mouse_button_is_recorded():
    recorder = MacroRecorder()
    _tap(recorder, ecodes.BTN_LEFT)
    assert [step["tap"] for step in recorder.steps] == ["left"]


def test_recorded_steps_are_executable_by_macro_parser():
    recorder = MacroRecorder()
    _tap(recorder, ecodes.KEY_LEFTCTRL, ecodes.KEY_C)
    _tap(recorder, ecodes.KEY_F5)
    steps = steps_to_macro_steps(recorder.steps)
    macros = parse_macros([{"id": "rec", "steps": steps}])
    assert macros["rec"].id == "rec"


def test_steps_to_macro_merges_pauses():
    steps = [
        {"tap": "a"},
        {"wait": True, "delay_ms": 100},
        {"wait": True, "delay_ms": 50},
    ]
    normalized = steps_to_macro_steps(steps)
    assert normalized == [{"tap": "a"}, {"wait": True, "delay_ms": 150}]


def test_steps_to_macro_drops_empty_wait():
    assert steps_to_macro_steps([{"wait": True, "delay_ms": 0}]) == []


def test_steps_to_macro_rejects_bad_key():
    from mousecfg.input.actions import ActionError

    with pytest.raises(ActionError):
        steps_to_macro_steps([{"tap": "несуществующая"}])  # type: ignore[list-item]


def test_by_id_lookup():
    """Ключи модификаторов и обычные коды дают понятные токены."""
    assert _key_token(ecodes.KEY_LEFTCTRL) == "ctrl"
    assert _key_token(ecodes.KEY_LEFTALT) == "alt"
    assert _key_token(ecodes.KEY_LEFTMETA) == "super"
    assert _key_token(ecodes.KEY_A) == "A" or _key_token(ecodes.KEY_A) == "KEY_A"
