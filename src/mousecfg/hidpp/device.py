"""Устройство Logitech с протоколом HID++ 2.0.

Класс держит карту фич устройства и предоставляет типизированные операции
(имя, прошивка, DPI, report rate, режим onboard). Любая отправка идёт через
`SafePlane`, поэтому запрещённые команды отсекаются ещё до записи в hidraw.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field

from . import guard, protocol
from .protocol import (
    DeviceError,
    FeatureId,
    FeatureUnsupportedError,
    ProtocolError,
    TransportError,
    parse_frame,
)
from .safeplane import Journal, SafePlane
from .transport import HidrawTransport

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 0.6
DEFAULT_RETRIES = 3
# Сколько посторонних кадров (трафик Solaar/Piper) терпим, прежде чем сдаться.
_MAX_FOREIGN_REPLIES = 10
# Код BUSY: устройство занято (например, своей внутренней работой после
# переключения настроек). Значение временное, повторяем.
BUSY_ERROR_CODE = 0x08
RETRY_DELAY = 0.05


@dataclass
class DeviceInfo:
    path: str
    name: str = ""
    kind: str = ""
    protocol_version: tuple[int, int] | None = None
    firmware: list[str] = field(default_factory=list)
    serial: str = ""


# Названия расположений зон (Solaar LEDZoneLocations).
LIGHTING_LOCATIONS = {
    0x00: "unknown",
    0x01: "primary",
    0x02: "logo",
    0x03: "left-side",
    0x04: "right-side",
    0x05: "combined",
}

# Эффекты подсветки: id -> (имя, {параметр: (смещение, размер)}).
# Таблица повторяет проверенные на железе кадры Solaar (hidpp20.LEDEffects).
LIGHTING_EFFECTS: dict[int, tuple[str, dict[str, tuple[int, int]]]] = {
    0x00: ("disabled", {}),
    0x01: ("static", {"color": (0, 3), "ramp": (3, 1)}),
    0x02: ("pulse", {"color": (0, 3), "speed": (3, 1)}),
    0x03: ("cycle", {"period": (5, 2), "intensity": (7, 1)}),
    0x04: ("wave", {"period": (6, 2), "direction": (9, 1)}),
    0x0A: ("breathe", {"color": (0, 3), "period": (3, 2), "form": (5, 1), "intensity": (6, 1)}),
    0x0B: ("ripple", {"color": (0, 3), "period": (4, 2)}),
    0x0E: ("decomposition", {"period": (6, 2), "intensity": (8, 1)}),
}


def lighting_effect_name(effect_id: int) -> str:
    known = LIGHTING_EFFECTS.get(effect_id)
    return known[0] if known else f"effect-{effect_id:#04x}"


def _lighting_effect_bytes(slot: int, effect_id: int, params: dict[str, int]) -> bytes:
    """Собрать 11-байтовое тело эффекта: слот + 10 байтов параметров."""
    schema = LIGHTING_EFFECTS.get(effect_id, (None, {}))[1]
    unknown = set(params) - set(schema)
    if unknown:
        raise ValueError(
            f"эффект {lighting_effect_name(effect_id)!r} не знает параметров: {', '.join(sorted(unknown))}"
        )
    body = bytearray(10)
    for name, (offset, size) in schema.items():
        value = int(params.get(name, 0))
        if not 0 <= value < (1 << (8 * size)):
            raise ValueError(f"параметр {name}={value} не влезает в {size} байт")
        body[offset : offset + size] = value.to_bytes(size, "big")
    return bytes((slot,)) + bytes(body)


@dataclass
class LightingEffect:
    slot: int
    effect_id: int
    capabilities: int = 0
    period: int = 0

    @property
    def name(self) -> str:
        return lighting_effect_name(self.effect_id)


@dataclass
class LightingZone:
    index: int
    location: int = 0
    effects: list[LightingEffect] = field(default_factory=list)

    @property
    def location_name(self) -> str:
        return LIGHTING_LOCATIONS.get(self.location, f"zone-{self.location:#04x}")


@dataclass
class LightingInfo:
    supported: bool
    capabilities: int = 0
    zones: list[LightingZone] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return bool(self.capabilities & 0x0001)


class HidppDevice:
    def __init__(
        self,
        transport: HidrawTransport,
        *,
        journal: Journal | None = None,
        allow_onboard_switch: bool = False,
        dry_run: bool = False,
        serial: str = "",
    ) -> None:
        self.transport = transport
        self.safeplane = SafePlane(
            transport,
            allow_onboard_switch=allow_onboard_switch,
            journal=journal,
            dry_run=dry_run,
        )
        self.serial = serial
        self._features: dict[int, int] = {}

    # -- низкий уровень -------------------------------------------------
    def ping(self, timeout: float = 1.0) -> tuple[int, int]:
        """Вернуть версию протокола (major, minor)."""
        mark = int(time.time() * 1000) & 0xFF or 0x5A
        frame = protocol.build_ping_frame(self.transport.device_index, mark=mark)

        for attempt in range(DEFAULT_RETRIES + 1):
            try:
                self.transport.drain()
                self.transport.send(frame)  # ping не несёт конфигурации, политика не нужна
                raw = self.transport.expect_reply(timeout)
            except TimeoutError:
                if attempt == DEFAULT_RETRIES:
                    raise
                continue

            reply = parse_frame(raw)
            if reply.device_index not in (self.transport.device_index, self.transport.device_index ^ 0xFF):
                continue
            major, minor = reply.params[0], reply.params[1]
            return major, minor
        raise ProtocolError("устройство не ответило на ping")

    def request(
        self,
        feature: int,
        function: int,
        *params: int | bytes,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> bytes:
        """Отправить feature-запрос и вернуть параметры ответа.

        `params` — целые (по одному байту) или готовые байтовые строки.
        """
        feature_index = self.feature_index(feature)
        payload = b"".join(
            bytes((value,)) if isinstance(value, int) else bytes(value) for value in params
        )
        frame = protocol.build_frame(
            self.transport.device_index,
            feature_index,
            function,
            payload,
            software_id=protocol.SOFTWARE_ID,
        )

        for attempt in range(retries + 1):
            sent = self.safeplane.send(feature, function, payload, frame)
            if not sent:
                # dry-run изменяющей команды: ответа не будет, и это правильно.
                return b""
            try:
                return self._wait_for_reply(feature, function, feature_index, timeout)
            except TimeoutError:
                if attempt == retries:
                    raise
                logger.debug(
                    "нет ответа на %s (%#06x/%#04x), повтор %d",
                    guard.function_name(feature, function),
                    feature,
                    function,
                    attempt + 1,
                )
            except DeviceError as exc:
                if exc.code != BUSY_ERROR_CODE or attempt == retries:
                    raise
                logger.debug("устройство занято (BUSY), повтор %d", attempt + 1)
            time.sleep(RETRY_DELAY * (attempt + 1))
        raise ProtocolError("недостижимо")

    def _wait_for_reply(self, feature: int, function: int, feature_index: int, timeout: float) -> bytes:
        expected_function = function | protocol.SOFTWARE_ID
        for _ in range(_MAX_FOREIGN_REPLIES):
            raw = self.transport.expect_reply(timeout)
            try:
                reply = parse_frame(raw)
            except ProtocolError:
                # Мышь шлёт по этому же узлу обычные input-отчёты и свои
                # нотификации. Это не ошибка — просто читаем дальше.
                logger.debug("не HID++ кадр, пропускаем: %s", raw.hex())
                continue
            if reply.is_error:
                raise DeviceError(feature, function, reply.error_code or 0)
            if reply.feature_index != feature_index or reply.function != expected_function:
                logger.debug("чужой кадр, пропускаем: %s", raw.hex())
                continue
            return reply.params
        raise ProtocolError("не удалось получить ответ на наш запрос")

    # -- карта фич ------------------------------------------------------
    def feature_index(self, feature: int) -> int:
        if feature in self._features:
            return self._features[feature]
        self.discover_features()
        if feature not in self._features:
            raise FeatureUnsupportedError(feature)
        return self._features[feature]

    def discover_features(self, *, refresh: bool = False) -> dict[int, int]:
        """Построить карту feature id -> index через ROOT и FEATURE_SET."""
        if self._features and not refresh:
            return self._features

        features: dict[int, int] = {}

        root_reply = self._raw_request(0, 0x00, struct.pack("!H", FeatureId.FEATURE_SET))
        feature_set_index = root_reply[0]
        if not feature_set_index:
            raise ProtocolError("устройство без FEATURE_SET: HID++ 2.0 не поддерживается")

        features[FeatureId.ROOT] = 0
        features[FeatureId.FEATURE_SET] = feature_set_index

        count_reply = self._raw_request(feature_set_index, 0x00)
        count = count_reply[0]
        logger.debug("FEATURE_SET: %d фич, индекс 0x%02x", count, feature_set_index)

        for index in range(count + 1):
            if index in (0, feature_set_index):
                continue
            try:
                reply = self._raw_request(feature_set_index, 0x10, bytes((index,)))
            except DeviceError as exc:
                logger.debug("фича по индексу %d недоступна: %s", index, exc)
                continue
            if len(reply) < 2:
                continue
            feature_id = protocol.u16_be(reply, 0)
            features[feature_id] = index

        self._features = features
        return features

    def has_feature(self, feature: int) -> bool:
        return feature in self.discover_features()

    def _raw_request(
        self,
        feature_index: int,
        function: int,
        payload: bytes = b"",
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes:
        frame = protocol.build_frame(
            self.transport.device_index, feature_index, function, payload, software_id=protocol.SOFTWARE_ID
        )
        self.safeplane.send_by_index(
            feature_index,
            function,
            payload,
            frame,
            name="root-knock" if (feature_index, function) == (0, 0x00) else "featset-enumerate",
        )
        expected_function = function | protocol.SOFTWARE_ID
        for _ in range(_MAX_FOREIGN_REPLIES):
            raw = self.transport.expect_reply(timeout)
            try:
                reply = parse_frame(raw)
            except ProtocolError:
                logger.debug("не HID++ кадр, пропускаем: %s", raw.hex())
                continue
            if reply.is_error:
                raise DeviceError(feature_index, function, reply.error_code or 0)
            if reply.feature_index != feature_index or reply.function != expected_function:
                # Нотификации мыши и трафик других клиентов (Solaar, Piper).
                logger.debug("чужой кадр, пропускаем: %s", raw.hex())
                continue
            return reply.params
        raise ProtocolError("не удалось получить ответ на перечисление фич")

    # -- информационные операции ---------------------------------------
    def get_name(self) -> str:
        length_reply = self.request(FeatureId.DEVICE_NAME, 0x00)
        if not length_reply:
            return ""
        length = length_reply[0]
        name = b""
        while len(name) < length:
            chunk = self.request(FeatureId.DEVICE_NAME, 0x10, len(name))
            if not chunk:
                break
            name += chunk[: length - len(name)]
        return name.decode("utf-8", errors="replace")

    def get_kind(self) -> str:
        try:
            reply = self.request(FeatureId.DEVICE_NAME, 0x20)
        except (FeatureUnsupportedError, DeviceError):
            return ""
        kinds = {0: "keyboard", 1: "remote-control", 2: "numpad", 3: "mouse", 4: "touchpad", 5: "trackball", 6: "presenter"}
        return kinds.get(reply[0], "") if reply else ""

    def get_firmware(self) -> list[str]:
        try:
            count_reply = self.request(FeatureId.DEVICE_FW_VERSION, 0x00)
        except (FeatureUnsupportedError, DeviceError):
            return []
        count = count_reply[0] if count_reply else 0
        result: list[str] = []
        for index in range(count):
            try:
                info = self.request(FeatureId.DEVICE_FW_VERSION, 0x10, index)
            except DeviceError:
                continue
            if len(info) < 8:
                continue
            level = info[0] & 0x0F
            if level in (0, 1):
                name = info[1:4].rstrip(b"\x00").decode("ascii", errors="replace")
                major, minor, build = struct.unpack("!BBH", info[4:8])
                version = f"{major:02X}.{minor:02X}"
                if build:
                    version += f".B{build:04X}"
                result.append(f"{name} {version}".strip())
        return result

    # -- DPI ------------------------------------------------------------
    def get_dpi(self, sensor: int = 0) -> int:
        reply = self.request(FeatureId.ADJUSTABLE_DPI, 0x20, sensor)
        if len(reply) < 5:
            raise ProtocolError(f"короткий ответ DPI: {reply.hex()}")
        current = protocol.u16_be(reply, 1)
        default = protocol.u16_be(reply, 3)
        return current or default

    def get_dpi_default(self, sensor: int = 0) -> int:
        reply = self.request(FeatureId.ADJUSTABLE_DPI, 0x20, sensor)
        return protocol.u16_be(reply, 3)

    def get_sensor_count(self) -> int:
        reply = self.request(FeatureId.ADJUSTABLE_DPI, 0x00)
        return reply[0] if reply else 0

    def set_dpi(self, dpi: int, sensor: int = 0) -> None:
        if not 0 < dpi < 0xFFFF:
            raise ValueError("DPI вне диапазона")
        self.request(FeatureId.ADJUSTABLE_DPI, 0x30, sensor, protocol.u16_bytes(dpi))

    # -- report rate ----------------------------------------------------
    def get_rate_ms(self) -> int:
        reply = self.request(FeatureId.REPORT_RATE, 0x10)
        return reply[0] if reply else 0

    def supported_rates_ms(self) -> list[int]:
        reply = self.request(FeatureId.REPORT_RATE, 0x00)
        flags = reply[0] if reply else 0
        return [index + 1 for index in range(8) if flags & (1 << index)]

    def set_rate_ms(self, milliseconds: int) -> None:
        rates = self.supported_rates_ms()
        if rates and milliseconds not in rates:
            raise ValueError(f"частота {milliseconds} мс не поддерживается, доступны {rates}")
        self.request(FeatureId.REPORT_RATE, 0x20, milliseconds)

    # -- режим onboard --------------------------------------------------
    def get_mode(self) -> str:
        """'onboard', 'host' или 'unknown'."""
        reply = self.request(FeatureId.ONBOARD_PROFILES, 0x20)
        if not reply:
            return "unknown"
        modes = {0x00: "host", 0x01: "onboard", 0x02: "host"}
        return modes.get(reply[0], f"unknown:{reply[0]:#04x}")

    def set_host_mode(self) -> str:
        """Перевести устройство в Host mode. Персистентная операция,
        требует allow_onboard_switch в SafePlane."""
        self.request(FeatureId.ONBOARD_PROFILES, 0x10, 0x02)
        return self.get_mode()

    # -- подсветка (RGB_EFFECTS 0x8071) ---------------------------------
    def lighting_info(self) -> LightingInfo:
        """Возможности подсветки: сколько зон и что они умеют.

        Устройства без фичи 0x8071 дают пустую структуру, а не ошибку:
        профиль с подсветкой тогда просто пропускается при применении.
        """
        if not self.has_feature(FeatureId.RGB_EFFECTS):
            return LightingInfo(supported=False)
        info = self.request(FeatureId.RGB_EFFECTS, 0x00, 0xFF, 0xFF, 0x00)
        if len(info) < 7:
            return LightingInfo(supported=False)
        _, _, zone_count, _, capabilities = struct.unpack("!BBBHH", info[0:7])
        zones: list[LightingZone] = []
        for index in range(zone_count):
            zones.append(self.lighting_zone(index))
        return LightingInfo(supported=True, capabilities=capabilities, zones=zones)

    def lighting_zone(self, index: int = 0) -> LightingZone:
        """Описание зоны: расположение и доступные на ней эффекты."""
        reply = self.request(FeatureId.RGB_EFFECTS, 0x00, index, 0xFF, 0x00)
        # Solaar читает зону той же функцией 0x00 со смещением 2:
        # [echo index, echo 0xFF, location u16 BE, count].
        location, count = struct.unpack("!HB", reply[2:5]) if len(reply) >= 5 else (0, 0)
        effects: list[LightingEffect] = []
        for slot in range(count):
            raw = self.request(FeatureId.RGB_EFFECTS, 0x00, index, slot, 0x00)
            if len(raw) < 8:
                continue
            _, __, effect_id, capabilities, period = struct.unpack("!BBHHH", raw[0:8])
            effects.append(
                LightingEffect(slot=slot, effect_id=effect_id, capabilities=capabilities, period=period)
            )
        return LightingZone(index=index, location=location, effects=effects)

    def set_lighting(
        self,
        effect_id: int,
        params: dict[str, int] | None = None,
        *,
        zone: int = 0,
        slot: int | None = None,
    ) -> int:
        """Включить эффект подсветки зоны (волатильно, fn 0x10).

        `params` — значения параметров эффекта (color/period/intensity/...).
        Если `slot` не задан, слот ищется по `effect_id` у зоны. В отличие от
        NvConfig (fn 0x30), эта команда не пишет в энергонезависимую память.
        """
        params = params or {}
        used_slot = slot
        if used_slot is None:
            zone_info = self.lighting_zone(zone)
            used_slot = next((item.slot for item in zone_info.effects if item.effect_id == effect_id), None)
            if used_slot is None:
                known = ", ".join(f"{item.effect_id:#04x}" for item in zone_info.effects) or "нет"
                raise ProtocolError(
                    f"эффект {effect_id:#04x} недоступен у зоны {zone} (доступны: {known})"
                )

        payload = _lighting_effect_bytes(used_slot, effect_id, params)
        # Префикс — номер зоны, суффикс — режим активации (0x01 у Solaar).
        self.request(FeatureId.RGB_EFFECTS, 0x10, bytes((zone,)) + payload + b"\x01")
        return used_slot

    def info(self) -> DeviceInfo:
        info = DeviceInfo(path=self.transport.path, serial=self.serial)
        try:
            info.protocol_version = self.ping()
        except (TimeoutError, ProtocolError, TransportError) as exc:
            logger.debug("ping не удался: %s", exc)
        info.name = self.get_name()
        info.kind = self.get_kind()
        info.firmware = self.get_firmware()
        return info
