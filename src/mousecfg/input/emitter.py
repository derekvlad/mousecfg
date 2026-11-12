"""Отправка событий через uinput.

Виртуальное устройство повторяет возможности физической мыши и добавляет
клавиатуру — макросы и ремап эмулируются на хосте, в память мыши ничего
не пишется.
"""

from __future__ import annotations

import logging

import evdev
from evdev import ecodes

from mousecfg.i18n import t

logger = logging.getLogger(__name__)

VIRTUAL_NAME = "mousecfg virtual input"

# События, которые точно нужно пропускать насквозь. EV_KEY здесь обязателен:
# после EVIOCGRAB непривязанные кнопки (в том числе левая и правая) должны
# доходить до сессии как есть, иначе мышь «замерзает» при каждом захвате.
PASSTHROUGH_EVENT_TYPES = (ecodes.EV_REL, ecodes.EV_ABS, ecodes.EV_KEY, ecodes.EV_MSC, ecodes.EV_SYN, ecodes.EV_LED, ecodes.EV_REP)

KEY_CAPABILITIES: tuple[int, ...] = tuple(sorted({code for code in ecodes.KEY if 0 < code < ecodes.KEY_MAX}))
BUTTON_CAPABILITIES: tuple[int, ...] = tuple(sorted(code for code in ecodes.BTN))


class Emitter:
    """Обёртка над uinput.

    `create()` поднимает виртуальное устройство с кнопками и клавиатурой;
    `pass_event()` пропускает событие насквозь, `tap()`/`combo()` отправляют
    клавиши, `hold()`/`release()` управляют удержанием.
    """

    def __init__(self, device: evdev.InputDevice | None = None, *, dry_run: bool = False) -> None:
        self._source = device
        self._dry_run = dry_run
        self._ui: evdev.UInput | None = None

    # -- жизненный цикл -------------------------------------------------
    def create(self) -> None:
        if self._ui is not None or self._dry_run:
            return
        events = {ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL]}
        events[ecodes.EV_KEY] = list(BUTTON_CAPABILITIES) + list(KEY_CAPABILITIES)
        self._ui = evdev.UInput(events, name=VIRTUAL_NAME, version=1)
        logger.debug(t("виртуальное устройство %s создано", "virtual device %s created"), VIRTUAL_NAME)

    def close(self) -> None:
        if self._ui is not None:
            self._ui.close()
            self._ui = None

    def __enter__(self) -> Emitter:
        self.create()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- отправка -------------------------------------------------------
    def write(self, etype: int, code: int, value: int) -> None:
        if self._dry_run or self._ui is None:
            logger.info(t("[dry-run] событие %s %s %s", "[dry-run] event %s %s %s"), ecodes.EV.get(etype, etype), code, value)
            return
        self._ui.write(etype, code, value)
        self._ui.syn()

    def pass_event(self, event: evdev.InputEvent) -> None:
        """Пропустить исходное событие (движение, колесо) без изменений."""
        if self._dry_run or self._ui is None:
            return
        if event.type in PASSTHROUGH_EVENT_TYPES:
            self.write(event.type, event.code, event.value)

    def hold(self, code: int) -> None:
        self.write(ecodes.EV_KEY, code, 1)

    def release(self, code: int) -> None:
        self.write(ecodes.EV_KEY, code, 0)

    def tap(self, code: int, *, delay_ms: int = 12) -> None:
        self.hold(code)
        self._sleep(delay_ms)
        self.release(code)

    def combo(self, codes: tuple[int, ...] | list[int], *, delay_ms: int = 12) -> None:
        """Нажать комбинацию: модификаторы вниз, клавиша, всё обратно."""
        modifiers = [code for code in codes if code in MODIFIER_CODES]
        normal = [code for code in codes if code not in MODIFIER_CODES]
        for code in modifiers:
            self.hold(code)
        for code in normal:
            self.tap(code, delay_ms=delay_ms)
        for code in reversed(modifiers):
            self.release(code)

    @staticmethod
    def _sleep(milliseconds: int) -> None:
        import time

        if milliseconds > 0:
            time.sleep(milliseconds / 1000.0)


MODIFIER_CODES = frozenset(
    {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTMETA}
)
