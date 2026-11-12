"""Ремап кнопок на evdev: перехват и подмена.

Важное ограничение evdev: grab работает на устройство целиком, поэтому
движение и колесо приходится ретранслировать через uinput. Мышь при этом
продолжает работать как обычно, а «перехваченной» становится только та
кнопка, которую пользователь явно изменил в профиле.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field

import evdev
from evdev import ecodes

from mousecfg.i18n import t

from .actions import Action, ActionKind, button_name
from .emitter import Emitter
from .macros import Macro, MacroRunner

logger = logging.getLogger(__name__)

# Паузы между событиями при «удержании» клавиши, чтобы приложение успело
# обработать нажатие. Значение подобрано эмпирически и настраивается.
AUTO_RELEASE_MS = 1200


@dataclass
class RemapStats:
    events: int = 0
    remapped: int = 0
    passed: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def note_latency(self, started: float) -> None:
        self.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        if len(self.latencies_ms) > 512:
            del self.latencies_ms[:256]

    def percentile(self, percent: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(len(ordered) * percent / 100.0))
        return ordered[index]

    def summary(self) -> str:
        if not self.latencies_ms:
            return t(f"событий {self.events}, перехвачено {self.remapped}, пропущено {self.passed}",
                     f"events {self.events}, intercepted {self.remapped}, passed {self.passed}")
        return (
            t(f"событий {self.events}, перехвачено {self.remapped}, пропущено {self.passed}, "
              f"задержка p50 {self.percentile(50):.2f} мс, p95 {self.percentile(95):.2f} мс",
              f"events {self.events}, intercepted {self.remapped}, passed {self.passed}, "
              f"latency p50 {self.percentile(50):.2f} ms, p95 {self.percentile(95):.2f} ms")
        )


class Remapper:
    """Граб одного event-узла и подмена нажатий согласно карте действий."""

    def __init__(
        self,
        device: evdev.InputDevice,
        bindings: dict[int, Action],
        macros: dict[str, Macro] | None = None,
        *,
        dry_run: bool = False,
        auto_release_ms: int = AUTO_RELEASE_MS,
    ) -> None:
        self.device = device
        self.bindings = bindings
        self.macros = macros or {}
        self.auto_release_ms = auto_release_ms
        self.stats = RemapStats()
        self.emitter = Emitter(dry_run=dry_run)
        self.macro_runner: MacroRunner | None = None
        self._stop = threading.Event()
        self._held: dict[int, int] = {}  # исходный код кнопки -> удерживаемый код
        self._release_timers: dict[int, threading.Timer] = {}
        self._missing_macros: set[str] = set()

    # -- жизненный цикл -------------------------------------------------
    def run(self) -> None:
        self.emitter.create()
        self.macro_runner = MacroRunner(self.emitter)
        self.device.grab()
        logger.info(t("перехват %s (%s) активен", "interception of %s (%s) is active"), self.device.path, self.device.name)
        try:
            self._loop()
        finally:
            self._release_all()
            with contextlib.suppress(OSError):
                self.device.ungrab()
            self.emitter.close()
            if self.macro_runner:
                self.macro_runner.join()

    def _loop(self) -> None:
        """Читать события через select, чтобы `stop()` срабатывал без событий.

        `read_loop()` блокируется до следующего события и не даёт проверить
        флаг остановки: на неподвижной мыши перехват так не выключить.
        """
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(self.device.fd, selectors.EVENT_READ)
            while not self._stop.is_set():
                if not selector.select(timeout=0.25):
                    continue
                for event in self.device.read():
                    if self._stop.is_set():
                        return
                    self._handle(event)

    def stop(self) -> None:
        self._stop.set()

    # -- обработка ------------------------------------------------------
    def _handle(self, event: evdev.InputEvent) -> None:
        self.stats.events += 1
        if event.type == ecodes.EV_KEY and event.code in self.bindings:
            self.stats.remapped += 1
            self._handle_bound(event)
            return

        if event.type == ecodes.EV_KEY:
            self.stats.passed += 1
        if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
            return
        self.emitter.pass_event(event)

    def _handle_bound(self, event: evdev.InputEvent) -> None:
        action = self.bindings[event.code]
        started = time.perf_counter()

        if event.value == 2:  # автоповтор
            return

        if action.kind is ActionKind.IGNORE:
            return

        if action.kind is ActionKind.PASSTHROUGH:
            self.emitter.pass_event(event)
            return

        if event.value == 1:  # нажатие
            logger.debug(t("кнопка %s -> %s", "button %s -> %s"), button_name(event.code), action.describe())
            self._press(event.code, action)
            self.stats.note_latency(started)
        else:
            self._release(event.code, action)
            self.stats.note_latency(started)

    def _press(self, code: int, action: Action) -> None:
        if action.kind is ActionKind.MACRO:
            macro = self.macros.get(action.macro or "")
            if macro is None:
                if action.macro not in self._missing_macros:
                    logger.warning(t("макрос %r не найден, действие по кнопке %s пропущено", "macro %r not found, action for button %s skipped"), action.macro, button_name(code))
                    self._missing_macros.add(str(action.macro))
                return

            if self.macro_runner and self.macro_runner.running == macro.id:
                # Повторное нажатие — прерываем макрос.
                self.macro_runner.stop(wait=False)
                return
            if self.macro_runner:
                self.macro_runner.run(macro)
            return

        # KEYS: удерживаем модификаторы, а обычные клавиши «тапаем».
        modifiers = [item for item in action.keys if item in _MODIFIER_CODES]
        normal = [item for item in action.keys if item not in _MODIFIER_CODES]

        if modifiers:
            for modifier in modifiers:
                self.emitter.hold(modifier)
            self._held[code] = modifiers[0]
            self._arm_release(code, modifiers)

        if normal:
            first = normal[0]
            self.emitter.hold(first)
            self._held.setdefault(code, first)
            self._arm_release(code, normal)
        elif not modifiers:
            logger.warning(t("действие кнопки %s пустое", "the action for button %s is empty"), button_name(code))

    def _release(self, code: int, action: Action) -> None:
        timer = self._release_timers.pop(code, None)
        if timer is not None:
            timer.cancel()

        if action.kind is ActionKind.MACRO:
            return

        held = self._held.pop(code, None)
        if held is not None:
            self.emitter.release(held)
        for modifier in [item for item in action.keys if item in _MODIFIER_CODES]:
            self.emitter.release(modifier)

    def _arm_release(self, code: int, codes: list[int]) -> None:
        """Самопроизвольное отпускание: страховка от «залипшей» клавиши."""
        timer = threading.Timer(self.auto_release_ms / 1000.0, self._auto_release, args=(code, codes))
        timer.daemon = True
        self._release_timers[code] = timer
        timer.start()

    def _auto_release(self, code: int, codes: list[int]) -> None:
        logger.debug(t("автоотпускание после %d мс: %s", "auto-release after %d ms: %s"), self.auto_release_ms, [button_name(item) for item in codes])
        held = self._held.pop(code, None)
        if held is not None:
            self.emitter.release(held)
        for item in codes:
            if item in _MODIFIER_CODES:
                self.emitter.release(item)

    def _release_all(self) -> None:
        for timer in self._release_timers.values():
            timer.cancel()
        self._release_timers.clear()
        for held in set(self._held.values()):
            self.emitter.release(held)
        self._held.clear()


_MODIFIER_CODES = frozenset(
    {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTMETA}
)
