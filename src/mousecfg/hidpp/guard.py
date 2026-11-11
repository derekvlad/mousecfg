"""Классификация HID++ команд: что можно, что нельзя и когда можно.

Требование проекта: настройки применяются волатильно, а во встроенную
память мыши не пишется ничего. Гарантия держится не на аккуратности
разработчика, а на том, что каждая команда перед отправкой проходит через
`safeplane`, а `guard` знает про неё одно из трёх:

* VOLATILE            — живёт в оперативной памяти устройства до отключения;
* PERSISTENT          — попадает в энергонезависимую память; запрещено;
* PERSISTENT_CONSENTED — попадает в память, но может быть выполнено один раз
  с явного согласия пользователя (переход On-Board -> Host mode).

Неизвестная пара (feature, function) не разрешается никогда: пессимистичное
умолчание. Если функция нужна — она описывается в `POLICY` вместе с
обоснованием, а не «пробуется вслепую».
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .protocol import FeatureId, ProtocolError


class Classification(enum.Enum):
    VOLATILE = "volatile"
    PERSISTENT_CONSENTED = "persistent-consented"
    PERSISTENT = "persistent"


@dataclass(frozen=True)
class Rule:
    feature: int
    function: int
    classification: Classification
    note: str
    mutating: bool = False
    """Меняет ли команда состояние устройства.

    Нужно для dry-run: читать можно всегда, а вот записывающие команды в
    этом режиме не уходят на устройство.
    """


# Названия функций для читаемых сообщений и записи в журнал.
_FUNCTION_NAMES: dict[tuple[int, int], str] = {
    (FeatureId.ROOT, 0x10): "ping",
    (FeatureId.ROOT, 0x00): "knock/getFeature",
    (FeatureId.FEATURE_SET, 0x00): "getCount",
    (FeatureId.FEATURE_SET, 0x10): "getFeatureId",
    (FeatureId.DEVICE_NAME, 0x00): "getNameLength",
    (FeatureId.DEVICE_NAME, 0x10): "getChunk",
    (FeatureId.DEVICE_NAME, 0x20): "getKind",
    (FeatureId.DEVICE_FW_VERSION, 0x00): "getEntityCount",
    (FeatureId.DEVICE_FW_VERSION, 0x10): "getEntityInfo",
    (FeatureId.ADJUSTABLE_DPI, 0x00): "getSensorCount",
    (FeatureId.ADJUSTABLE_DPI, 0x10): "getSensorDpiList",
    (FeatureId.ADJUSTABLE_DPI, 0x20): "getSensorDpi",
    (FeatureId.ADJUSTABLE_DPI, 0x30): "setSensorDpi",
    (FeatureId.REPORT_RATE, 0x00): "getSupportedRates",
    (FeatureId.REPORT_RATE, 0x10): "getReportRate",
    (FeatureId.REPORT_RATE, 0x20): "setReportRate",
    (FeatureId.ONBOARD_PROFILES, 0x20): "getDeviceMode",
    (FeatureId.ONBOARD_PROFILES, 0x10): "setDeviceMode",
    (FeatureId.ONBOARD_PROFILES, 0x30): "setActiveProfile",
    (FeatureId.ONBOARD_PROFILES, 0x40): "getActiveProfile",
    (FeatureId.ONBOARD_PROFILES, 0x50): "readSector",
    (FeatureId.ONBOARD_PROFILES, 0x60): "startWriteSector",
    (FeatureId.ONBOARD_PROFILES, 0x70): "writeSectorData",
    (FeatureId.ONBOARD_PROFILES, 0x80): "endWriteSector",
    (FeatureId.MOUSE_BUTTON_SPY, 0x00): "getButtonCount",
    (FeatureId.MOUSE_BUTTON_SPY, 0x10): "getButtonInfo",
    (FeatureId.MOUSE_BUTTON_SPY, 0x20): "setButtonEcho",
    (FeatureId.RGB_EFFECTS, 0x00): "getInfo/zone/effect",
    (FeatureId.RGB_EFFECTS, 0x10): "setZoneEffect",
    (FeatureId.RGB_EFFECTS, 0x50): "setSwControl",
    (FeatureId.RGB_EFFECTS, 0x30): "setNvConfig",
    (FeatureId.PER_KEY_LIGHTING_V2, 0x00): "getInfo",
    (FeatureId.PER_KEY_LIGHTING_V2, 0x10): "getZoneInfo",
    (FeatureId.PER_KEY_LIGHTING_V2, 0x20): "setZoneState",
    (FeatureId.PER_KEY_LIGHTING_V2, 0x30): "setZoneLevel",
    (FeatureId.PER_KEY_LIGHTING_V2, 0x40): "setZoneColor",
    (FeatureId.DEVICE_RESET, 0x00): "reset",
    (FeatureId.CONFIG_DEVICE_PROPS, 0x10): "getProperty",
    (FeatureId.CONFIG_DEVICE_PROPS, 0x20): "setProperty",
    (FeatureId.ENABLE_HIDDEN_FEATURES, 0x00): "setHiddenFeature",
    (FeatureId.DFU_CONTROL_SIGNED, 0x00): "getInfo",
}


def function_name(feature: int, function: int) -> str:
    known = _FUNCTION_NAMES.get((feature, function))
    if known:
        return known
    try:
        feature_name = FeatureId(feature).name.lower()
    except ValueError:
        feature_name = f"unknown:{feature:04x}"
    return f"{feature_name}:{function:#04x}"


POLICY: dict[tuple[int, int], Rule] = {}


def _allow(feature: int, function: int, note: str, *, mutating: bool = False) -> None:
    POLICY[(feature, function)] = Rule(feature, function, Classification.VOLATILE, note, mutating=mutating)


def _deny(feature: int, function: int, note: str) -> None:
    POLICY[(feature, function)] = Rule(feature, function, Classification.PERSISTENT, note, mutating=True)


def _consent(feature: int, function: int, note: str) -> None:
    POLICY[(feature, function)] = Rule(
        feature, function, Classification.PERSISTENT_CONSENTED, note, mutating=True
    )


# --- чтение: безопасно всегда ----------------------------------------
_allow(FeatureId.ROOT, 0x10, "ping: запрос версии протокола")
_allow(FeatureId.ROOT, 0x00, "получение индекса фичи по её id")
_allow(FeatureId.FEATURE_SET, 0x00, "количество фич")
_allow(FeatureId.FEATURE_SET, 0x10, "id фичи по индексу")
_allow(FeatureId.DEVICE_NAME, 0x00, "длина имени")
_allow(FeatureId.DEVICE_NAME, 0x10, "фрагмент имени")
_allow(FeatureId.DEVICE_NAME, 0x20, "тип устройства")
_allow(FeatureId.DEVICE_FW_VERSION, 0x00, "число сущностей прошивки")
_allow(FeatureId.DEVICE_FW_VERSION, 0x10, "информация о прошивке")
_allow(FeatureId.ADJUSTABLE_DPI, 0x00, "число сенсоров")
_allow(FeatureId.ADJUSTABLE_DPI, 0x10, "список поддерживаемых DPI")
_allow(FeatureId.ADJUSTABLE_DPI, 0x20, "текущий DPI")
_allow(FeatureId.REPORT_RATE, 0x00, "поддерживаемые частоты опроса")
_allow(FeatureId.REPORT_RATE, 0x10, "текущая частота опроса")
_allow(FeatureId.ONBOARD_PROFILES, 0x20, "текущий режим устройства (On-Board/Host)")
_allow(FeatureId.MOUSE_BUTTON_SPY, 0x00, "число кнопок")
_allow(FeatureId.MOUSE_BUTTON_SPY, 0x10, "информация о кнопке")
_allow(FeatureId.RGB_EFFECTS, 0x00, "возможности подсветки, зоны и эффекты (чтение)")
_allow(FeatureId.PER_KEY_LIGHTING_V2, 0x00, "возможности per-key подсветки")
_allow(FeatureId.PER_KEY_LIGHTING_V2, 0x10, "информация о зоне per-key")

# --- запись рантайм-настроек: волатильно ------------------------------
_allow(
    FeatureId.ADJUSTABLE_DPI,
    0x30,
    "установка DPI: живёт в RAM до отключения мыши",
    mutating=True,
)
_allow(FeatureId.REPORT_RATE, 0x20, "смена частоты опроса: волатильно", mutating=True)
_allow(
    FeatureId.RGB_EFFECTS,
    0x50,
    "передача управления подсветкой хосту (SW control). Волатильно: "
    "живёт в RAM до отключения мыши; без него прошивка рисует цвет "
    "onboard-профиля и игнорирует наши эффекты",
    mutating=True,
)
_allow(
    FeatureId.RGB_EFFECTS,
    0x10,
    "эффект подсветки зоны (цвет/яркость/режим): волатильно, "
    "пропадёт при отключении мыши — как и остальные настройки профиля",
    mutating=True,
)

# --- то, что писать нельзя --------------------------------------------
_deny(
    FeatureId.ONBOARD_PROFILES,
    0x50,
    "чтение секторов встроенной памяти: не пишет, но и не нужно проекту",
)
_deny(FeatureId.ONBOARD_PROFILES, 0x60, "начало записи сектора onboard-памяти")
_deny(FeatureId.ONBOARD_PROFILES, 0x70, "запись данных сектора onboard-памяти")
_deny(FeatureId.ONBOARD_PROFILES, 0x80, "финализация записи сектора onboard-памяти")
_deny(FeatureId.ONBOARD_PROFILES, 0x30, "выбор активного onboard-профиля")
_deny(FeatureId.DEVICE_RESET, 0x00, "сброс устройства может стереть конфигурацию")
_deny(
    FeatureId.RGB_EFFECTS,
    0x30,
    "NvConfig подсветки: пишет эффект/цвет в энергонезависимую память мыши "
    "(Solaar держит её запрещённой по умолчанию для незнакомых моделей)",
)
_deny(FeatureId.CONFIG_DEVICE_PROPS, 0x20, "изменение свойств конфигурации устройства")
_deny(FeatureId.ENABLE_HIDDEN_FEATURES, 0x00, "включение скрытых фич — путь к неизвестным записям")
_deny(FeatureId.DFU_CONTROL_SIGNED, 0x20, "прошивка устройства")

# --- единственная разрешённая запись в конфигурацию устройства --------
_consent(
    FeatureId.ONBOARD_PROFILES,
    0x10,
    "переключение режима устройства. Host mode — обязательное условие того, "
    "чтобы настройки вообще применялись; выполняется один раз с явного согласия",
)


class DeniedCommand(ProtocolError):
    """Команда запрещена политикой проекта."""

    def __init__(self, feature: int, function: int, reason: str) -> None:
        self.feature = feature
        self.function = function
        self.reason = reason
        super().__init__(f"запрещено: {function_name(feature, function)} ({feature:#06x}/{function:#04x}) — {reason}")


def check(feature: int, function: int, *, allowed_consented: frozenset[tuple[int, int]] = frozenset()) -> Rule:
    """Проверить команду перед отправкой.

    `allowed_consented` — набор пар, на которые пользователь дал явное
    согласие в рамках текущего запуска.
    """
    rule = POLICY.get((feature, function))
    if rule is None:
        raise DeniedCommand(
            feature,
            function,
            "команда неизвестна политике; неизвестное считается записью в память устройства",
        )
    if rule.classification is Classification.PERSISTENT:
        raise DeniedCommand(feature, function, rule.note)
    if rule.classification is Classification.PERSISTENT_CONSENTED and (feature, function) not in allowed_consented:
        raise DeniedCommand(
            feature,
            function,
            f"{rule.note}. Требуется явное согласие (--allow-onboard-switch)",
        )
    return rule


def audit() -> dict[str, list[str]]:
    """Сводка политики — для команды `mousecfg judge`."""
    result: dict[str, list[str]] = {c.value: [] for c in Classification}
    for (feature, function), rule in sorted(POLICY.items()):
        result[rule.classification.value].append(
            f"{feature:#06x}/{function:#04x} {function_name(feature, function)} — {rule.note}"
        )
    return result
