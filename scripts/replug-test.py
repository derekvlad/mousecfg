#!/usr/bin/env python3
"""Проверка волатильности настроек: значения должны сброситься при переподключении.

Скрипт:
1. запоминает текущие DPI и report rate;
2. ставит заметные значения (например 4096 dpi и 8 мс);
3. просит физически отключить и подключить мышь;
4. читает значения заново и делает вывод.

Ожидаемый результат: значения вернулись к дефолтным. Это подтверждает, что
проект меняет только оперативную память устройства.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mousecfg import session  # noqa: E402

TEST_DPI = 4096
TEST_RATE_MS = 8


def read_state() -> tuple[int, int, str]:
    with session.open_device(keep_journal=False) as opened:
        device = opened.device
        return device.get_dpi(), device.get_rate_ms(), device.get_mode()


def main() -> int:
    dpi, rate, mode = read_state()
    print(f"Сейчас: dpi={dpi}, report_rate={rate} мс, режим={mode}")

    with session.open_device(keep_journal=False) as opened:
        opened.device.set_dpi(TEST_DPI)
        opened.device.set_rate_ms(TEST_RATE_MS)
    after_set_dpi, after_set_rate, _ = read_state()
    print(f"После записи: dpi={after_set_dpi}, report_rate={after_set_rate} мс")

    if after_set_dpi != TEST_DPI or after_set_rate != TEST_RATE_MS:
        print("Не удалось задать тестовые значения — проверьте режим (нужен Host).", file=sys.stderr)
        return 1

    print("\nОтключите мышь от USB и подключите обратно, затем нажмите Enter.")
    try:
        input()
    except EOFError:
        print("Нужен интерактивный ввод.", file=sys.stderr)
        return 2

    new_dpi, new_rate, new_mode = read_state()
    print(f"После переподключения: dpi={new_dpi}, report_rate={new_rate} мс, режим={new_mode}")

    volatile = new_dpi != TEST_DPI or new_rate != TEST_RATE_MS
    if volatile:
        print("\nНастройки сбросились -> они жили в оперативной памяти мыши.")
        print("Это ровно то поведение, которого требует проект.")
        return 0

    print(
        "\nНастройки сохранились после переподключения.\n"
        "Возможные причины: устройство удерживает их самостоятельно, либо мышь\n"
        "не переподключалась по-настоящему (проверьте, что порт обесточился).\n"
        "Зафиксируйте наблюдение в docs/device-matrix.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
