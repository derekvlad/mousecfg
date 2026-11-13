"""Кадры двух диалектов YJX-контроллера.

Разные прошивки на одном и том же USB ID говорят по-разному:

* `mkespn` — 64-байтный кадр `55 cmd sub chk len off_lo off_hi page data…`,
  ответ начинается с `aa`. Так описана мышь W12 (VID a8a4).
* `yjx` — 64-байтный кадр `f0 cmd …` с адресами памяти; так работает SDK
  для AJ159/AJ139 (VID a8a4 и a8a5).

Какой из них слушает наша мышь, выясняется пробой. Здесь только сборка
запросов и разбор ответов — отправка и политика лежат в `device.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

FRAME_SIZE = 64

MKESPN_REQUEST_MARKER = 0x55
MKESPN_REPLY_MARKER = 0xAA
MKESPN_HEADER_SIZE = 8

YJX_MARKER = 0xF0
YJX_MOUSE = 0x01
# Базовый адрес области чтения из SDK (READ_ADDRESS).
YJX_READ_ADDRESS = 0x0BA5


class FrameError(ValueError):
    """Кадр не помещается в 64 байта или разобран неверно."""


def mkespn_frame(
    cmd: int,
    sub: int = 0,
    *,
    length: int = 0,
    offset: int = 0,
    page: int = 0,
    data: bytes = b"",
) -> bytes:
    """Собрать запрос MKESPN. `chk` не проверяется прошивкой — пишем 0."""
    if len(data) > FRAME_SIZE - MKESPN_HEADER_SIZE:
        raise FrameError(f"payload is too long: {len(data)} bytes")
    frame = bytearray(FRAME_SIZE)
    frame[0] = MKESPN_REQUEST_MARKER
    frame[1] = cmd & 0xFF
    frame[2] = sub & 0xFF
    frame[3] = 0
    frame[4] = (length or len(data)) & 0xFF
    frame[5] = offset & 0xFF
    frame[6] = (offset >> 8) & 0xFF
    frame[7] = page & 0xFF
    if data:
        frame[MKESPN_HEADER_SIZE : MKESPN_HEADER_SIZE + len(data)] = data
    return bytes(frame)


@dataclass(frozen=True)
class MkespnReply:
    cmd: int
    sub: int
    page: int
    data: bytes
    raw: bytes


def parse_mkespn_reply(frame: bytes) -> MkespnReply | None:
    """Разобрать ответ `aa …`. None — это не ответ вендорского канала."""
    if len(frame) < MKESPN_HEADER_SIZE or frame[0] != MKESPN_REPLY_MARKER:
        return None
    length = frame[4]
    if length:
        data = frame[MKESPN_HEADER_SIZE : MKESPN_HEADER_SIZE + length]
    else:
        data = frame[MKESPN_HEADER_SIZE:].rstrip(b"\x00")
    return MkespnReply(cmd=frame[1], sub=frame[2], page=frame[7], data=data, raw=frame)


# Значение поля 3 в кадрах записи конфигурации. Прошивка его не
# проверяет, но веб-инструмент AJAZZ всегда ставит 0x0b — повторяем,
# чтобы кадр был побайтно таким же, как у штатного инструмента.
CONFIG_WRITE_MARKER = 0x0B
# Классический заголовок кадра записи config (0f/ae): marker, len, 1, 1, 1.
CONFIG_WRITE_PREFIX = bytes([CONFIG_WRITE_MARKER, 0x30, 0x01, 0x01, 0x01])


def mkespn_config_write_frame(config: bytes) -> bytes:
    """Собрать кадр записи `0f/ae` из 48-байтной конфигурации.

    Раскладка совпадает с веб-инструментом: `55 0f ae <marker> 30 01 01 01`
    и далее данные конфигурации. Прямой записи в память здесь нет — кадр
    только собирается; отправляет его `device` после проверки политики.
    """
    if not config:
        raise FrameError("config is empty")
    # Чтение отдаёт 47 байт (последний, 48-й, скрыт), а запись всегда
    # 48-байтная: недостающий байт дополняем нулём, как это делает
    # веб-инструмент для моделей без compose-поля в этом байте.
    body = bytearray(config[: FRAME_SIZE - 8])
    if len(body) < 0x30:
        body += bytes(0x30 - len(body))
    frame = bytearray(FRAME_SIZE)
    frame[0] = MKESPN_REQUEST_MARKER
    frame[1] = 0x0F
    frame[2] = 0xAE
    frame[3 : 3 + len(CONFIG_WRITE_PREFIX)] = CONFIG_WRITE_PREFIX
    frame[8 : 8 + len(body)] = body
    return bytes(frame)


def mkespn_keys_write_frame(keys: bytes) -> bytes:
    """Собрать кадр записи раскладки кнопок `09/a5` (34 = 0x22).

    В байтах 5..8 — `0x20 00 00 00` (как у инструмента), затем записи
    кнопок по 4 байта, затем фиксированные RGB/служебные поля.
    """
    frame = bytearray(FRAME_SIZE)
    frame[0] = MKESPN_REQUEST_MARKER
    frame[1] = 0x09
    frame[2] = 0xA5
    frame[3] = 0xA5
    frame[4] = 0x22
    frame[5:9] = bytes([0x20, 0x00, 0x00, 0x00])
    frame[9 : 9 + len(keys)] = keys[: FRAME_SIZE - 9]
    return bytes(frame)


def yjx_packet(cmd: int, payload: bytes = b"") -> bytes:
    """Короткий пакет SDK: `f0 cmd payload…`."""
    if len(payload) > FRAME_SIZE - 2:
        raise FrameError(f"payload is too long: {len(payload)} bytes")
    packet = bytearray(FRAME_SIZE)
    packet[0] = YJX_MARKER
    packet[1] = cmd & 0xFF
    packet[2 : 2 + len(payload)] = payload
    return bytes(packet)


def yjx_addressed_packet(
    cmd: int,
    address: int,
    offset: int,
    *,
    payload: bytes = b"",
    frame_profile: int = 0,
    device_type: int = YJX_MOUSE,
) -> bytes:
    """Адресный пакет SDK: `f0 cmd type cs len addr_lo addr_hi frame data…`.

    `offset` (он же `len` в SDK) — смещение внутри области `address`.
    """
    if len(payload) > FRAME_SIZE - 8:
        raise FrameError(f"payload is too long: {len(payload)} bytes")
    packet = bytearray(FRAME_SIZE)
    packet[0] = YJX_MARKER
    packet[1] = cmd & 0xFF
    packet[2] = device_type & 0xFF
    packet[4] = offset & 0xFF
    packet[5] = address & 0xFF
    packet[6] = (address >> 8) & 0xFF
    packet[7] = frame_profile & 0xFF
    if payload:
        packet[8 : 8 + len(payload)] = payload
    packet[3] = sum(packet[4:]) & 0xFF
    return bytes(packet)


def normalize_yjx_reply(frame: bytes) -> bytes:
    """Убрать маркер `f0`, если он пришёл в данных отчёта."""
    if frame and frame[0] == YJX_MARKER:
        return bytes(frame[1:])
    return bytes(frame)


def printable_text(data: bytes) -> str:
    """Оставить только видимый ASCII — для имени модели и версии прошивки."""
    return "".join(chr(byte) if 32 <= byte < 127 else "" for byte in data).strip()


def hex_bytes(data: bytes) -> str:
    return " ".join(f"{byte:02x}" for byte in data)
