"""Тесты ремапа кнопок: логика без реального устройства.

Устройство подменяется объектом с минимальным интерфейсом, эмиттер
работает в dry-run. Проверяется главное: ремап ничего не знает о мыши
и не пытается что-либо в неё записать.
"""

from __future__ import annotations

import time

from evdev import ecodes

from mousecfg.input.actions import parse_action
from mousecfg.input.macros import MacroRunner, parse_macros
from mousecfg.input.remapper import Remapper


class FakeEvent:
    def __init__(self, etype: int, code: int, value: int) -> None:
        self.type = etype
        self.code = code
        self.value = value


class FakeDevice:
    """Минимум, который трогает Remapper (кроме grab/read_loop)."""

    path = "/dev/input/event-fake"
    name = "Fake Mouse"

    def __init__(self) -> None:
        self.grabbed = False
        self.ungrabbed = False

    def grab(self) -> None:
        self.grabbed = True

    def ungrab(self) -> None:
        self.ungrabbed = True


def make_remapper(bindings: dict[str, object], macros: list[dict] | None = None) -> Remapper:
    parsed_bindings = {}
    for name, spec in bindings.items():
        from mousecfg.input.actions import button_code

        parsed_bindings[button_code(name)] = parse_action(spec)

    parsed_macros = parse_macros(macros or [])
    remapper = Remapper(FakeDevice(), parsed_bindings, parsed_macros, dry_run=True)
    remapper.emitter.create()  # в dry-run ничего не создаёт
    remapper.macro_runner = MacroRunner(remapper.emitter)
    return remapper


def test_passthrough_keeps_button():
    remapper = make_remapper({"side1": "passthrough"})
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 1))
    assert remapper.stats.remapped == 1
    assert remapper.emitter._ui is None  # dry-run не создаёт uinput


def test_ignore_swallows_button():
    remapper = make_remapper({"side1": "ignore"})
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 1))
    assert remapper._held == {}


def test_keys_action_tracks_held_keys():
    remapper = make_remapper({"side2": "ctrl+c"})
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1))
    # Удерживается модификатор (или первая обычная клавиша) — главное, что есть.
    assert remapper._held[ecodes.BTN_EXTRA] in (ecodes.KEY_LEFTCTRL, ecodes.KEY_C)
    assert ecodes.BTN_EXTRA in remapper._release_timers

    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_EXTRA, 0))
    assert ecodes.BTN_EXTRA not in remapper._held
    assert ecodes.BTN_EXTRA not in remapper._release_timers


def test_macro_button_runs_macro_and_repeat_stops_it():
    remapper = make_remapper(
        {"middle": {"macro": "burst"}},
        [
            {
                "id": "burst",
                "interval_ms": 5,
                "steps": [{"tap": "ctrl+c", "delay_ms": 2000}],
            }
        ],
    )
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_MIDDLE, 1))
    runner = remapper.macro_runner
    assert runner is not None
    time.sleep(0.05)
    assert runner.running == "burst"

    # Повторное нажатие прерывает макрос, а не ставит в очередь.
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_MIDDLE, 1))
    time.sleep(0.05)
    assert runner.running is None


def test_missing_macro_is_reported_once(caplog):
    remapper = make_remapper({"middle": {"macro": "nope"}})
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_MIDDLE, 1))
    assert "nope" in remapper._missing_macros


def test_unbound_keys_are_passed_through():
    remapper = make_remapper({"side1": "browser-back"})
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
    assert remapper.stats.passed == 1
    assert remapper.stats.remapped == 0


class RecordingUInput:
    """Подменяет uinput: пишет события в список, чтобы их можно было проверить."""

    def __init__(self) -> None:
        self.written: list[tuple[int, int, int]] = []

    def write(self, etype: int, code: int, value: int) -> None:
        self.written.append((etype, code, value))

    def syn(self) -> None:
        pass


def test_emitter_passes_original_button_through():
    """После EVIOCGRAB непривязанные кнопки обязаны доходить до сессии.

    Если EV_KEY не попадёт в набор «насквозь», мышь замерзает: левая и правая
    кнопки теряются сразу после захвата устройства.
    """
    from mousecfg.input.emitter import Emitter

    emitter = Emitter()
    ui = RecordingUInput()
    emitter._ui = ui  # type: ignore[assignment]

    emitter.pass_event(FakeEvent(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
    emitter.pass_event(FakeEvent(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
    emitter.pass_event(FakeEvent(ecodes.EV_REL, ecodes.REL_X, 5))

    assert (ecodes.EV_KEY, ecodes.BTN_LEFT, 1) in ui.written
    assert (ecodes.EV_KEY, ecodes.BTN_LEFT, 0) in ui.written
    assert (ecodes.EV_REL, ecodes.REL_X, 5) in ui.written


def test_auto_release_clears_held_key():
    remapper = make_remapper({"side2": "ctrl+c"})
    remapper.auto_release_ms = 20
    remapper._handle(FakeEvent(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1))
    assert ecodes.BTN_EXTRA in remapper._held
    time.sleep(0.15)
    assert ecodes.BTN_EXTRA not in remapper._held


def test_stats_percentiles():
    remapper = make_remapper({})
    for value in (1.0, 2.0, 3.0, 4.0):
        remapper.stats.latencies_ms.append(value)
    assert remapper.stats.percentile(50) == 3.0
    assert "p95" in remapper.stats.summary()
