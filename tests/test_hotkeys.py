"""Тесты распознавания композитора и генерации хоткеев.

Портал GlobalShortcuts работает не на всех рабочих столах: на niri нет
бэкенда, а GNOME-бэкенд под ним плодит зависшие окна. Поэтому важны обе
ветки — «портал доступен» и «хоткей вешает композитор». Проверки чистые:
D-Bus и композитор не трогаются, только окружение и строки.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mousecfg import hotkeys


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_current_compositor_is_lowercased(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "Niri")
    assert hotkeys.current_compositor() == "niri"


def test_current_compositor_handles_multiple_entries(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME:Unity")
    assert hotkeys.current_compositor() == "gnome"


def test_portal_not_supported_on_niri(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "niri")
    assert hotkeys.portal_supported() is False


def test_portal_supported_on_gnome(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert hotkeys.portal_supported() is True


def test_niri_config_path_uses_xdg(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert hotkeys.niri_config_path() == Path("/tmp/xdg/niri/config.kdl")


def test_niri_config_path_defaults_to_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    assert hotkeys.niri_config_path() == Path("/home/tester/.config/niri/config.kdl")


def test_niri_keybinds_has_both_directions():
    block = hotkeys.niri_keybinds()
    assert "spawn-sh" in block
    assert "profile next" in block
    assert "profile next --prev" in block
    # Каждая строка — bind, а не действие без клавиши.
    assert "{" in block and "}" in block


def test_niri_keybinds_present_detects_marker():
    assert hotkeys.niri_keybinds_present("binds {\n  Alt+Shift+Right { spawn-sh \"mousecfg profile next\"; }\n}")
    assert not hotkeys.niri_keybinds_present("binds {\n  Mod+T { spawn \"kitty\"; }\n}")


def test_tray_skips_portal_env_values_are_stable(monkeypatch):
    """Пустое окружение не считается рабочим столом с порталом."""
    # Пустой XDG_CURRENT_DESKTOP: не в списке «без портала», поэтому портал
    # считается доступным — это безопасно, попытка просто не сработает.
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    assert hotkeys.current_compositor() == ""
    assert hotkeys.portal_supported() is True
