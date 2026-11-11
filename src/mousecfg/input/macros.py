"""Макросы: последовательность шагов, исполняемая на хосте.

Макрос — это данные (TOML) плюс интерпретатор. Мышь о них не знает:
нажатия перехватываются на evdev, а события отправляются через uinput.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from evdev import ecodes

from .actions import ActionError, _resolve_key
from .emitter import Emitter

logger = logging.getLogger(__name__)

MAX_STEPS = 64
MAX_TOTAL_DELAY_MS = 10_000


@dataclass
class Step:
    keys: tuple[int, ...] = ()
    delay_ms: int = 0
    description: str = ""

    def describe(self) -> str:
        if self.description:
            return self.description
        names = [ecodes.KEY.get(code, str(code)) for code in self.keys]
        text = "->".join(names) if names else "(пауза)"
        return f"{text} ({self.delay_ms} мс)" if self.delay_ms else text


@dataclass
class Macro:
    id: str
    name: str = ""
    steps: list[Step] = field(default_factory=list)
    interval_ms: int = 40

    def describe(self) -> str:
        return " ; ".join(step.describe() for step in self.steps)


def _parse_step(raw: Any) -> Step:
    if not isinstance(raw, dict):
        raise ActionError(f"шаг макроса должен быть таблицей, получено {type(raw).__name__}")

    delay = int(raw.get("delay_ms", raw.get("delay", 0)) or 0)
    if delay < 0 or delay > MAX_TOTAL_DELAY_MS:
        raise ActionError(f"задержка {delay} мс вне допустимого диапазона")

    if "key" in raw:
        codes = tuple(_resolve_key(token) for token in str(raw["key"]).replace("+", " ").split() if token)
        return Step(keys=codes, delay_ms=delay, description=str(raw.get("note", "")))

    if "tap" in raw:
        codes = tuple(_resolve_key(token) for token in str(raw["tap"]).replace("+", " ").split() if token)
        return Step(keys=codes, delay_ms=delay, description=str(raw.get("note", "")))

    if "wait" in raw or "delay" in raw:
        return Step(keys=(), delay_ms=delay, description=str(raw.get("note", "")) or f"пауза {delay} мс")

    raise ActionError(f"в шаге макроса нет 'key', 'tap' или 'wait': {raw!r}")


def parse_macros(raw_list: list[dict]) -> dict[str, Macro]:
    macros: dict[str, Macro] = {}
    for raw in raw_list:
        if "id" not in raw:
            raise ActionError(f"у макроса нет id: {raw!r}")
        steps = [_parse_step(item) for item in raw.get("steps", [])]
        if not steps:
            raise ActionError(f"макрос {raw['id']!r} без шагов")
        if len(steps) > MAX_STEPS:
            raise ActionError(f"макрос {raw['id']!r}: больше {MAX_STEPS} шагов не поддерживается")
        macro = Macro(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            steps=steps,
            interval_ms=int(raw.get("interval_ms", 40)),
        )
        macros[macro.id] = macro
    return macros


class MacroRunner:
    """Исполняет макрос в отдельном потоке.

    Повторное нажатие во время выполнения останавливает текущий макрос
    (поведение игровых макросов: «нажал ещё раз — прервалось»), а не
    выстраивает очередь.
    """

    def __init__(self, emitter: Emitter) -> None:
        self.emitter = emitter
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._current: str | None = None

    @property
    def running(self) -> str | None:
        return self._current

    def run(self, macro: Macro) -> None:
        self.stop(wait=True)
        with self._lock:
            self._stop.clear()
            self._current = macro.id
            self._thread = threading.Thread(target=self._execute, args=(macro,), daemon=True, name=f"macro-{macro.id}")
            self._thread.start()

    def stop(self, *, wait: bool = False) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._stop.set()
        if wait:
            thread.join(timeout=2.0)

    def join(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _execute(self, macro: Macro) -> None:
        logger.debug("макрос %s: %s", macro.id, macro.describe())
        try:
            for step in macro.steps:
                if self._stop.is_set():
                    return
                if step.keys:
                    self.emitter.combo(list(step.keys), delay_ms=macro.interval_ms)
                if step.delay_ms:
                    # Пауза должна прерываться, поэтому спим короткими отрезками.
                    deadline = time.monotonic() + step.delay_ms / 1000.0
                    while time.monotonic() < deadline:
                        if self._stop.is_set():
                            return
                        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        finally:
            with self._lock:
                self._current = None
