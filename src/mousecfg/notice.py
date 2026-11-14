"""Предупреждение о низком заряде мыши.

Демон и так опрашивает заряд раз в 30 секунд, но молчит: у мышей без HID++
прошивка отдаёт значение грубо и подолгу держит 98–100%, поэтому момент,
когда мышь вот-вот сядет, легко пропустить. Здесь — решение «пора
предупредить» (чистая функция, проверяется тестами) и само уведомление
рабочего стола.

Уведомление отправляется один раз на разрядку: после предупреждения
следующее не отправится, пока заряд снова не поднимется выше порога
восстановления (гистерезис) или мышь не поставят на зарядку.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from .i18n import t

logger = logging.getLogger(__name__)

# Порог предупреждения совпадает с порогом «разряжено» в `power.py`
# (значок `battery-caution-symbolic`), чтобы число и значок не спорили.
LOW_THRESHOLD = 15
# Выше этого значения считаем, что заряд восстановлен, и снова вооружаемся.
REARM_THRESHOLD = 25


class LowBatteryMonitor:
    """Состояние предупреждений для одной мыши.

    ``update`` вызывает демон после каждого опроса заряда; он возвращает
    текст уведомления, только когда предупредить нужно именно сейчас.
    """

    def __init__(self, threshold: int = LOW_THRESHOLD, rearm: int = REARM_THRESHOLD) -> None:
        self.threshold = threshold
        self.rearm = rearm
        self._warned = False

    def update(self, percent: int | None, charging: bool | None) -> tuple[str, str] | None:
        """Решение «предупредить сейчас» и тексты уведомления.

        None — предупреждать не нужно (заряда нет данных, ещё высоко, мышь
        на зарядке или уже предупреждали). Иначе — ``(заголовок, текст)``.
        """
        if percent is None or not 0 <= percent <= 100:
            return None
        # Мышь на кабеле: заряд растёт, предупреждать не о чем. Заодно
        # взводим счётчик заново — после снятия с зарядки цикл начинается снова.
        if charging:
            self._warned = False
            return None
        if percent > self.rearm:
            self._warned = False
            return None
        if percent > self.threshold or self._warned:
            return None
        self._warned = True
        return (
            t("Мышь разряжена", "Mouse battery low"),
            t(
                f"Осталось около {percent}%. Подключите кабель, иначе мышь скоро отключится.",
                f"About {percent}% left. Connect the cable or the mouse will soon shut off.",
            ),
        )


def notify(title: str, body: str, *, urgency: str = "normal") -> bool:
    """Уведомление рабочего стола через `notify-send`.

    Мягкая зависимость: если `notify-send` нет (нет графической сессии),
    просто возвращаем False. Уведомление никогда не должно ронять демон.
    """
    command = shutil.which("notify-send")
    if command is None:
        logger.debug(t("notify-send не найден: уведомление пропущено", "notify-send not found: notification skipped"))
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [command, "--app-name=mousecfg", f"--urgency={urgency}", title, body],
            check=False,
            timeout=5.0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(t("уведомление не отправилось: %s", "notification failed: %s"), exc)
        return False
    return result.returncode == 0


__all__ = ["LOW_THRESHOLD", "REARM_THRESHOLD", "LowBatteryMonitor", "notify"]
