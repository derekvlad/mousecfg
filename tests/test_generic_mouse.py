"""Мыши без HID++: профиль применяется, но команды протокола не уходят.

Такие модели (например, AJAZZ 2.4G на вендорском протоколе) управляются
только через evdev: профили, кнопки и макросы живут на хосте, а DPI, частота
опроса и подсветка задаются самой мышью.
"""

from __future__ import annotations

import pytest

from mousecfg import discovery, engine, profiles, session


def _evdev_mouse() -> discovery.MouseInfo:
    return discovery.MouseInfo(
        key="serial:D1K_251010b271_A72V1",
        name="YJX-CHIP AJAZZ 2.4G Mouse",
        serial="D1K_251010b271_A72V1",
        usb_id="a8a5:2255",
        usb_address="1-2",
        vendor_id=0xA8A5,
        product_id=0x2255,
        protocol="evdev",
        family="ajazz-2.4g",
        hidraw_path="/dev/hidraw4",
        input_path="/dev/input/event20",
        interface_number=0,
        capabilities=discovery.EVDEV_CAPABILITIES,
    )


def test_apply_profile_without_hidpp_skips_protocol_settings() -> None:
    profile = profiles.Profile(
        id="default",
        name="Default",
        dpi=1600,
        report_rate_ms=1,
        buttons={"side1": "copy"},
        lighting={"effect": "static", "color": 0xFF0000},
    )

    result = engine.apply_profile(None, profile)

    assert result.ok
    assert result.applied == {}
    skipped = " ".join(result.skipped)
    assert "DPI" in skipped
    assert "частота" in skipped or "report rate" in skipped
    assert "подсветка" in skipped or "lighting" in skipped
    assert "evdev" in skipped


def test_apply_profile_without_hidpp_keeps_buttons_plan() -> None:
    profile = profiles.Profile(id="default", buttons={"side1": "copy"})

    assert engine.plan_button_actions(profile) == {"side1": "KEY_LEFTCTRL+KEY_C"}


def test_open_device_by_hint_explains_missing_hidpp(monkeypatch: pytest.MonkeyPatch) -> None:
    mouse = _evdev_mouse()
    monkeypatch.setattr(discovery, "find_by_hint", lambda _hint: [])
    monkeypatch.setattr(discovery, "mouse_for_hint", lambda _hint: mouse)

    with pytest.raises(session.UnsupportedProtocol) as excinfo:
        session.open_device("a8a5:2255")

    message = str(excinfo.value)
    assert mouse.name in message
    assert "HID++" in message
    assert "evdev" in message


def test_open_device_without_hint_lists_evdev_mice(monkeypatch: pytest.MonkeyPatch) -> None:
    mouse = _evdev_mouse()
    monkeypatch.setattr(discovery, "find_mice", lambda: [mouse])

    with pytest.raises(session.DeviceNotFound) as excinfo:
        session.open_device()

    message = str(excinfo.value)
    assert mouse.name in message
    assert "DPI" in message


def test_evdev_mouse_has_no_protocol_capabilities() -> None:
    mouse = _evdev_mouse()

    assert not mouse.supports_hidpp
    assert mouse.supports("buttons")
    for capability in ("dpi", "report_rate", "lighting", "mode"):
        assert not mouse.supports(capability)


def test_daemon_applies_evdev_profile_without_hidraw(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Для evdev-мыши демон не открывает hidraw, но профиль выбирает."""
    from mousecfg import daemon, registry

    path = tmp_path / "profiles.toml"
    path.write_text(
        'version = 1\n\n[[profile]]\nid = "default"\ndpi = 1600\n\n'
        '[profile.buttons]\nside1 = "copy"\n',
        encoding="utf-8",
    )
    state = registry.DeviceState(
        key="serial:1",
        serial="1",
        name="YJX-CHIP AJAZZ 2.4G Mouse",
        protocol="evdev",
        profiles_file=str(path),
    )
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))
    runtime = daemon.DeviceRuntime(key=state.key, state=state)
    d._load_profile_set(runtime)
    d.devices[state.key] = runtime

    monkeypatch.setattr(session, "open_device", lambda *_a, **_k: pytest.fail("hidraw must not be opened"))
    monkeypatch.setattr(registry, "save_state", lambda _state: None)

    d._apply(runtime, reason="тест")

    assert runtime.active_profile is not None
    assert runtime.active_profile.id == "default"
    assert runtime.dpi is None
    assert runtime.report_rate_ms is None
