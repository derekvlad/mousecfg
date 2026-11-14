"""Отображение заряда мыши: подпись и значок.

Вынесено из трея, чтобы значок и пороги проверялись без GTK и не зависели от
того, загружен ли GTK3 или GTK4.
"""

from __future__ import annotations

from mousecfg.i18n import t

# Пороги совпадают с Adwaita/GNOME: значок выглядит привычно.
FULL_THRESHOLD = 80
GOOD_THRESHOLD = 40
LOW_THRESHOLD = 15


def battery_display(percent: object) -> tuple[str, str] | None:
    """Подпись и имя значка заряда. None — показывать нечего."""
    if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
        return None
    if percent > FULL_THRESHOLD:
        icon = "battery-full-symbolic"
    elif percent > GOOD_THRESHOLD:
        icon = "battery-good-symbolic"
    elif percent > LOW_THRESHOLD:
        icon = "battery-low-symbolic"
    else:
        icon = "battery-caution-symbolic"
    return t(f"заряд: {percent}%", f"battery: {percent}%"), icon


def battery_note(percent: object) -> str | None:
    """Пояснение к числу: прошивка отдаёт заряд грубо.

    У мышей без HID++ (AJAZZ 2.4G) байт заряда меняется шагом ~2% и может
    подолгу стоять на 98–100% даже при активной работе от батареи. Значение
    честное (то же отдаёт штатный веб-инструмент), но огрублённое, поэтому
    рядом уместна оговорка, а не «точность до процента».
    """
    if battery_display(percent) is None:
        return None
    return t(
        f"прошивка сообщает {percent}%, точность ограничена",
        f"firmware reports {percent}%, precision is limited",
    )


def hover_title(
    name: str = "", profile: str = "", percent: object = None, note: str = ""
) -> str:
    """Текст всплывающей подсказки значка трея.

    Панели (в частности noctalia) показывают `Title` элемента
    StatusNotifierItem, поэтому заряд кладём именно туда. Пустые куски
    пропускаем, чтобы не было двойных разделителей.
    """
    info = battery_display(percent)
    battery = info[0] if info is not None else t("заряд: —", "battery: —")
    parts = ["mousecfg"]
    if profile and profile != "—":
        parts.append(profile)
    parts.append(battery)
    if name:
        parts.append(name)
    if note:
        parts.append(note)
    return " · ".join(parts)


__all__ = ["battery_display", "battery_note", "hover_title"]
