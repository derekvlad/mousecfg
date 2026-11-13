"""Вендорский канал YJX/MKESPN — только чтение.

Протокол закрытый и документирован сообществом. В этом пакете нет ни одной
функции записи: запись конфигурации у таких мышей сразу уходит в onboard-
флеш, а команда `0d 00` с непустым телом стирает участок памяти. Политика
зафиксирована в `guard.py` и проверяется до каждого `os.write`.
"""

from .device import VendorDevice, VendorProbe, VendorResponse
from .guard import Dialect, VendorRefused

__all__ = [
    "Dialect",
    "VendorDevice",
    "VendorProbe",
    "VendorRefused",
    "VendorResponse",
]
