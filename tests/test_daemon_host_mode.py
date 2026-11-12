"""Регрессия «после перезагрузки мышь снова в On-Board и настройки не применяются».

Прошивка некоторых мышей (например, G102) не помнит Host mode между сессиями:
после перезагрузки устройство возвращается в On-Board, и настройки с хоста
игнорируются. `mousecfg mode host --allow-onboard-switch` — единственная
персистентная операция проекта, и раньше согласие нигде не сохранялось, так
что демон при старте не мог перейти в Host сам.

Теперь при явном переходе согласие запоминается (`consent.json`), и демон при
подключении мыши, оказавшейся в On-Board, повторяет переход. Здесь
проверяется, что:

* согласие записывается и читается;
* без согласия демон НЕ трогает режим (иначе это была бы неявная запись);
* с согласием демон переходит в Host и обновляет состояние.
"""

from __future__ import annotations

import pytest

from mousecfg import consent, daemon, registry


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Согласие и состояние пишутся в tmp — реальные файлы не трогаем."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("mousecfg.registry.save_state", lambda _state: None)
    yield


def test_auto_host_off_by_default(monkeypatch, tmp_path):
    """Без записанного согласия демон не переходит в Host сам."""
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))
    assert d._allow_switch_for("serial:1") is False


def test_allow_switch_true_when_cli_flag(monkeypatch, tmp_path):
    """Флаг демона `--allow-onboard-switch` даёт согласие на текущий запуск."""
    d = daemon.Daemon(
        daemon.DaemonConfig(watch_apps=False, apply_buttons=False, allow_onboard_switch=True)
    )
    assert d._allow_switch_for("serial:1") is True


def test_allow_switch_honors_recorded_consent(monkeypatch, tmp_path):
    """После `mode host --allow-onboard-switch` согласие помнится между запусками."""
    consent.record("serial:1")
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))
    assert d._allow_switch_for("serial:1") is True
    assert d._allow_switch_for("serial:other") is False


def test_no_auto_host_disables_consent(monkeypatch, tmp_path):
    """`--no-auto-host` отключает автоматический переход, даже если согласие есть."""
    consent.record("serial:1")
    d = daemon.Daemon(
        daemon.DaemonConfig(watch_apps=False, apply_buttons=False, auto_host=False)
    )
    assert d._allow_switch_for("serial:1") is False


class _FakeDevice:
    """Минимальная заглушка HID++-устройства для смены режима."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.host_calls = 0

    def set_host_mode(self) -> str:
        self.host_calls += 1
        self.mode = "host"
        return self.mode

    def get_mode(self) -> str:
        return self.mode

    # -- то, что `_attach` читает помимо режима ---------------------------
    def has_feature(self, _feature: int) -> bool:
        return True

    def info(self):
        from mousecfg.hidpp.device import DeviceInfo

        return DeviceInfo(path="/dev/hidraw9", name="Test Mouse")

    def get_dpi(self) -> int:
        return 1600

    def get_rate_ms(self) -> int:
        return 1


def test_switch_to_host_succeeds(monkeypatch, tmp_path):
    """С согласием переход выполняется и состояние обновляется на host."""
    consent.record("serial:1")
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))
    device = _FakeDevice("onboard")

    mode = d._switch_to_host(device, name="Test Mouse")

    assert device.host_calls == 1
    assert mode == "host"


def test_switch_to_host_survives_device_error(monkeypatch, tmp_path):
    """Ошибка перехода не роняет демон: режим остаётся onboard."""
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))

    class _Broken(_FakeDevice):
        def set_host_mode(self) -> str:
            self.host_calls += 1
            raise OSError("usb отключился")

    device = _Broken("onboard")
    mode = d._switch_to_host(device, name="Test Mouse")

    assert mode == "onboard"
    assert device.host_calls == 1


def test_consent_roundtrip(tmp_path, monkeypatch):
    """Запись/чтение/снятие согласия."""
    assert consent.granted("serial:1") is False
    consent.record("serial:1")
    assert consent.granted("serial:1") is True
    consent.forget("serial:1")
    assert consent.granted("serial:1") is False


def test_consent_ignores_unknown_key(tmp_path, monkeypatch):
    """Ключ `unknown` не сохраняется: он не идентифицирует устройство."""
    consent.record("unknown")
    assert consent.granted("unknown") is False
    assert consent.load() == set()


def test_consent_survives_corrupt_file(tmp_path, monkeypatch):
    """Битый файл согласия не роняет демон — считается, что согласия нет."""
    from mousecfg import config

    path = config.consent_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ это не json", encoding="utf-8")
    assert consent.granted("serial:1") is False
    # И запись после этого снова работает.
    consent.record("serial:1")
    assert consent.granted("serial:1") is True


def test_attach_switches_to_host_with_consent(tmp_path, monkeypatch):
    """Интеграционно: `_attach` на On-Board-мыши с согласием уходит в Host."""
    from mousecfg import discovery

    profiles = tmp_path / "profiles.toml"
    profiles.write_text('version = 1\n\n[[profile]]\nid = "default"\ndpi = 1600\n', encoding="utf-8")

    device = _FakeDevice("onboard")
    node = discovery.HidrawNode(
        path="/dev/hidraw9",
        name="Test Mouse",
        vendor_id=0x046D,
        product_id=0xC092,
        bus="usb",
        serial="1",
        interface_number=1,
        hid_id="0003:046D:C092.0002",
        usb_address="1-2",
    )
    key = registry.device_key(serial="1", usb_id=node.usb_id, usb_address="1-2")
    consent.record(key)

    class _Opened:
        def __init__(self) -> None:
            self.node = node
            self.device = device

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("mousecfg.session.open_device", lambda *_a, **_k: _Opened())
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))
    monkeypatch.setattr(d, "_apply", lambda *_a, **_k: None)

    d._attach(node, key)

    assert device.host_calls == 1
    assert d.state[key].mode == "host"


def test_attach_keeps_onboard_without_consent(tmp_path, monkeypatch):
    """Без согласия `_attach` не трогает режим — в память не пишем."""
    from mousecfg import discovery

    profiles = tmp_path / "profiles.toml"
    profiles.write_text('version = 1\n\n[[profile]]\nid = "default"\ndpi = 1600\n', encoding="utf-8")

    device = _FakeDevice("onboard")
    node = discovery.HidrawNode(
        path="/dev/hidraw9",
        name="Test Mouse",
        vendor_id=0x046D,
        product_id=0xC092,
        bus="usb",
        serial="1",
        interface_number=1,
        hid_id="0003:046D:C092.0002",
        usb_address="1-2",
    )
    key = registry.device_key(serial="1", usb_id=node.usb_id, usb_address="1-2")

    class _Opened:
        def __init__(self) -> None:
            self.node = node
            self.device = device

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("mousecfg.session.open_device", lambda *_a, **_k: _Opened())
    d = daemon.Daemon(daemon.DaemonConfig(watch_apps=False, apply_buttons=False))
    monkeypatch.setattr(d, "_apply", lambda *_a, **_k: None)

    d._attach(node, key)

    assert device.host_calls == 0
    assert d.state[key].mode == "onboard"
