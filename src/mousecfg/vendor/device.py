"""Чтение вендорского канала YJX: определение диалекта и разведочные дампы.

Здесь нет ни одного кадра записи: перед отправкой каждый запрос проходит
`guard.ensure_read()`, а самих запросов ровно столько, сколько перечислено
в read-allowlist. Назначение модуля — честно показать, что мышь отвечает,
и отдать сырые данные для документации; декодировать наугад мы не пытаемся.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mousecfg.i18n import t

from . import framing
from .framing import MkespnReply
from .guard import Dialect, VendorRefused, ensure_onboard_write, ensure_read
from .parsers import (
    DeviceIdentification,
    DpiConfig,
    InfoBlock,
    SessionState,
    parse_dpi_config,
    parse_identification,
    parse_info_block,
    parse_session_state,
)
from .transport import VendorTransport, VendorTransportError

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


class VendorError(RuntimeError):
    """Вендорский канал не ответил или ответ не распознан."""


@dataclass
class VendorResponse:
    label: str
    request: str
    reply: str
    text: str = ""


@dataclass
class VendorProbe:
    path: str
    dialect: str
    identification: str = ""
    version: str = ""
    device: DeviceIdentification | None = None
    dpi: DpiConfig | None = None
    session: SessionState | None = None
    info: InfoBlock | None = None
    responses: list[VendorResponse] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def decoded(self) -> dict[str, object]:
        """Только расшифрованные поля — для `get` и машинного вывода."""
        data: dict[str, object] = {"dialect": self.dialect}
        if self.device is not None:
            data["model"] = self.device.model
            data["code"] = self.device.code
            data["firmware_dates"] = self.device.firmware_dates
        if self.dpi is not None:
            data["dpi"] = self.dpi.current_dpi
            data["dpi_levels"] = self.dpi.levels
            data["report_rate_hz"] = self.dpi.poll_rate_hz
        if self.session is not None and self.session.percent is not None:
            data["battery_percent"] = self.session.percent
        return data

    def read_battery_percent(self) -> int | None:
        """Проценты заряда (запрос только чтения `30/a5`)."""
        if self.session is None:
            return None
        return self.session.percent


class VendorDevice:
    """Одна мышь на вендорском интерфейсе. Только чтение."""

    def __init__(self, path: str, *, timeout: float = 0.8) -> None:
        self.transport = VendorTransport(path, timeout=timeout)
        self.path = self.transport.path

    def __enter__(self) -> VendorDevice:
        self.transport.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.transport.close()

    # -- диалект --------------------------------------------------------
    def detect_dialect(self) -> Dialect | None:
        """Какой из двух диалектов слушает прошивка."""
        if self._mkespn_query(0x03, 0x00) is not None:
            return Dialect.MKESPN
        if self._yjx_read(framing.yjx_packet(0x04), accept=0x04) is not None:
            return Dialect.YJX
        return None

    # -- разведка -------------------------------------------------------
    def probe(self, *, deep: bool = False) -> VendorProbe:
        """Определить диалект и, если `deep`, снять read-only дампы."""
        dialect = self.detect_dialect()
        probe = VendorProbe(path=self.path, dialect=dialect.value if dialect else "unknown")
        if dialect is None:
            probe.notes.append(
                t(
                    "ни один из известных диалектов не ответил; значит канал другой",
                    "neither of the known dialects answered; the channel must be different",
                )
            )
            return probe

        if dialect is Dialect.MKESPN:
            self._probe_mkespn(probe, deep=deep)
        else:
            self._probe_yjx(probe, deep=deep)

        if probe.identification:
            match = _VERSION_RE.search(probe.identification)
            if match:
                probe.version = match.group(0)
        probe.notes.append(
            t(
                "прочитано только чтением; запись в этом канале проект не выполняет",
                "read-only: the project never writes in this channel",
            )
        )
        return probe

    def _probe_mkespn(self, probe: VendorProbe, *, deep: bool) -> None:
        identify = self._mkespn_query(0x03, 0x00)
        if identify is not None:
            probe.identification = framing.printable_text(identify.data)
            probe.device = parse_identification(probe.identification)
            self._remember(probe, "идентификация", framing.mkespn_frame(0x03, 0x00), identify.raw, identify.data)

        config = self._mkespn_query(0x0E, 0xA5, length=0x30)
        if config is not None:
            probe.dpi = parse_dpi_config(config)
            self._remember(
                probe,
                t("DPI / частота / подсветка", "DPI / report rate / lighting"),
                framing.mkespn_frame(0x0E, 0xA5, length=0x30),
                config.raw,
                config.data,
            )

        battery = self._mkespn_query(0x30, 0xA5, length=0x0A)
        if battery is not None:
            probe.session = parse_session_state(battery)
            self._remember(
                probe,
                t("заряд и USB", "battery and USB"),
                framing.mkespn_frame(0x30, 0xA5, length=0x0A),
                battery.raw,
                battery.data,
            )

        if not deep:
            return
        reads: list[tuple[str, int, int, int, int]] = [
            (t("идентификатор устройства", "device identifier"), 0xFA, 0xAE, 0, 8),
            (t("режимы, страница 0", "modes, page 0"), 0x08, 0xA5, 0, 0x30),
        ]
        for label, cmd, sub, page, length in reads:
            reply = self._mkespn_query(cmd, sub, page=page, length=length)
            request = framing.mkespn_frame(cmd, sub, page=page, length=length)
            if reply is None:
                probe.notes.append(t(f"{label}: нет ответа", f"{label}: no reply"))
                continue
            self._remember(probe, label, request, reply.raw, reply.data)

        self._read_info_block(probe)

    def _read_info_block(self, probe: VendorProbe) -> None:
        """Заводские коды на страницах 5..6 (`08/a5`) — только чтение."""
        pages: dict[int, bytes] = {}
        for page in (5, 6):
            reply = self._mkespn_query(0x08, 0xA5, page=page, length=0x30)
            if reply is None:
                continue
            pages[page] = bytes(reply.data)
        if not pages:
            return
        probe.info = parse_info_block(pages)
        self._remember(
            probe,
            t("заводские коды, страницы 5..6", "factory codes, pages 5..6"),
            framing.mkespn_frame(0x08, 0xA5, page=5, length=0x30),
            b"",
            b"".join(pages[page] for page in sorted(pages)),
        )

    def _probe_yjx(self, probe: VendorProbe, *, deep: bool) -> None:
        ping = self._yjx_read(framing.yjx_packet(0x04), accept=0x04)
        if ping is not None:
            probe.identification = framing.printable_text(framing.normalize_yjx_reply(ping))
            self._remember(probe, "ping / версия", framing.yjx_packet(0x04), ping, framing.normalize_yjx_reply(ping))
        if not deep:
            return
        reads: list[tuple[str, bytes, tuple[int, ...]]] = [
            ("заряд и USB", framing.yjx_packet(0x30, bytes([0x01, 0, 0, 0, 0, 0])), (0x30,)),
            ("конфигурация", framing.yjx_addressed_packet(0x08, 0x0BA5, 0x2C), (0x08, 0x0E)),
            ("данные устройства", framing.yjx_addressed_packet(0x30, 0x0BA5, 0x2E), (0x30, 0x0E)),
            ("профиль", framing.yjx_addressed_packet(0x0E, 0x0BA5, 0x2E), (0x0E, 0x08)),
        ]
        for label, request, accept in reads:
            reply = self._yjx_read(request, accept=accept)
            if reply is None:
                probe.notes.append(t(f"{label}: нет ответа", f"{label}: no reply"))
                continue
            self._remember(probe, label, request, reply, framing.normalize_yjx_reply(reply))

    # -- низкий уровень -------------------------------------------------
    def _remember(self, probe: VendorProbe, label: str, request: bytes, reply: bytes, data: bytes) -> None:
        probe.responses.append(
            VendorResponse(
                label=label,
                request=framing.hex_bytes(request),
                reply=framing.hex_bytes(reply),
                text=framing.printable_text(data),
            )
        )

    def _mkespn_query(self, cmd: int, sub: int, *, page: int = 0, length: int = 0) -> MkespnReply | None:
        ensure_read(Dialect.MKESPN, cmd, sub)
        request = framing.mkespn_frame(cmd, sub, page=page, length=length)

        def accept(frame: bytes) -> MkespnReply | None:
            reply = framing.parse_mkespn_reply(frame)
            if reply is None or reply.cmd != cmd or reply.sub != sub:
                return None
            return reply

        return self.transport.exchange(request, accept)  # type: ignore[return-value]

    def _yjx_read(self, request: bytes, *, accept: int | tuple[int, ...]) -> bytes | None:
        command = request[1]
        ensure_read(Dialect.YJX, command, request[2] if len(request) > 2 else 0)
        accepted = (accept,) if isinstance(accept, int) else accept

        def matches(frame: bytes) -> bytes | None:
            view = framing.normalize_yjx_reply(frame)
            if view and view[0] in accepted:
                return view
            return None

        return self.transport.exchange(request, matches)  # type: ignore[return-value]

    # -- чтение/запись конфигурации (запись — только по согласию) --------
    def read_config(self) -> MkespnReply | None:
        """Прочитать 48-байтную конфигурацию (`0e/a5`)."""
        return self._mkespn_query(0x0E, 0xA5, length=0x30)

    def read_keys(self) -> MkespnReply | None:
        """Прочитать раскладку кнопок (`08/a5`). Может вернуть `ff`."""
        return self._mkespn_query(0x08, 0xA5, length=0x20)

    def write_config(self, config: bytes, *, dry_run: bool = False) -> bytes | None:
        """Записать конфигурацию в память мыши. Только по явному согласию.

        `dry_run` не отправляет кадр вообще. Перед отправкой команда проходит
        `guard.ensure_onboard_write`. После записи вызывающий сам снимает
        контрольное чтение.
        """
        frame = framing.mkespn_config_write_frame(config)
        ensure_onboard_write(frame[1], frame[2])
        if dry_run:
            return None
        return self._write_and_wait(frame, expect_cmd=0x0F)

    def write_keys(self, keys: bytes, *, dry_run: bool = False) -> bytes | None:
        """Записать раскладку кнопок (`09/a5`). Только по явному согласию."""
        frame = framing.mkespn_keys_write_frame(keys)
        ensure_onboard_write(frame[1], frame[2])
        if dry_run:
            return None
        return self._write_and_wait(frame, expect_cmd=0x09)

    def _write_and_wait(self, frame: bytes, *, expect_cmd: int) -> bytes | None:
        def accept(raw: bytes) -> bytes | None:
            reply = framing.parse_mkespn_reply(raw)
            if reply is not None and reply.cmd == expect_cmd:
                return reply.raw
            return None

        return self.transport.exchange(frame, accept)  # type: ignore[return-value]


def read_battery_percent(path: str, *, timeout: float = 0.8) -> int | None:
    """Прочитать только заряд (один кадр `30/a5`).

    Отдельная функция для демона: не тянет за собой полную разведку и
    подходит для периодического опроса. Ничего не пишет и не бросает
    исключений при недоступном узле — просто возвращает None.
    """
    try:
        with VendorDevice(path, timeout=timeout) as device:
            dialect = device.detect_dialect()
            if dialect is not Dialect.MKESPN:
                return None
            reply = device._mkespn_query(0x30, 0xA5, length=0x0A)  # noqa: SLF001
    except (VendorError, OSError):
        return None
    if reply is None:
        return None
    return parse_session_state(reply).percent


__all__ = [
    "Dialect",
    "VendorDevice",
    "VendorError",
    "VendorProbe",
    "VendorRefused",
    "VendorResponse",
    "VendorTransportError",
    "read_battery_percent",
]
