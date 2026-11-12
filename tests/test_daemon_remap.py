"""Регрессия «после перезагрузки не работают кнопки и макросы».

Две независимые причины, и обе нужно закрыть:

1. Правило udev навешивало тег `uaccess` только на `ACTION=="add"`. С
   systemd v258 ACL навешивает udevd по change-событиям, поэтому ACL на
   `/dev/input/event*` не появлялся (проверяется в `tests/test_udev_rules.py`).
2. Даже когда права появляются с запозданием — демон стартует вместе с
   графической сессией, и event-узел может быть недоступен в момент
   подключения мыши. Раньше демон в этом случае запоминал ошибку и больше
   не пытался поднять ремап до переподключения устройства.

Здесь проверяется поведение (2): демон периодически повторяет попытку
поднять перехват, пока event-узел не станет доступен.
"""

from __future__ import annotations

from pathlib import Path

from mousecfg import daemon, registry
from mousecfg.input.detect import InputDeviceInfo

PROFILES = """
version = 1

[[profile]]
id = "default"
dpi = 1600

[profile.buttons]
side1 = "browser-back"
side2 = "copy"
"""


def _runtime(tmp_path: Path) -> daemon.DeviceRuntime:
    path = tmp_path / "profiles.toml"
    path.write_text(PROFILES, encoding="utf-8")
    state = registry.DeviceState(
        key="serial:1", serial="1", name="Test Mouse", profiles_file=str(path)
    )
    return daemon.DeviceRuntime(key=state.key, state=state)


def _daemon(tmp_path: Path, runtime: daemon.DeviceRuntime) -> daemon.Daemon:
    options = daemon.DaemonConfig(watch_apps=False, apply_buttons=True)
    d = daemon.Daemon(options)
    d._find_input = lambda _runtime: _INPUT_INFO  # type: ignore[method-assign]
    d._load_profile_set(runtime)
    d.devices[runtime.key] = runtime
    return d


_INPUT_INFO = InputDeviceInfo(
    path="/dev/input/event9",
    name="Test Mouse",
    phys="usb-0/input0",
    uniq="1",
    buttons=(272, 273),
)


def test_missing_input_node_sets_retry_timer(monkeypatch, tmp_path):
    """Если узла нет — демон запоминает ошибку и планирует повтор."""
    monkeypatch.setattr("mousecfg.registry.save_state", lambda _state: None)
    runtime = _runtime(tmp_path)
    d = _daemon(tmp_path, runtime)

    def missing(_runtime: daemon.DeviceRuntime) -> InputDeviceInfo:
        raise LookupError("не найден event-узел мыши для ремапа кнопок")

    d._find_input = missing  # type: ignore[method-assign]
    d._start_remap(runtime)

    assert runtime.input_info is None
    assert "event-узел" in runtime.last_error


def test_remap_retried_until_node_appears(monkeypatch, tmp_path):
    """Пока узла нет — повторяем; как только появился — поднимаем перехват."""
    monkeypatch.setattr("mousecfg.registry.save_state", lambda _state: None)
    restarted: list[str] = []

    runtime = _runtime(tmp_path)
    d = _daemon(tmp_path, runtime)
    d._restart_remap = lambda rt: restarted.append(rt.key)  # type: ignore[method-assign]

    calls = {"n": 0}

    def flaky(_runtime: daemon.DeviceRuntime) -> InputDeviceInfo:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LookupError("не найден event-узел мыши для ремапа кнопок")
        return _INPUT_INFO

    d._find_input = flaky  # type: ignore[method-assign]

    # Первая попытка при подключении падает: узла ещё нет.
    d._start_remap(runtime)
    assert runtime.input_info is None
    assert calls["n"] == 1

    # Повтор сразу не идёт: таймер ещё не истёк.
    d._retry_pending_remaps()
    assert calls["n"] == 1

    # Сдвигаем таймер в прошлое — как будто прошло REMAP_RETRY_SECONDS.
    runtime.remap_retry_at = 0.0
    d._retry_pending_remaps()

    assert runtime.input_info is not None
    assert runtime.state.input_path == "/dev/input/event9"
    assert restarted == [runtime.key]
    # После успеха ошибка про права снята.
    assert runtime.last_error == ""


def test_retry_skips_when_remap_disabled(monkeypatch, tmp_path):
    """Выключенный вручную ремап повтор не поднимает."""
    monkeypatch.setattr("mousecfg.registry.save_state", lambda _state: None)
    runtime = _runtime(tmp_path)
    d = _daemon(tmp_path, runtime)
    calls = {"n": 0}

    def flaky(_runtime: daemon.DeviceRuntime) -> InputDeviceInfo:
        calls["n"] += 1
        raise LookupError("не найден event-узел мыши для ремапа кнопок")

    d._find_input = flaky  # type: ignore[method-assign]
    runtime.remap_disabled = True
    d._retry_pending_remaps()

    assert calls["n"] == 0


def test_retry_skips_when_input_known(monkeypatch, tmp_path):
    """Если узел уже найден, повтор не дёргает устройство лишний раз."""
    monkeypatch.setattr("mousecfg.registry.save_state", lambda _state: None)
    restarted: list[str] = []
    runtime = _runtime(tmp_path)
    d = _daemon(tmp_path, runtime)
    d._restart_remap = lambda rt: restarted.append(rt.key)  # type: ignore[method-assign]

    d._start_remap(runtime)
    assert restarted == [runtime.key]

    restarted.clear()
    runtime.remap_retry_at = 0.0
    d._retry_pending_remaps()
    assert restarted == []
