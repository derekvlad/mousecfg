"""Тесты выбора языка интерфейса и хелпера ``t``."""

from __future__ import annotations

import pytest

from mousecfg import i18n, profiles
from mousecfg.input import actions

ENV_VARS = ("MOUSECFG_LANG", "LC_ALL", "LC_MESSAGES", "LANG")


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Очистить переменные локали и выставить только заданные."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    i18n.set_language(None)


def test_default_is_russian(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert i18n.language() == "ru"


def test_lang_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LANG="en_US.UTF-8")
    assert i18n.language() == "en"


@pytest.mark.parametrize("value", ["en", "en_US.UTF-8", "en-GB", "EN"])
def test_locale_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _env(monkeypatch, LANG=value)
    assert i18n.language() == "en"


def test_mousecfg_lang_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, MOUSECFG_LANG="en", LANG="ru_RU.UTF-8")
    assert i18n.language() == "en"


def test_lc_all_beats_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LC_ALL="en_US.UTF-8", LANG="ru_RU.UTF-8")
    assert i18n.language() == "en"


def test_unknown_locale_falls_back_to_russian(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LANG="de_DE.UTF-8")
    assert i18n.language() == "ru"


def test_empty_values_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LC_ALL="", LANG="en_US.UTF-8")
    assert i18n.language() == "en"


def test_set_language_overrides_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LANG="ru_RU.UTF-8")
    assert i18n.set_language("en") == "en"
    assert i18n.language() == "en"
    assert i18n.set_language(None) == "ru"


def test_set_language_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        i18n.set_language("de")


def test_t_uses_current_language(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert i18n.t("привет", "hello") == "привет"
    i18n.set_language("en")
    assert i18n.t("привет", "hello") == "hello"


def test_english_action_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LANG="en_US.UTF-8")
    assert actions.parse_action("ignore").describe() == "nothing"
    assert actions.parse_action("passthrough").describe() == "as is"
    with pytest.raises(actions.ActionError, match="unknown key"):
        actions.parse_keys("ctrl+nosuchkey")


def test_english_profile_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LANG="en_US.UTF-8")
    with pytest.raises(profiles.ProfileError, match="is not supported"):
        profiles.parse_rate("123Hz")
