"""Политика вендорского канала: разрешено только чтение.

У YJX-мышей запись конфигурации сразу уходит в onboard-флеш, а команда
`0d 00` с непустым телом вообще стирает участок памяти. Поэтому в пакете
нет «универсального отправителя»: перед каждым кадром проверяется, что это
одна из считанных read-команд. Всё остальное отклоняется до `os.write`.
"""

from __future__ import annotations

from enum import StrEnum

from mousecfg.i18n import t


class Dialect(StrEnum):
    """Семейство кадров, которое слушает конкретная прошивка."""

    MKESPN = "mkespn"
    YJX = "yjx"


class VendorRefused(RuntimeError):
    """Кадр не прошёл политику: в вендорский канал можно только читать."""


# Разрешённые чтения MKESPN: (cmd, sub) → что возвращает.
MKESPN_READS: dict[tuple[int, int], str] = {
    (0x03, 0x00): "идентификация",
    (0x08, 0xA5): "страница режимов",
    (0x0E, 0xA5): "DPI / частота / подсветка",
    (0x30, 0xA5): "состояние сессии",
    (0xFA, 0xAE): "идентификатор устройства",
}

# Команды MKESPN, которые пишут в память. Держим отдельно, чтобы отказ был
# не «неизвестная команда», а внятным: именно эти кадры трогают флеш.
MKESPN_WRITES: dict[int, str] = {
    0x09: "запись страницы режимов",
    0x0D: "запись раскладки кнопок (с непустым телом стирает память)",
    0x0F: "запись DPI / частоты / подсветки",
    0x10: "маркер перед записью режимов",
    0x22: "вспомогательная запись подсветки",
    0x23: "вспомогательная запись подсветки",
}

# Разрешённые чтения YJX (SDK): cmd → что возвращает. Байт 2 у адресных
# кадров — тип устройства, он на политику не влияет.
YJX_READS: dict[int, str] = {
    0x04: "ping / версия прошивки",
    0x08: "чтение памяти (короткое)",
    0x0E: "чтение памяти (повтор)",
    0x30: "заряд и USB",
    0xED: "состояние сна",
}

# Команды YJX, которые пишут.
YJX_WRITES: dict[int, str] = {
    0x09: "запись раскладки кнопок",
    0x0D: "запись макроса",
    0x0F: "запись блока памяти",
    0x10: "фиксация макроса",
    0x22: "вспомогательная запись",
    0x23: "вспомогательная запись",
    0x52: "рукопожатие (меняет состояние сессии)",
}


def ensure_read(dialect: Dialect, cmd: int, sub: int = 0) -> str:
    """Пропустить только read-команду. Вернуть её назначение.

    Для MKESPN проверяется пара `(cmd, sub)`, для YJX — только `cmd`:
    у адресных кадров второй байт занят типом устройства.
    """
    if dialect is Dialect.MKESPN:
        writes = MKESPN_WRITES
        reads = MKESPN_READS
        key = (cmd, sub)
    else:
        writes = YJX_WRITES
        reads = YJX_READS
        key = cmd

    purpose = reads.get(key)
    if purpose is not None:
        return purpose

    if dialect is Dialect.MKESPN and cmd == 0x0D and sub == 0x00:
        raise VendorRefused(
            t(
                "команда 0d/00 — запись раскладки; с непустым телом она стирает участок "
                "onboard-памяти. Отправка отменена.",
                "command 0d/00 writes the keymap; with a non-empty body it clears a range of "
                "onboard memory. The send was cancelled.",
            )
        )
    if cmd in writes:
        raise VendorRefused(
            t(
                f"команда {cmd:02x}/{sub:02x} — это {writes[cmd]}: она пишет в память мыши, "
                "а проект в onboard-флеш не пишет никогда. Отправка отменена.",
                f"command {cmd:02x}/{sub:02x} is {writes[cmd]}: it writes to the mouse memory, "
                "and the project never writes to onboard flash. The send was cancelled.",
            )
        )
    raise VendorRefused(
        t(
            f"команда {cmd:02x}/{sub:02x} не входит в список разрешённых чтений. "
            "В вендорский канал можно только читать.",
            f"command {cmd:02x}/{sub:02x} is not in the read-only allowlist. "
            "The vendor channel is read-only.",
        )
    )


# --- запись в onboard-память: только по явному согласию -----------------
#
# Эти команды действительно меняют энергонезависимую память мыши. Проект
# отправляет их только по явной команде (`mousecfg onboard ...`) и только
# после того, как снят снимок. Ниже — узкий allowlist, который шире
# read-only запрета ровно на то, что нужно для чтения и правки конфигурации.
MKESPN_CONSENTED_WRITES: dict[int, str] = {
    0x0F: "запись DPI / частоты / подсветки",
    0x09: "запись раскладки кнопок",
}


class VendorWriteRefused(VendorRefused):
    """Запись не прошла политику (нет согласия или команда вне allowlist)."""


def ensure_onboard_write(cmd: int, sub: int) -> str:
    """Пропустить write-команду во время явной операции записи.

    Разрешены только команды, которыми правится конфигурация. Отдельно и
    с особым текстом запрещена `0d/00`: с непустым телом она стирает
    участок памяти, а не пишет запись.
    """
    purpose = MKESPN_CONSENTED_WRITES.get(cmd)
    if purpose is not None:
        return purpose
    if cmd == 0x0D and sub == 0x00:
        raise VendorWriteRefused(
            t(
                "команда 0d/00 с непустым телом стирает участок onboard-памяти. "
                "Проект её не отправляет даже при разрешённой записи.",
                "command 0d/00 with a non-empty body clears a range of onboard memory. "
                "The project never sends it, even when writing is allowed.",
            )
        )
    raise VendorWriteRefused(
        t(
            f"команда {cmd:02x}/{sub:02x} не входит в список разрешённых записей.",
            f"command {cmd:02x}/{sub:02x} is not in the write allowlist.",
        )
    )
