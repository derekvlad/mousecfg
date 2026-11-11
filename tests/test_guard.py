"""Тесты политики безопасности: запрещённое не должно уходить в устройство."""

from __future__ import annotations

import pytest

from mousecfg.hidpp import guard
from mousecfg.hidpp.protocol import FeatureId


def test_volatile_settings_allowed():
    guard.check(FeatureId.ADJUSTABLE_DPI, 0x30)
    guard.check(FeatureId.REPORT_RATE, 0x20)
    guard.check(FeatureId.ADJUSTABLE_DPI, 0x20)


def test_lighting_volatile_allowed_persistent_denied():
    """Подсветка: волатильная запись эффекта (0x10) разрешена, NvConfig (0x30) — нет."""
    rule = guard.check(FeatureId.RGB_EFFECTS, 0x10)
    assert rule.classification is guard.Classification.VOLATILE
    guard.check(FeatureId.RGB_EFFECTS, 0x00)  # чтение зон/эффектов
    with pytest.raises(guard.DeniedCommand):
        guard.check(FeatureId.RGB_EFFECTS, 0x30)


def test_lighting_sw_control_claim_is_volatile():
    """Claim SW-контроля (0x50) — волатильная команда, разрешена.

    Без неё прошивка рисует цвет onboard-профиля и игнорирует наши эффекты.
    Проверено на железе: это RAM-состояние, не запись в память.
    """
    rule = guard.check(FeatureId.RGB_EFFECTS, 0x50)
    assert rule.classification is guard.Classification.VOLATILE
    assert rule.mutating


@pytest.mark.parametrize(
    ("feature", "function"),
    [
        (FeatureId.ONBOARD_PROFILES, 0x30),  # выбор активного onboard-профиля
        (FeatureId.ONBOARD_PROFILES, 0x50),  # чтение секторов памяти
        (FeatureId.ONBOARD_PROFILES, 0x60),  # начало записи сектора
        (FeatureId.ONBOARD_PROFILES, 0x70),  # данные сектора
        (FeatureId.ONBOARD_PROFILES, 0x80),  # финализация записи
        (FeatureId.DEVICE_RESET, 0x00),
        (FeatureId.CONFIG_DEVICE_PROPS, 0x20),
        (FeatureId.ENABLE_HIDDEN_FEATURES, 0x00),
    ],
)
def test_persistent_commands_denied(feature, function):
    with pytest.raises(guard.DeniedCommand):
        guard.check(feature, function)


def test_mode_switch_requires_consent():
    with pytest.raises(guard.DeniedCommand):
        guard.check(FeatureId.ONBOARD_PROFILES, 0x10)

    rule = guard.check(FeatureId.ONBOARD_PROFILES, 0x10, allowed_consented={(FeatureId.ONBOARD_PROFILES, 0x10)})
    assert rule.classification is guard.Classification.PERSISTENT_CONSENTED


def test_unknown_command_denied():
    """Неизвестное считается записью — иначе политика ничего не стоит."""
    with pytest.raises(guard.DeniedCommand):
        guard.check(0xDEAD, 0x00)
    with pytest.raises(guard.DeniedCommand):
        guard.check(FeatureId.ADJUSTABLE_DPI, 0x40)  # не существует
    with pytest.raises(guard.DeniedCommand):
        guard.check(FeatureId.REPORT_RATE, 0x30)


def test_audit_lists_three_groups():
    audit = guard.audit()
    assert audit["volatile"]
    assert audit["persistent"]
    assert len(audit["persistent-consented"]) == 1


def test_function_names_are_readable():
    assert guard.function_name(FeatureId.ADJUSTABLE_DPI, 0x30) == "setSensorDpi"
    assert "unknown" in guard.function_name(0xDEAD, 0x00)
