"""Применение профиля к устройству и описание результата.

Выполняются только волатильные команды: DPI и report rate. Всё, что
требует записи в память мыши, здесь просто отсутствует.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .hidpp.device import HidppDevice
from .hidpp.protocol import DeviceError, FeatureUnsupportedError, ProtocolError
from .i18n import t
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
        parts = [t(f"профиль {self.profile_id}", f"profile {self.profile_id}")]
        if self.applied:
            parts.append(", ".join(f"{key}={value}" for key, value in self.applied.items()))
        if self.skipped:
            parts.append(t("пропущено: ", "skipped: ") + "; ".join(self.skipped))
        if self.error:
            parts.append(t(f"ошибка: {self.error}", f"error: {self.error}"))
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
            plan[name] = t(f"ошибка: {exc}", f"error: {exc}")
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

    if profile.lighting:
        _apply_lighting(device, profile, result)

    if profile.buttons:
        result.skipped.append(t("кнопки применяет evdev-слой (см. mousecfg run-profile)", "buttons are applied by the evdev layer (see mousecfg run-profile)"))

    return result


def _apply_lighting(device: HidppDevice, profile: Profile, result: ApplyResult) -> None:
    """Применить подсветку профиля волатильно (fn 0x10, не NvConfig)."""
    from .hidpp.device import LIGHTING_EFFECTS

    lighting = profile.lighting or {}
    if not device.has_feature(0x8071):
        result.skipped.append(t("подсветка: устройство не поддерживает RGB_EFFECTS", "lighting: the device does not support RGB_EFFECTS"))
        return

    effect_name = str(lighting.get("effect", "static" if "color" in lighting else "disabled")).lower()
    effects_by_name = {name: effect_id for effect_id, (name, _) in LIGHTING_EFFECTS.items()}
    effect_id = effects_by_name.get(effect_name)
    if effect_id is None:
        result.skipped.append(t(f"подсветка: эффект {effect_name!r} неизвестен устройству", f"lighting: effect {effect_name!r} is unknown to the device"))
        return

    schema = LIGHTING_EFFECTS[effect_id][1]
    params: dict[str, int] = {}
    if "color" in lighting and "color" in schema:
        params["color"] = int(lighting["color"])
    brightness = lighting.get("brightness")
    if brightness is not None:
        intensity = max(0, min(255, round(int(brightness) * 255 / 100)))
        if "intensity" in schema:
            params["intensity"] = intensity
        elif "color" in schema and effect_name == "static":
            # У static яркости как байта нет: притемняем сам цвет.
            params["color"] = _scale_color(int(lighting.get("color", 0xFFFFFF)), int(brightness))
    for key in ("period", "intensity", "form", "ramp", "speed", "direction"):
        if key in lighting and key in schema:
            params[key] = int(lighting[key])

    zone = int(lighting.get("zone", 0))
    try:
        device.set_lighting(effect_id, params, zone=zone)
    except (FeatureUnsupportedError, DeviceError, ProtocolError, TimeoutError, ValueError) as exc:
        result.skipped.append(t(f"подсветка: {exc}", f"lighting: {exc}"))
        return
    result.applied["lighting"] = effect_name


def _scale_color(color: int, brightness: int) -> int:
    factor = max(0.0, min(1.0, brightness / 100.0))
    red = round(((color >> 16) & 0xFF) * factor)
    green = round(((color >> 8) & 0xFF) * factor)
    blue = round((color & 0xFF) * factor)
    return (red << 16) | (green << 8) | blue

