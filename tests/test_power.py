"""Значок и подпись заряда — чистые функции, без GTK и без устройства."""

from __future__ import annotations

import pytest

from mousecfg.power import battery_display


@pytest.mark.parametrize(
    ("percent", "expected_icon"),
    [
        (100, "battery-full-symbolic"),
        (81, "battery-full-symbolic"),
        (80, "battery-good-symbolic"),
        (50, "battery-good-symbolic"),
        (41, "battery-good-symbolic"),
        (40, "battery-low-symbolic"),
        (16, "battery-low-symbolic"),
        (15, "battery-caution-symbolic"),
        (0, "battery-caution-symbolic"),
    ],
)
def test_battery_icon_thresholds(percent: int, expected_icon: str) -> None:
    info = battery_display(percent)

    assert info is not None
    label, icon = info
    assert icon == expected_icon
    assert str(percent) in label


@pytest.mark.parametrize("value", [None, "", 101, -1, True, 1.5, []])
def test_battery_hides_for_unknown_values(value: object) -> None:
    assert battery_display(value) is None


def test_hover_title_includes_battery_profile_and_name() -> None:
    from mousecfg.power import hover_title

    title = hover_title(name="YJX-CHIP AJAZZ 2.4G Mouse", profile="default", percent=78)

    assert title.startswith("mousecfg")
    assert "заряд: 78%" in title
    assert "default" in title
    assert "AJAZZ" in title


def test_hover_title_without_battery_still_shows_placeholder() -> None:
    from mousecfg.power import hover_title

    title = hover_title(name="Mouse", profile="", percent=None)

    assert "заряд: —" in title
    assert "Mouse" in title
    assert " ·  · " not in title  # пустые поля не оставляют двойных разделителей


def test_hover_title_skips_empty_name() -> None:
    from mousecfg.power import hover_title

    title = hover_title(name="", profile="default", percent=50)

    assert title == "mousecfg · default · заряд: 50%"
