"""Тесты udev-правил: ошибки в них тихие (ACL просто не появляется).

Главная ловушка — условие ACTION у правил, навешивающих тег `uaccess`.
С systemd v258 ACL по этому тегу навешивает systemd-udevd, а logind лишь
шлёт change-события, чтобы udev их переприменил. Поэтому правило обязано
совпадать и на `add`, и на `change`: с `ACTION=="add"` тег ставится только
при появлении устройства, а любое последующее change-событие (активация
сессии, `udevadm trigger --subsystem-match=input`) правило уже не
затрагивает — и ACL на `/dev/input/event*` не появляется, из-за чего
перестают работать кнопки и макросы. Правила для hidraw в этом же проекте
работают именно потому, что не ограничены `add`.
"""

from __future__ import annotations

import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# Файлы правил, которые ставятся в /etc/udev/rules.d.
RULE_FILES = [
    "60-mousecfg.rules",
    "70-mousecfg-input.rules",
    "71-mousecfg-virtual.rules",
]

_SEAT_LATE_INDEX = 73  # /usr/lib/udev/rules.d/73-seat-late.rules


def _logical_lines(path: Path) -> list[str]:
    """Строки правил без комментариев, склеенные по `\\`."""
    out: list[str] = []
    buffer = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not buffer and (not line or line.startswith("#")):
            continue
        buffer = f"{buffer}{line}" if buffer else line
        if buffer.endswith("\\"):
            # Правило продолжается на следующей строке.
            buffer = buffer[:-1].rstrip() + " "
            continue
        out.append(buffer)
        buffer = ""
    if buffer:
        out.append(buffer)
    return out


def _action_condition(rule: str) -> str | None:
    """Условие ACTION в правиле, если оно есть (`==add`, `!=remove`, ...)."""
    match = re.search(r'(?:^|,)\s*ACTION\s*(==|!=)\s*"([^"]*)"', rule)
    if match is None:
        return None
    return f"{match.group(1)}{match.group(2)}"


def test_rule_files_exist():
    for name in RULE_FILES:
        assert (DATA / name).is_file(), f"нет файла правил {name}"


def test_uaccess_rule_files_sort_before_seat_late():
    """Правило с uaccess должно грузиться раньше 73-seat-late.rules.

    Иначе тег не превращается в ACL: 73-seat-late.rules — последнее место,
    где `uaccess` попадает в очередь builtin.
    """
    for name in RULE_FILES:
        assert name[:2].isdigit(), f"{name}: имя должно начинаться с номера"
        assert int(name[:2]) < _SEAT_LATE_INDEX, (
            f"{name}: файл должен сортироваться до 73-seat-late.rules"
        )


def test_uaccess_rules_apply_on_change_too():
    """Ни одно правило с TAG+="uaccess" не должно стоять только на add.

    Так делают все правила в этом проекте: и hidraw (60), и виртуальное
    устройство (71), и evdev (70). Иначе после перезагрузки ACL не
    навешивается — см. docstring модуля.
    """
    checked = 0
    for name in RULE_FILES:
        for rule in _logical_lines(DATA / name):
            if 'TAG+="uaccess"' not in rule:
                continue
            checked += 1
            condition = _action_condition(rule)
            assert condition != "==add", (
                f"{name}: правило с uaccess ограничено ACTION==\"add\" — "
                f"на systemd >= v258 ACL не появится после перезагрузки: {rule!r}"
            )
    assert checked, "не найдено ни одного правила с TAG+=\"uaccess\""


def test_input_mouse_rule_matches_expected_node():
    """Правило для evdev-узла мыши: подсистема input, event*, ID_INPUT_MOUSE."""
    rules = [
        rule
        for rule in _logical_lines(DATA / "70-mousecfg-input.rules")
        if 'TAG+="uaccess"' in rule
    ]
    assert len(rules) == 1, "ожидается ровно одно правило uaccess для evdev"
    rule = rules[0]
    assert 'SUBSYSTEM=="input"' in rule
    assert 'KERNEL=="event*"' in rule
    assert 'ENV{ID_INPUT_MOUSE}=="1"' in rule
