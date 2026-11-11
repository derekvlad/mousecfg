"""Единственная точка, через которую проект общается с устройством.

Задача модуля — сделать невозможным «случайный» обход политики: любой
исходящий кадр сначала классифицируется (`guard`), потом пишется в журнал,
и только потом уходит в hidraw.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import guard
from .guard import DeniedCommand  # noqa: F401  (реэкспорт для удобства)
from .transport import HidrawTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentCommand:
    timestamp: float
    feature: int
    function: int
    classification: str
    name: str
    payload: bytes
    frame: bytes


class Journal:
    """Журнал отправленных команд в JSON Lines.

    Нужен для двух вещей: разбора инцидентов («а что мы вообще отправляли?»)
    и счётчика персистентных операций.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._entries: list[SentCommand] = []

    @property
    def entries(self) -> tuple[SentCommand, ...]:
        return tuple(self._entries)

    def persistent_count(self) -> int:
        return sum(1 for entry in self._entries if entry.classification != "volatile")

    def record(self, command: SentCommand) -> None:
        self._entries.append(command)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(command.timestamp, 6),
            "feature": f"{command.feature:#06x}",
            "function": f"{command.function:#04x}",
            "name": command.name,
            "classification": command.classification,
            "sent": command.payload.hex(),
            "frame": command.frame.hex(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class SafePlane:
    """Канал отправки с проверкой политики и журналированием."""

    def __init__(
        self,
        transport: HidrawTransport,
        *,
        allow_onboard_switch: bool = False,
        journal: Journal | None = None,
        dry_run: bool = False,
    ) -> None:
        self.transport = transport
        self.allow_onboard_switch = allow_onboard_switch
        self.journal = journal or Journal(None)
        self.dry_run = dry_run
        self._allowed_consented: frozenset[tuple[int, int]] = frozenset()
        # Подписи уже отмеченных персистентных операций: не повторяем один и
        # тот же warning при каждом вызове.
        self._settled_persistent: set[bytes] = set()

    def grant_consented(self, pairs: Iterable[tuple[int, int]]) -> None:
        self._allowed_consented = frozenset(self._allowed_consented | set(pairs))

    @property
    def consented(self) -> frozenset[tuple[int, int]]:
        return self._allowed_consented

    def check(self, feature: int, function: int) -> guard.Rule:
        allowed = self._allowed_consented
        if self.allow_onboard_switch:
            allowed = allowed | {p for p, rule in guard.POLICY.items() if rule.classification is guard.Classification.PERSISTENT_CONSENTED}
        return guard.check(feature, function, allowed_consented=allowed)

    def send(self, feature: int, function: int, payload: bytes, frame: bytes) -> bool:
        """Проверить политику, записать в журнал и отправить.

        Возвращает True, если кадр действительно ушёл в устройство.
        """
        rule = self.check(feature, function)
        name = guard.function_name(feature, function)
        command = SentCommand(
            timestamp=time.time(),
            feature=feature,
            function=function,
            classification=rule.classification.value,
            name=name,
            payload=payload,
            frame=frame,
        )
        self.journal.record(command)

        if rule.classification is not guard.Classification.VOLATILE:
            byte = frame[3]
            signature = bytes((frame[2], 0x80 | (byte & 0xF0) | (byte & 0x0F)))
            if signature not in self._settled_persistent:
                logger.warning(
                    "персистентная операция %s (%#06x/%#04x): %s",
                    name,
                    feature,
                    function,
                    rule.note,
                )
                self._settled_persistent.add(signature)
        else:
            logger.debug("-> %s (%#06x/%#04x) %s", name, feature, function, payload.hex())

        if self.dry_run:
            if rule.mutating:
                logger.info("[dry-run] изменяющий кадр не отправлен: %s", frame.hex())
                return False
            logger.info("[dry-run] только чтение, кадр уходит: %s", frame.hex())

        self.transport.drain()
        self.transport.send(frame)
        return True

    def send_by_index(self, feature_index: int, function: int, payload: bytes, frame: bytes, name: str) -> bool:
        """Отправка кадра по индексу фичи.

        Единственный случай, когда политика не применяется: индексы
        назначает само устройство, поэтому (feature id, function) заранее
        неизвестны. Все такие кадры приходят из безопасных
        read-only-веток перечисления фич (ROOT knock и FEATURE_SET).
        """
        self.journal.record(
            SentCommand(
                timestamp=time.time(),
                feature=feature_index,
                function=function,
                classification="by-index",
                name=name,
                payload=payload,
                frame=frame,
            )
        )
        logger.debug("-> [index %#04x] %s %s", feature_index, name, payload.hex())
        self.transport.drain()
        self.transport.send(frame)
        return True
