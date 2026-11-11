#!/usr/bin/env python3
"""Проверка «во флеш ничего не пишется» на живом устройстве.

Два режима:

1. По журналу проекта (по умолчанию, root не нужен):

       python3 scripts/capture-hid.py -- python3 -m mousecfg set-dpi 1600

   Запускает команду и разбирает всё, что за это время ушло на устройство.
   Если среди кадров есть персистентные (кроме явно разрешённого перехода
   в Host mode) — скрипт завершается с ненулевым кодом.

2. Через usbmon (нужен root и tcpdump): полный дамп USB-трафика для
   независимой проверки.

       sudo python3 scripts/capture-hid.py --usbmon capture.pcap -- python3 -m mousecfg set-dpi 1600
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mousecfg import config  # noqa: E402

CONSENTED = {"persistent-consented"}


def _journal_offset() -> int:
    path = config.journal_file()
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _new_entries(offset: int) -> list[dict]:
    import json

    path = config.journal_file()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[offset:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _child_env() -> dict[str, str]:
    """Дочерняя команда должна видеть наш пакет, даже если он не установлен."""
    import os

    env = dict(os.environ)
    src = str(Path(__file__).resolve().parent.parent / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}:{existing}" if existing else src
    return env


def _run_with_usbmon(args: argparse.Namespace, command: list[str]) -> int:
    if shutil.which("tcpdump") is None:
        print("нужен tcpdump (или используйте режим по умолчанию)", file=sys.stderr)
        return 1
    interfaces = sorted(Path("/sys/class/net").glob("usbmon*"))
    if not interfaces:
        print("usbmon не загружен: sudo modprobe usbmon", file=sys.stderr)
        return 1
    interface = interfaces[0].name
    print(f"дамп {interface} -> {args.usbmon}", file=sys.stderr)
    dump = subprocess.Popen(["tcpdump", "-i", interface, "-w", args.usbmon, "-U"])
    time.sleep(1.0)
    try:
        code = subprocess.call(command, env=_child_env())
    finally:
        dump.terminate()
        dump.wait(timeout=5)
    print(f"дамп сохранён: {args.usbmon}", file=sys.stderr)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usbmon", metavar="PCAP", help="писать полный дамп USB-трафика через tcpdump")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="команда после --")
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("укажите команду после --", file=sys.stderr)
        return 2

    if args.usbmon:
        return _run_with_usbmon(args, command)

    offset = _journal_offset()
    code = subprocess.call(command, env=_child_env())
    entries = _new_entries(offset)
    if code != 0:
        print(f"команда завершилась с кодом {code}, проверяем всё равно", file=sys.stderr)
    _print_report(entries)
    return code


def _print_report(entries: list[dict]) -> None:
    print(f"\nКоманда отправила {len(entries)} кадров HID++.")
    persistent = [entry for entry in entries if entry.get("classification") not in {"volatile", "by-index"}]
    if not persistent:
        print("Персистентных операций: 0 — во встроенную память мыши ничего не записано.")
        return
    print("Персистентные операции:")
    for entry in persistent:
        print(f"  {entry['name']} ({entry['feature']}/{entry['function']}) — {entry['classification']}")
    unexpected = [entry for entry in persistent if entry.get("classification") not in CONSENTED]
    if unexpected:
        print("ЭТО ОШИБКА: операция не должна была уйти.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(main())
