"""Применение профиля к устройству и описание результата.

Выполняются только волатильные команды: DPI и report rate. Всё, что
требует записи в память мыши, здесь просто отсутствует.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .hidpp.device import HidppDevice
from .hidpp.protocol import DeviceError, FeatureUnsupportedError, ProtocolError
from .profiles import Profile

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    profile_id: str
    applied: dict[str, object] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def describe(self) -> str:
        parts = [f"профиль {self.profile_id}"]
        if self.applied:
            parts.append(", ".join(f"{key}={value}" for key, value in self.applied.items()))
        if self.skipped:
            parts.append("пропущено: " + "; ".join(self.skipped))
        if self.error:
            parts.append(f"ошибка: {self.error}")
        return " | ".join(parts)


def plan_button_actions(profile: Profile) -> dict[str, str]:
    """Человекочитаемый план для кнопок профиля.

    Реальный ремап выполняет evdev-слой (input/remapper.py); здесь только
    разбор профиля, чтобы GUI и CLI показывали одно и то же.
    """
    from .input.actions import ActionError, parse_action

    plan: dict[str, str] = {}
    for name, spec in profile.buttons.items():
        try:
            plan[name] = parse_action(spec).describe()
        except ActionError as exc:
            plan[name] = f"ошибка: {exc}"
    return plan


def apply_profile(device: HidppDevice, profile: Profile) -> ApplyResult:
    """Применить волатильную часть профиля."""
    result = ApplyResult(profile_id=profile.id)

    if profile.dpi is not None:
        try:
            device.set_dpi(profile.dpi)
            result.applied["dpi"] = device.get_dpi()
        except (FeatureUnsupportedError, DeviceError, ProtocolError, TimeoutError) as exc:
            result.error = f"DPI: {exc}"
            return result

    if profile.report_rate_ms is not None:
        try:
            device.set_rate_ms(profile.report_rate_ms)
            result.applied["report_rate_ms"] = device.get_rate_ms()
        except (FeatureUnsupportedError, DeviceError, ProtocolError, TimeoutError) as exc:
            # Смена частоты опроса иногда перезапускает USB-устройство; это не
            # повод считать весь профиль неприменённым.
            result.skipped.append(f"report_rate: {exc}")

    if profile.buttons:
        result.skipped.append("кнопки применяет evdev-слой (см. mousecfg run-profile)")

    return result
