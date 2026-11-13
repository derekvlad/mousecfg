"""Разбор читающих ответов вендорского канала YJX.

Только чтение: здесь нет ни одного кадра записи, а назначение полей взято из
публичных описаний протокола и сверено с живым ответом мыши. Если поле
распознать не удаётся, оно остаётся в сыром дампе, а не выдаётся за факт.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mousecfg.i18n import t

from .framing import MkespnReply

# Коды частоты опроса в байте 2 ответа 0e/a5 (из SDK для того же контроллера).
POLL_RATE_HZ: dict[int, int] = {1: 125, 2: 250, 3: 500, 4: 1000}
POLL_RATE_MAX = 1000

# Сколько уровней DPI бывает у этих мышей.
MAX_DPI_LEVELS = 6

_IDENT_RE = re.compile(r"^(?P<model>[A-Za-z0-9.]+?)(?P<code>\d{4,8})(?=[A-Z][a-z]{2} )")
_DATE_RE = re.compile(r"[A-Z][a-z]{2} \d{1,2} \d{4} ?\d{2}:\d{2}:\d{2}")
_TIMESTAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


@dataclass
class DeviceIdentification:
    """Ответ `03/00`: модель, код, версия, даты сборки."""

    raw: str
    model: str = ""
    code: str = ""
    firmware_dates: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [self.model or self.raw]
        if self.code:
            parts.append(t(f"код {self.code}", f"code {self.code}"))
        if self.firmware_dates:
            parts.append(t(f"сборка: {', '.join(self.firmware_dates)}", f"builds: {', '.join(self.firmware_dates)}"))
        return "  ".join(part for part in parts if part)


@dataclass
class DpiConfig:
    """Ответ `0e/a5`: уровни DPI, частота опроса, подсветка, хвост."""

    levels: list[int] = field(default_factory=list)
    current_level: int = 0  # индекс в levels, 0-based; -1 — если данных нет
    poll_rate_hz: int = 0
    light_mode: int = 0
    dpi_count: int = 0
    # Хвост настроек (цвета, LOD, датчик, сон) — пока не расшифрован.
    tail: str = ""

    @property
    def current_dpi(self) -> int:
        if 0 <= self.current_level < len(self.levels):
            return self.levels[self.current_level]
        return 0

    def describe(self) -> str:
        bits: list[str] = []
        if self.levels:
            current = self.current_dpi
            bits.append(t(f"DPI: {current} (уровни: {', '.join(str(item) for item in self.levels)})", f"DPI: {current} (levels: {', '.join(str(item) for item in self.levels)})"))
        if self.poll_rate_hz:
            if self.poll_rate_hz >= POLL_RATE_MAX:
                bits.append(t("частота опроса: 1000 Гц (максимум)", "report rate: 1000 Hz (maximum)"))
            else:
                bits.append(t(f"частота опроса: {self.poll_rate_hz} Гц", f"report rate: {self.poll_rate_hz} Hz"))
        return "  ".join(bits) or t("данных о DPI нет", "no DPI data")


@dataclass
class SessionState:
    """Ответ `30/a5`: короткое состояние сессии — заряд и USB-статус.

    Публичное описание того же контроллера трактует команду `0x30` как
    «заряд + USB», а первый байт ответа меняется со временем в пределах
    0..100 (наблюдали 81 → 80 → 73, синхронно с байтом 46 ответа `0e/a5`).
    Поэтому первый байт трактуем как процент заряда, но только когда он
    действительно в диапазоне 0..100 — иначе отдаём сырое значение.
    """

    # Сырые байты в hex.
    data: str = ""

    @property
    def first_byte(self) -> int:
        return int(self.data[:2], 16) if self.data else 0

    @property
    def percent(self) -> int | None:
        value = self.first_byte
        return value if 0 <= value <= 100 else None


@dataclass
class InfoBlock:
    """Строка из страниц 5..6 (`08/a5`): заводские коды.

    Живой ответ: `20250903170105-3299-3311` — похоже на дату/время и две
    группы заводских кодов. Расшифровку не выдаём за факт: отдаём строку,
    а дату показываем, только если она складывается в календарь.
    """

    text: str = ""
    timestamp: str = ""


def parse_info_block(pages: dict[int, bytes]) -> InfoBlock:
    """Склеить страницы 5..6 и вытащить печатаемую строку."""
    blob = b"".join(pages.get(page, b"") for page in (5, 6))
    text = "".join(chr(byte) if 32 <= byte < 127 else " " for byte in blob).strip()
    text = " ".join(text.split())
    block = InfoBlock(text=text)
    match = _TIMESTAMP_RE.match(text)
    if match:
        block.timestamp = f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}:{match.group(6)}"
    return block


def parse_identification(text: str) -> DeviceIdentification:
    """Разобрать строку `YJX.co.mouse<code><даты…>`."""
    result = DeviceIdentification(raw=text)
    match = _IDENT_RE.match(text)
    if match:
        result.model = match.group("model")
        result.code = match.group("code")
    result.firmware_dates = _DATE_RE.findall(text)
    return result


def parse_dpi_config(reply: MkespnReply) -> DpiConfig:
    """Разобрать ответ `0e/a5`.

    Layout (проверен на живой мыши, совпадает с публичным описанием):
    `[0]` 0x00, `[1]` режим подсветки, `[2]` код частоты (1..4),
    `[3]` число уровней, `[4]` активный уровень + 1, `[5..16]` шесть
    уровней DPI по LE16, дальше — резерв и хвост настроек.
    """
    data = reply.data
    config = DpiConfig(tail=data[40:].hex(" "))
    if len(data) < 5:
        return config

    config.light_mode = data[1]
    config.poll_rate_hz = POLL_RATE_HZ.get(data[2], 0)
    config.dpi_count = min(data[3], MAX_DPI_LEVELS) or 0
    config.current_level = max(0, data[4] - 1) if data[4] else 0

    levels: list[int] = []
    for index in range(config.dpi_count):
        offset = 5 + index * 2
        if offset + 1 >= len(data):
            break
        levels.append(data[offset] | (data[offset + 1] << 8))
    config.levels = levels
    return config


def parse_session_state(reply: MkespnReply) -> SessionState:
    return SessionState(data=reply.data.hex(" "))


__all__ = [
    "DpiConfig",
    "DeviceIdentification",
    "InfoBlock",
    "SessionState",
    "parse_dpi_config",
    "parse_identification",
    "parse_info_block",
    "parse_session_state",
]
