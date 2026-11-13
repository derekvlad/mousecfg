"""Ввод-вывод вендорского канала через /dev/hidraw.

Свойства канала: 64-байтные отчёты без report id, ответ приходит тем же
узлом, посторонних отчётов на этом интерфейсе нет (в отличие от HID++-узла,
куда льётся движение мыши). Отсюда простой обмен «запрос — ответ».
"""

from __future__ import annotations

import logging
import os
import select
import time
from collections.abc import Callable
from pathlib import Path

from mousecfg.i18n import t

logger = logging.getLogger(__name__)

READ_SIZE = 64
DEFAULT_TIMEOUT = 0.8


class VendorTransportError(OSError):
    """Узел вендорского канала недоступен или оборвался."""


class VendorTransport:
    """Одно открытое hidraw-устройство. Пишет только то, что дал `device`."""

    def __init__(self, path: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = str(path)
        self.timeout = timeout
        self._fd: int | None = None

    # -- жизненный цикл -------------------------------------------------
    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise VendorTransportError(
                t(
                    f"нет доступа к {self.path}: нужно правило udev (data/60-mousecfg.rules) "
                    "и повторное `sudo udevadm trigger --subsystem-match=hidraw`",
                    f"no access to {self.path}: a udev rule is required (data/60-mousecfg.rules) "
                    "plus `sudo udevadm trigger --subsystem-match=hidraw`",
                )
            ) from exc
        except FileNotFoundError as exc:
            raise VendorTransportError(
                t(f"{self.path} не существует — устройство отключено?", f"{self.path} does not exist — is the device disconnected?")
            ) from exc

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> VendorTransport:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    # -- ввод-вывод -----------------------------------------------------
    def send(self, frame: bytes) -> None:
        fd = self._require_fd()
        try:
            os.write(fd, frame)
        except OSError as exc:
            raise VendorTransportError(t(f"запись в {self.path} не удалась: {exc}", f"write to {self.path} failed: {exc}")) from exc

    def read(self, timeout: float | None = None) -> bytes | None:
        """Прочитать один отчёт. None — в пределах таймаута ничего не пришло."""
        fd = self._require_fd()
        ready, _, _ = select.select([fd], [], [], self.timeout if timeout is None else timeout)
        if not ready:
            return None
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return None
        except OSError as exc:
            raise VendorTransportError(t(f"чтение из {self.path} не удалось: {exc}", f"read from {self.path} failed: {exc}")) from exc
        if not data:
            raise VendorTransportError(t(f"{self.path} закрылся (устройство отключено?)", f"{self.path} was closed (device disconnected?)"))
        return data

    def drain(self) -> None:
        """Съесть всё, что уже висит во входном буфере."""
        while self.read(timeout=0.0) is not None:
            pass

    def exchange(self, frame: bytes, accept: Callable[[bytes], object | None]) -> object | None:
        """Отправить кадр и дождаться ответа, который примет `accept`.

        Ждать до таймаута: на канале могут оказаться чужие/несозревшие
        отчёты, их молча пропускаем.
        """
        self.drain()
        self.send(frame)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            data = self.read(timeout=min(remaining, 0.1))
            if data is None:
                continue
            accepted = accept(data)
            if accepted is not None:
                return accepted
            logger.debug(t("кадр вендорского канала пропущен: %s", "vendor frame skipped: %s"), data[:8].hex())

    def _require_fd(self) -> int:
        if self._fd is None:
            raise VendorTransportError(t(f"{self.path} не открыт", f"{self.path} is not open"))
        return self._fd
