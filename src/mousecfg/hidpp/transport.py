"""Чтение и запись кадров через /dev/hidraw.

Зависимостей нет: pyudev нужен только для поиска узла, сам ввод-вывод —
обычные os.read/os.write с таймаутом через select.
"""

from __future__ import annotations

import logging
import os
import select
import time
from collections.abc import Callable
from pathlib import Path

from mousecfg.i18n import t

from . import protocol
from .protocol import TransportError

logger = logging.getLogger(__name__)

# Размер буфера чтения: длинный кадр 20 байт, запас на будущее.
READ_SIZE = 64


class HidrawTransport:
    """Одно открытое hidraw-устройство.

    Все записи идут через `send` — это единственная точка, где мы вообще
    касаемся устройства, и её контролирует `safeplane`.
    """

    def __init__(self, path: str | Path, *, device_index: int = protocol.USB_DEVICE_INDEX) -> None:
        self.path = str(path)
        self.device_index = device_index
        self._fd: int | None = None
        self._write_hook: Callable[[bytes], None] | None = None

    # -- жизненный цикл -------------------------------------------------
    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise TransportError(
                t(f"нет доступа к {self.path}: нужен ACL/правило udev (см. data/60-mousecfg.rules)",
                f"no access to {self.path}: an ACL/udev rule is required (see data/60-mousecfg.rules)")
            ) from exc
        except FileNotFoundError as exc:
            raise TransportError(t(f"{self.path} не существует — устройство отключено?", f"{self.path} does not exist — is the device disconnected?")) from exc

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> HidrawTransport:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def set_write_hook(self, hook: Callable[[bytes], None] | None) -> None:
        """Наблюдатель за исходящими кадрами (журнал, аудит)."""
        self._write_hook = hook

    # -- ввод-вывод -----------------------------------------------------
    def send(self, frame: bytes) -> None:
        fd = self._require_fd()
        if self._write_hook is not None:
            self._write_hook(frame)
        try:
            os.write(fd, frame)
        except OSError as exc:
            raise TransportError(t(f"запись в {self.path} не удалась: {exc}", f"write to {self.path} failed: {exc}")) from exc

    def drain(self) -> None:
        """Съесть всё, что уже висит во входном буфере."""
        while self.read(timeout=0.0) is not None:
            pass

    def read(self, timeout: float = 0.05) -> bytes | None:
        """Прочитать один кадр. None — если в пределах таймаута ничего не пришло."""
        fd = self._require_fd()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return None
        except OSError as exc:
            raise TransportError(t(f"чтение из {self.path} не удалось: {exc}", f"read from {self.path} failed: {exc}")) from exc
        if not data:
            raise TransportError(t(f"{self.path} закрылся (устройство отключено?)", f"{self.path} was closed (device disconnected?)"))
        return data

    def expect_reply(self, timeout: float = 1.0) -> bytes:
        """Дождаться HID++ кадра (report id 0x10/0x11).

        По этому же hidraw-узлу идёт поток обычных input-отчётов мыши
        (report id 0x01): на 1000 Гц это сотни кадров в секунду. Они не
        имеют отношения к HID++ и молча пропускаются — иначе они бы
        «съедали» окно ожидания.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(t(f"устройство {self.path} не ответило за {timeout:.2f} с", f"device {self.path} did not respond within {timeout:.2f} s"))
            data = self.read(timeout=min(remaining, 0.25))
            if data is None:
                continue
            if data[0] in (protocol.SHORT_REPORT_ID, protocol.LONG_REPORT_ID):
                return data
            logger.debug(t("input-отчёт (%#04x байт) пропущен", "input report (%#04x byte) skipped"), data[0])

    def _require_fd(self) -> int:
        if self._fd is None:
            raise TransportError(t(f"{self.path} не открыт", f"{self.path} is not open"))
        return self._fd


def frame_hex(frame: bytes) -> str:
    return " ".join(f"{byte:02x}" for byte in frame)
