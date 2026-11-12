"""Запись макроса: пользователь нажимает клавиши, мы превращаем их в шаги.

Модуль намеренно не знает про GTK: он читает evdev-события и складывает их
в список шагов. GUI лишь показывает кнопку «Записать» и передаёт recorder'у
выбранный event-узел. Так запись можно проверить тестом без графики.

Почему для записи нужен именно evdev, а не окно GTK: глобальную комбинацию
(например, Ctrl+Alt+T) оконный менеджер перехватит раньше, чем её увидит
окно. Чтение узла клавиатуры даёт «сырые» нажатия независимо от фокуса.
Но у узла может не быть ACL — тогда recorder честно сообщает об этом, а не
делает вид, что записал.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import evdev
from evdev import ecodes

from mousecfg.i18n import t

from .actions import code_to_token, parse_keys

logger = logging.getLogger(__name__)

# Модификаторы, которые не образуют «шаг» сами по себе, а приклеиваются
# к следующей клавише: Ctrl+Alt+T — один шаг, а не три.
MODIFIER_CODES: frozenset[int] = frozenset(
    {
        ecodes.KEY_LEFTCTRL,
        ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTALT,
        ecodes.KEY_RIGHTALT,
        ecodes.KEY_LEFTSHIFT,
        ecodes.KEY_RIGHTSHIFT,
        ecodes.KEY_LEFTMETA,
        ecodes.KEY_RIGHTMETA,
    }
)

# Кнопки мыши, которые тоже можно записать в макрос. Клик в макросе — это
# обычно BTN_LEFT/BTN_RIGHT; остальные кнопки мыши пользователь, как правило,
# и назначает, поэтому в записи оставляем только «интерфейсные» клики.
_IGNORED_MOUSE_BUTTONS = frozenset({ecodes.BTN_TOUCH, ecodes.BTN_TOOL_FINGER})

# Если между нажатиями прошло больше — вставляем явную паузу.
PAUSE_THRESHOLD_MS = 400
# Дольше этого срока запись идёт, только если пользователь продолжает жать.
MAX_RECORD_SECONDS = 60.0


class RecorderError(RuntimeError):
    pass


@dataclass
class RecordedEvent:
    """Одно записанное событие: клавиша или кнопка мыши, нажатие/отпускание."""

    code: int
    pressed: bool
    at: float


@dataclass
class MacroRecorder:
    """Слушает evdev-устройство и собирает шаги макроса.

    `on_event(code, pressed)` вызывается на каждое нажатие/отпускание (для
    живой подсветки в GUI). `on_step(step)` — каждый раз, когда шаг готов.
    Поток завершается по `stop()`; `record()` — блокирующий вариант для CLI.
    """

    devices: list[evdev.InputDevice] = field(default_factory=list)
    on_event: Callable[[int, bool], None] | None = None
    on_step: Callable[[dict], None] | None = None

    def __post_init__(self) -> None:
        self._steps: list[dict] = []
        self._held: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_press = 0.0
        self._lock = threading.Lock()
        self._error = ""

    # -- доступ к устройствам -------------------------------------------
    @classmethod
    def from_paths(cls, paths: Iterable[str], **kwargs: object) -> MacroRecorder:
        devices: list[evdev.InputDevice] = []
        errors: list[str] = []
        for path in paths:
            try:
                devices.append(evdev.InputDevice(path))
            except (PermissionError, OSError) as exc:
                errors.append(f"{path}: {exc}")
        if not devices:
            detail = "; ".join(errors) or t("не передано ни одного узла", "no device was given")
            raise RecorderError(
                t("нет доступа к узлам ввода для записи "
                f"({detail}). Нужно правило udev 70-mousecfg-input.rules или группа input.",
                "no access to input devices for recording "
                f"({detail}). The udev rule 70-mousecfg-input.rules or the input group is required.")
            )
        return cls(devices=devices, **kwargs)

    @property
    def steps(self) -> list[dict]:
        with self._lock:
            return list(self._steps)

    @property
    def error(self) -> str:
        return self._error

    # -- управление ------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="macro-recorder")
        self._thread.start()

    def stop(self) -> list[dict]:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        for device in self.devices:
            with contextlib.suppress(OSError):
                device.close()
        return self.steps

    def record(self, *, timeout: float | None = None) -> list[dict]:  # pragma: no cover - блокирующий CLI-режим
        self.start()
        deadline = time.monotonic() + (timeout or MAX_RECORD_SECONDS)
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.05)
        return self.stop()

    # -- внутренняя логика ----------------------------------------------
    def _loop(self) -> None:
        try:
            self._select_loop()
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            logger.warning(t("запись макроса прервана: %s", "macro recording interrupted: %s"), exc)

    def _select_loop(self) -> None:
        import select

        handles = {device.fd: device for device in self.devices}
        deadline = time.monotonic() + MAX_RECORD_SECONDS
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                ready, _, _ = select.select(list(handles), [], [], 0.2)
            except (OSError, ValueError):
                break
            for fd in ready:
                device = handles.get(fd)
                if device is None:
                    continue
                try:
                    for event in device.read():
                        self._feed(event)
                except OSError:
                    handles.pop(fd, None)

    def _feed(self, event: evdev.InputEvent) -> None:
        if event.type != ecodes.EV_KEY:
            return
        code = event.code
        if event.value == 2:  # автоповтор удерживаемой клавиши — не отдельный шаг
            return
        if code in _IGNORED_MOUSE_BUTTONS:
            return
        pressed = event.value == 1
        now = time.monotonic()
        if self.on_event is not None:
            self.on_event(code, pressed)
        if pressed:
            self._on_press(code, now)
        else:
            self._on_release(code)

    def _on_press(self, code: int, now: float) -> None:
        # Пауза перед новым нажатием, если между шагами была заметная тишина.
        with self._lock:
            has_steps = bool(self._steps) and not self._steps[-1].get("wait")
        if has_steps and self._last_press and now - self._last_press > PAUSE_THRESHOLD_MS / 1000.0:
            self._push({"wait": True, "delay_ms": int((now - self._last_press) * 1000)})
        self._last_press = now
        self._held.append(code)
        # Шаг собирается на нажатии «обычной» клавиши: к этому моменту в
        # `_held` уже лежат зажатые модификаторы. На отпускании не сбрасываем
        # повторно — иначе ctrl+shift+t дал бы лишний шаг «ctrl+shift».
        if code not in MODIFIER_CODES:
            self._flush()

    def _on_release(self, code: int) -> None:
        if code in self._held:
            self._held.remove(code)

    def _flush(self) -> None:
        """Превратить набор удерживаемых клавиш в один шаг."""
        if not self._held:
            return
        spec = self._combo_text(self._held)
        if spec:
            self._push({"tap": spec})

    def _push(self, step: dict) -> None:
        with self._lock:
            self._steps.append(step)
        if self.on_step is not None:
            self.on_step(step)

    @staticmethod
    def _combo_text(codes: Iterable[int]) -> str:
        """Собрать 'ctrl+shift+t' из кодов evdev (порядок: модификаторы, затем ключ)."""
        modifiers: list[str] = []
        keys: list[str] = []
        for code in codes:
            name = _key_token(code)
            if not name:
                continue
            (modifiers if code in MODIFIER_CODES else keys).append(name)
        return "+".join(modifiers + keys)


def _key_token(code: int) -> str:
    """Человекочитаемое имя клавиши, понятное parse_keys (ctrl, left, f4...)."""
    if code in MODIFIER_CODES:
        return {
            ecodes.KEY_LEFTCTRL: "ctrl",
            ecodes.KEY_RIGHTCTRL: "ctrl",
            ecodes.KEY_LEFTALT: "alt",
            ecodes.KEY_RIGHTALT: "alt",
            ecodes.KEY_LEFTSHIFT: "shift",
            ecodes.KEY_RIGHTSHIFT: "shift",
            ecodes.KEY_LEFTMETA: "super",
            ecodes.KEY_RIGHTMETA: "super",
        }[code]
    return code_to_token(code)


def steps_to_macro_steps(steps: list[dict]) -> list[dict]:
    """Нормализовать записанные шаги под схему macros.parse_macros.

    Запись даёт {'tap': '...'} и {'wait': True, 'delay_ms': N}; парсер макросов
    принимает ровно такой вид, поэтому функция нужна только для фильтрации
    пустых и «склеенных» пауз.
    """
    cleaned: list[dict] = []
    for step in steps:
        if step.get("wait"):
            delay = int(step.get("delay_ms", 0))
            if delay <= 0:
                continue
            if cleaned and cleaned[-1].get("wait"):
                cleaned[-1]["delay_ms"] += delay
                continue
            cleaned.append({"wait": True, "delay_ms": delay})
            continue
        spec = str(step.get("tap", "")).strip()
        if not spec:
            continue
        parse_keys(spec)  # проверяем, что шаг исполнится, ещё до сохранения
        cleaned.append({"tap": spec})
    return cleaned
