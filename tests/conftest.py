"""Общие настройки тестов.

Тесты проверяют русские тексты сообщений, поэтому язык интерфейса
принудительно фиксируется на ``ru``: иначе результат зависел бы от локали
машины, где запускаются тесты. Тесты английского выставляют язык сами.
"""

from __future__ import annotations

import pytest

from mousecfg import i18n


@pytest.fixture(autouse=True)
def _russian_interface():
    i18n.set_language("ru")
    yield
    i18n.set_language(None)
