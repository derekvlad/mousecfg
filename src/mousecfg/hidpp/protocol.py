"""HID++ 2.0 framing.

Только то, что нужно проекту: короткие/длинные сообщения, Software ID,
разбор ответов. Никаких записей в устройство здесь нет — этим занимается
`safeplane`, а классификацией команд — `guard`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from mousecfg.i18n import t

SHORT_REPORT_ID = 0x10
LONG_REPORT_ID = 0x11
SHORT_FRAME_SIZE = 7
LONG_FRAME_SIZE = 20

# Индекс устройства для проводного USB-подключения (без ресивера).
USB_DEVICE_INDEX = 0xFF

# Software ID занимает младший ниббл байта функции. Значения, занятые другими
# инструментами: 0x07 OpenRGB, 0x0A LGSTrayEx, 0x0B Solaar, 0x0D G HUB,
# 0x0F прошивка. Берём свободный 0x09, со взведённым битом 3, чтобы
# нотификации (sw_id = 0) было легко отличить от ответов.
SOFTWARE_ID = 0x09

# Ответ с ошибкой: [0xFF, feature_index, function, error_code, ...]
ERROR_MARKER = 0xFF

# Кадр HID++ 1.0 «запись в регистры» (0x80xx, 0x81xx, 0x82xx, 0x83xx).
# Проект такого не отправляет никогда: долговременная конфигурация живёт
# именно там.
HIDPP10_WRITE_FUNCTION_IDS = frozenset({0x80, 0x81, 0x82, 0x83})


class FeatureId(IntEnum):
    """Идентификаторы фич, которые проект умеет разбирать."""

    ROOT = 0x0000
    FEATURE_SET = 0x0001
    DEVICE_FW_VERSION = 0x0003
    DEVICE_NAME = 0x0005
    DEVICE_RESET = 0x1802
    CONFIG_DEVICE_PROPS = 0x1806
    ENABLE_HIDDEN_FEATURES = 0x1E00
    ADJUSTABLE_DPI = 0x2201
    REPORT_RATE = 0x8060
    RGB_EFFECTS = 0x8071
    PER_KEY_LIGHTING_V2 = 0x8081
    ONBOARD_PROFILES = 0x8100
    MOUSE_BUTTON_SPY = 0x8110
    DFU_CONTROL_SIGNED = 0x00C2


HIDPP_ERRORS = {
    0x00: "NO_ERROR",
    0x01: "UNKNOWN",
    0x02: "INVALID_ARGUMENT",
    0x03: "OUT_OF_RANGE",
    0x04: "HARDWARE_ERROR",
    0x05: "LOGITECH_INTERNAL",
    0x06: "INVALID_FEATURE_INDEX",
    0x07: "INVALID_FUNCTION_ID",
    0x08: "BUSY",
    0x09: "UNSUPPORTED",
    0x0A: "INVALID_HIDPP_MSG_ID",
    0xFF: "UNKNOWN_ERROR",
}


class TransportError(OSError):
    """Устройство пропало или hidraw недоступен."""


class ProtocolError(Exception):
    """Устройство ответило понятным, но неожиданным образом."""


class FeatureUnsupportedError(ProtocolError):
    def __init__(self, feature: int) -> None:
        super().__init__(t(f"feature {feature:#06x} отсутствует у устройства", f"feature {feature:#06x} is not present on the device"))
        self.feature = feature


class DeviceError(ProtocolError):
    def __init__(self, feature: int, function: int, code: int) -> None:
        self.code = code
        self.feature = feature
        self.function = function
        super().__init__(
            t(f"HID++ ошибка {code:#04x} ({HIDPP_ERRORS.get(code, 'unknown')}) "
              f"для feature {feature:#06x} функция {function >> 4}",
              f"HID++ error {code:#04x} ({HIDPP_ERRORS.get(code, 'unknown')}) "
              f"for feature {feature:#06x} function {function >> 4}")
        )


def build_frame(
    device_index: int,
    feature_index: int,
    function: int,
    params: bytes = b"",
    *,
    software_id: int = SOFTWARE_ID,
    force_long: bool | None = None,
) -> bytes:
    """Собрать кадр HID++ 2.0.

    `function` задаётся в «Solaar-нотации» — уже сдвинутый в старший ниббл
    (0x00, 0x10, 0x20, 0x30, ...). Младший ниббл займёт Software ID.
    """
    if not 0 <= device_index <= 0xFF:
        raise ValueError(t("device_index вне диапазона", "device_index is out of range"))
    if not 0 <= feature_index <= 0xFF:
        raise ValueError(t("feature_index вне диапазона", "feature_index is out of range"))
    if function & 0x0F:
        raise ValueError(t(f"function должен быть кратен 0x10, получено {function:#04x}", f"function must be a multiple of 0x10, got {function:#04x}"))

    payload = struct.pack("!BB", feature_index, function | (software_id & 0x0F)) + params

    long_message = len(payload) > SHORT_FRAME_SIZE - 2 if force_long is None else force_long
    if long_message:
        if len(payload) > LONG_FRAME_SIZE - 2:
            raise ValueError(t("слишком длинный параметр для длинного сообщения", "the parameter is too long for a long message"))
        return struct.pack("!BB18s", LONG_REPORT_ID, device_index, payload)
    if len(payload) > SHORT_FRAME_SIZE - 2:
        raise ValueError(t("параметры не помещаются в короткое сообщение", "the parameters do not fit into a short message"))
    return struct.pack("!BB5s", SHORT_REPORT_ID, device_index, payload)


def build_ping_frame(device_index: int = USB_DEVICE_INDEX, mark: int = 0x5A, *, software_id: int = SOFTWARE_ID) -> bytes:
    """Ping устройства: ROOT, функция 0x10, 3 параметра (0x00, 0x00, mark)."""
    return build_frame(device_index, 0x00, 0x10, bytes((0x00, 0x00, mark)), software_id=software_id)


@dataclass(frozen=True)
class Reply:
    report_id: int
    device_index: int
    feature_index: int
    function: int
    params: bytes
    raw: bytes

    @property
    def is_error(self) -> bool:
        return self.feature_index == ERROR_MARKER

    @property
    def error_code(self) -> int | None:
        return self.function if self.is_error else None

    @property
    def function_index(self) -> int:
        return self.function >> 4


def parse_frame(frame: bytes) -> Reply:
    """Разобрать кадр, принятый от устройства (с report id первым байтом)."""
    if len(frame) < 4:
        raise ProtocolError(t(f"слишком короткий кадр: {frame.hex()}", f"frame is too short: {frame.hex()}"))

    report_id = frame[0]
    if report_id not in (SHORT_REPORT_ID, LONG_REPORT_ID):
        raise ProtocolError(t(f"неизвестный report id {report_id:#04x}: {frame.hex()}", f"unknown report id {report_id:#04x}: {frame.hex()}"))

    if report_id == SHORT_REPORT_ID and len(frame) != SHORT_FRAME_SIZE:
        raise ProtocolError(t(f"короткий кадр неверной длины {len(frame)}: {frame.hex()}", f"short frame has wrong length {len(frame)}: {frame.hex()}"))
    if report_id == LONG_REPORT_ID and len(frame) != LONG_FRAME_SIZE:
        raise ProtocolError(t(f"длинный кадр неверной длины {len(frame)}: {frame.hex()}", f"long frame has wrong length {len(frame)}: {frame.hex()}"))

    return Reply(
        report_id=report_id,
        device_index=frame[1],
        feature_index=frame[2],
        function=frame[3],
        params=bytes(frame[4:]),
        raw=bytes(frame),
    )


def is_notification(frame: bytes) -> bool:
    """Кадр от устройства с software id = 0 — нотификация, а не ответ."""
    if len(frame) < 4:
        return False
    return frame[0] in (SHORT_REPORT_ID, LONG_REPORT_ID) and (frame[3] & 0x0F) == 0


def u16_be(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("!H", data, offset)[0]


def u16_bytes(value: int) -> bytes:
    return struct.pack("!H", value)
