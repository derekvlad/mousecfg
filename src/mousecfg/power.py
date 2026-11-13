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


__all__ = ["battery_display"]
