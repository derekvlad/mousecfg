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

from mousecfg.i18n import t

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
_allow(FeatureId.ROOT, 0x10, t("ping: запрос версии протокола", "ping: protocol version request"))
_allow(FeatureId.ROOT, 0x00, t("получение индекса фичи по её id", "get a feature index by its id"))
_allow(FeatureId.FEATURE_SET, 0x00, t("количество фич", "number of features"))
_allow(FeatureId.FEATURE_SET, 0x10, t("id фичи по индексу", "feature id by index"))
_allow(FeatureId.DEVICE_NAME, 0x00, t("длина имени", "name length"))
_allow(FeatureId.DEVICE_NAME, 0x10, t("фрагмент имени", "name chunk"))
_allow(FeatureId.DEVICE_NAME, 0x20, t("тип устройства", "device kind"))
_allow(FeatureId.DEVICE_FW_VERSION, 0x00, t("число сущностей прошивки", "number of firmware entities"))
_allow(FeatureId.DEVICE_FW_VERSION, 0x10, t("информация о прошивке", "firmware information"))
_allow(FeatureId.ADJUSTABLE_DPI, 0x00, t("число сенсоров", "number of sensors"))
_allow(FeatureId.ADJUSTABLE_DPI, 0x10, t("список поддерживаемых DPI", "list of supported DPI values"))
_allow(FeatureId.ADJUSTABLE_DPI, 0x20, t("текущий DPI", "current DPI"))
_allow(FeatureId.REPORT_RATE, 0x00, t("поддерживаемые частоты опроса", "supported report rates"))
_allow(FeatureId.REPORT_RATE, 0x10, t("текущая частота опроса", "current report rate"))
_allow(FeatureId.ONBOARD_PROFILES, 0x20, t("текущий режим устройства (On-Board/Host)", "current device mode (On-Board/Host)"))
_allow(FeatureId.MOUSE_BUTTON_SPY, 0x00, t("число кнопок", "number of buttons"))
_allow(FeatureId.MOUSE_BUTTON_SPY, 0x10, t("информация о кнопке", "button information"))
_allow(FeatureId.RGB_EFFECTS, 0x00, t("возможности подсветки, зоны и эффекты (чтение)", "lighting capabilities, zones and effects (read)"))
_allow(FeatureId.PER_KEY_LIGHTING_V2, 0x00, t("возможности per-key подсветки", "per-key lighting capabilities"))
_allow(FeatureId.PER_KEY_LIGHTING_V2, 0x10, t("информация о зоне per-key", "per-key zone information"))

# --- запись рантайм-настроек: волатильно ------------------------------
_allow(
    FeatureId.ADJUSTABLE_DPI,
    0x30,
    t("установка DPI: живёт в RAM до отключения мыши", "set DPI: lives in RAM until the mouse is disconnected"),
    mutating=True,
)
_allow(FeatureId.REPORT_RATE, 0x20, t("смена частоты опроса: волатильно", "change report rate: volatile"), mutating=True)
_allow(
    FeatureId.RGB_EFFECTS,
    0x50,
    t("передача управления подсветкой хосту (SW control). Волатильно: "
    "живёт в RAM до отключения мыши; без него прошивка рисует цвет "
    "onboard-профиля и игнорирует наши эффекты",
      "hand lighting control over to the host (SW control). Volatile: "
      "lives in RAM until the mouse is disconnected; without it the firmware "
      "draws the onboard profile colour and ignores our effects"),
    mutating=True,
)
_allow(
    FeatureId.RGB_EFFECTS,
    0x10,
    t("эффект подсветки зоны (цвет/яркость/режим): волатильно, "
    "пропадёт при отключении мыши — как и остальные настройки профиля",
      "zone lighting effect (colour/brightness/mode): volatile, "
      "lost when the mouse is disconnected, like the rest of the profile"),
    mutating=True,
)

# --- то, что писать нельзя --------------------------------------------
_deny(
    FeatureId.ONBOARD_PROFILES,
    0x50,
    t("чтение секторов встроенной памяти: не пишет, но и не нужно проекту", "read onboard memory sectors: does not write, but the project does not need it"),
)
_deny(FeatureId.ONBOARD_PROFILES, 0x60, t("начало записи сектора onboard-памяти", "start writing an onboard memory sector"))
_deny(FeatureId.ONBOARD_PROFILES, 0x70, t("запись данных сектора onboard-памяти", "write onboard memory sector data"))
_deny(FeatureId.ONBOARD_PROFILES, 0x80, t("финализация записи сектора onboard-памяти", "finalize onboard memory sector write"))
_deny(FeatureId.ONBOARD_PROFILES, 0x30, t("выбор активного onboard-профиля", "select the active onboard profile"))
_deny(FeatureId.DEVICE_RESET, 0x00, t("сброс устройства может стереть конфигурацию", "device reset may erase the configuration"))
_deny(
    FeatureId.RGB_EFFECTS,
    0x30,
    t("NvConfig подсветки: пишет эффект/цвет в энергонезависимую память мыши "
    "(Solaar держит её запрещённой по умолчанию для незнакомых моделей)",
      "lighting NvConfig: writes the effect/colour to the mouse non-volatile memory "
      "(Solaar keeps it denied by default for unknown models)"),
)
_deny(FeatureId.CONFIG_DEVICE_PROPS, 0x20, t("изменение свойств конфигурации устройства", "change device configuration properties"))
_deny(FeatureId.ENABLE_HIDDEN_FEATURES, 0x00, t("включение скрытых фич — путь к неизвестным записям", "enabling hidden features leads to unknown writes"))
_deny(FeatureId.DFU_CONTROL_SIGNED, 0x20, t("прошивка устройства", "device firmware update"))

# --- единственная разрешённая запись в конфигурацию устройства --------
_consent(
    FeatureId.ONBOARD_PROFILES,
    0x10,
    t("переключение режима устройства. Host mode — обязательное условие того, "
    "чтобы настройки вообще применялись; выполняется один раз с явного согласия",
      "switch the device mode. Host mode is required for settings to apply at all; "
      "performed once with explicit consent"),
)


class DeniedCommand(ProtocolError):
    """Команда запрещена политикой проекта."""

    def __init__(self, feature: int, function: int, reason: str) -> None:
        self.feature = feature
        self.function = function
        self.reason = reason
        super().__init__(t(f"запрещено: {function_name(feature, function)} ({feature:#06x}/{function:#04x}) — {reason}",
                           f"denied: {function_name(feature, function)} ({feature:#06x}/{function:#04x}) — {reason}"))


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
            t("команда неизвестна политике; неизвестное считается записью в память устройства",
                "the command is unknown to the policy; anything unknown is treated as a memory write"),
        )
    if rule.classification is Classification.PERSISTENT:
        raise DeniedCommand(feature, function, rule.note)
    if rule.classification is Classification.PERSISTENT_CONSENTED and (feature, function) not in allowed_consented:
        raise DeniedCommand(
            feature,
            function,
            t(f"{rule.note}. Требуется явное согласие (--allow-onboard-switch)",
                f"{rule.note}. Explicit consent is required (--allow-onboard-switch)"),
        )
    return rule


def audit() -> dict[str, list[str]]:
    """Сводка политики — для команды `mousecfg judge`."""
    result: dict[str, list[str]] = {c.value: [] for c in Classification}
    for (feature, function), rule in sorted(POLICY.items()):
        result[rule.classification.value].append(
            t(f"{feature:#06x}/{function:#04x} {function_name(feature, function)} — {rule.note}",
                f"{feature:#06x}/{function:#04x} {function_name(feature, function)} — {rule.note}")
        )
    return result
