#!/usr/bin/env python3
"""Сквозная самопроверка evdev-слоя без реальной мыши.

Источник и приёмник — uinput-устройства, поэтому физическая мышь не нужна.
Проверяется именно то, что не видно в dry-run:

* движение проходит насквозь при активном EVIOCGRAB;
* привязанные кнопки подменяются, непривязанные (левая/правая) — нет;
* макрос исполняется на хосте.

Запуск:

    PYTHONPATH=src python3 scripts/evdev-selftest.py

Требуются права на /dev/uinput и /dev/input/event*
(udev-правила data/60-mousecfg.rules и data/70-mousecfg-input.rules).
"""

from __future__ import annotations

import selectors
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import evdev  # noqa: E402
from evdev import ecodes  # noqa: E402

from mousecfg.input.actions import button_code, parse_action  # noqa: E402
from mousecfg.input.macros import parse_macros  # noqa: E402
from mousecfg.input.remapper import Remapper  # noqa: E402

SOURCE_NAME = "mousecfg-selftest-source"
VIRTUAL_NAME = "mousecfg virtual input"


def make_source() -> evdev.UInput:
    caps = {
        ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
        ecodes.EV_KEY: [
            ecodes.BTN_LEFT,
            ecodes.BTN_RIGHT,
            ecodes.BTN_MIDDLE,
            ecodes.BTN_SIDE,
            ecodes.BTN_EXTRA,
        ],
    }
    return evdev.UInput(caps, name=SOURCE_NAME, version=1)


def find_device_by_name(name: str, timeout: float = 3.0) -> evdev.InputDevice:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in evdev.list_devices():
            device = evdev.InputDevice(path)
            if device.name == name:
                return device
            device.close()
        time.sleep(0.05)
    raise RuntimeError(f"не найдено устройство {name!r} (проверьте права на /dev/input/event*)")


def collect(device: evdev.InputDevice, seconds: float) -> list[tuple[int, int, int]]:
    """Собрать события устройства за окно времени."""
    found: list[tuple[int, int, int]] = []
    with selectors.DefaultSelector() as selector:
        selector.register(device.fd, selectors.EVENT_READ)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not selector.select(timeout=end - time.monotonic()):
                break
            found.extend((event.type, event.code, event.value) for event in device.read())
    return found


def key_names(codes: set[int]) -> str:
    return ", ".join(sorted(evdev.ecodes.KEY.get(code, str(code)) for code in codes))


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    source = make_source()
    time.sleep(0.3)
    input_device = find_device_by_name(SOURCE_NAME)

    bindings = {
        button_code("side1"): parse_action("browser-back"),
        button_code("side2"): parse_action({"macro": "burst"}),
        button_code("middle"): parse_action("ctrl+c"),
    }
    macros = parse_macros(
        [{"id": "burst", "interval_ms": 5, "steps": [{"tap": "key_f13", "delay_ms": 10}]}]
    )
    remapper = Remapper(input_device, bindings, macros)
    thread = threading.Thread(target=remapper.run, daemon=True)
    thread.start()
    time.sleep(0.4)

    try:
        sink = find_device_by_name(VIRTUAL_NAME)
        time.sleep(0.3)

        source.write(ecodes.EV_REL, ecodes.REL_X, 7)
        source.syn()
        events = collect(sink, 0.5)
        checks.append(
            (
                "движение проходит насквозь",
                any(event[:2] == (ecodes.EV_REL, ecodes.REL_X) for event in events),
                str(events),
            )
        )

        source.write(ecodes.EV_KEY, ecodes.BTN_SIDE, 1)
        source.syn()
        source.write(ecodes.EV_KEY, ecodes.BTN_SIDE, 0)
        source.syn()
        codes = {event[1] for event in collect(sink, 0.6) if event[0] == ecodes.EV_KEY}
        checks.append(
            (
                "side1 -> KEY_LEFTALT+KEY_LEFT",
                {ecodes.KEY_LEFTALT, ecodes.KEY_LEFT} <= codes,
                key_names(codes),
            )
        )

        source.write(ecodes.EV_KEY, ecodes.BTN_MIDDLE, 1)
        source.syn()
        source.write(ecodes.EV_KEY, ecodes.BTN_MIDDLE, 0)
        source.syn()
        codes = {event[1] for event in collect(sink, 0.6) if event[0] == ecodes.EV_KEY}
        checks.append(
            (
                "middle -> KEY_LEFTCTRL+KEY_C",
                {ecodes.KEY_LEFTCTRL, ecodes.KEY_C} <= codes,
                key_names(codes),
            )
        )

        source.write(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1)
        source.syn()
        source.write(ecodes.EV_KEY, ecodes.BTN_EXTRA, 0)
        source.syn()
        codes = {event[1] for event in collect(sink, 1.2) if event[0] == ecodes.EV_KEY}
        checks.append(("side2 -> макрос KEY_F13", ecodes.KEY_F13 in codes, key_names(codes)))

        source.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1)
        source.syn()
        source.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0)
        source.syn()
        events = collect(sink, 0.5)
        right = ecodes.BTN_RIGHT
        checks.append(
            (
                "непривязанная BTN_RIGHT проходит как есть",
                any(event[:2] == (ecodes.EV_KEY, right) for event in events),
                key_names({event[1] for event in events if event[0] == ecodes.EV_KEY}),
            )
        )
        sink.close()
    finally:
        remapper.stop()
        thread.join(timeout=2)
        source.close()

    print("== Самопроверка evdev ==")
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
            print(f"        получено: {detail}")
    print()
    print(remapper.stats.summary())
    print()
    print("Ничего во встроенную память мыши не писалось: ремап полностью на хосте.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
