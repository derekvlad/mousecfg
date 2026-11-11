"""Регрессия «профиль сбрасывается на первый».

Демон пересчитывает профиль на каждой смене активного приложения. Если в
файле нет профиля с `match_apps`, `profiles.select` возвращает первый профиль
по порядку — и ручной выбор пользователя (CLI/трей/GUI) затирался на
ближайшей смене фокуса. Тест проверяет, что ручной выбор удерживается, а
профиль с `match_apps` по-прежнему важнее.
"""

from __future__ import annotations

from pathlib import Path

from mousecfg import daemon, engine, profiles, registry

PROFILES = """
version = 1

[[profile]]
id = "default"
dpi = 3200

[[profile]]
id = "game"
dpi = 2800

[[profile]]
id = "fps"
dpi = 800
match_apps = ["cs2"]
"""


class _Opened:
    """Заглушка контекстного менеджера session.open_device."""

    def __init__(self) -> None:
        self.device = object()

    def __enter__(self) -> _Opened:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _make_daemon(tmp_path: Path, text: str = PROFILES) -> tuple[daemon.Daemon, daemon.DeviceRuntime]:
    path = tmp_path / "profiles.toml"
    path.write_text(text, encoding="utf-8")
    options = daemon.DaemonConfig(watch_apps=False, apply_buttons=False)
    d = daemon.Daemon(options)
    state = registry.DeviceState(
        key="serial:1", serial="1", name="Test Mouse", profiles_file=str(path)
    )
    runtime = daemon.DeviceRuntime(key=state.key, state=state)
    d._load_profile_set(runtime)
    d.devices[state.key] = runtime
    return d, runtime


def _stub_apply(monkeypatch) -> None:
    monkeypatch.setattr("mousecfg.session.open_device", lambda *_a, **_k: _Opened())

    def fake_apply(_device, profile: profiles.Profile) -> engine.ApplyResult:
        return engine.ApplyResult(profile_id=profile.id, applied={"dpi": profile.dpi})

    monkeypatch.setattr(engine, "apply_profile", fake_apply)
    monkeypatch.setattr("mousecfg.registry.save_state", lambda _state: None)


def test_manual_pick_survives_focus_change(monkeypatch, tmp_path):
    _stub_apply(monkeypatch)
    d, runtime = _make_daemon(tmp_path)

    # Пользователь вручную выбрал game, затем сменилось окно (firefox).
    d.focused_app = "firefox"
    runtime.state.pinned_profile = "game"
    d._apply(runtime, reason="смена приложения")

    assert runtime.active_profile.id == "game"
    assert runtime.state.active_profile == "game"


def test_app_match_beats_manual_pick(monkeypatch, tmp_path):
    _stub_apply(monkeypatch)
    d, runtime = _make_daemon(tmp_path)

    runtime.state.pinned_profile = "game"
    d.focused_app = "cs2"
    d._apply(runtime, reason="смена приложения")

    # Профиль приложения конкретнее ручного выбора.
    assert runtime.active_profile.id == "fps"


def test_manual_pick_parks_when_app_match_leaves(monkeypatch, tmp_path):
    _stub_apply(monkeypatch)
    d, runtime = _make_daemon(tmp_path)

    runtime.state.pinned_profile = "game"
    d.focused_app = "cs2"
    d._apply(runtime, reason="смена приложения")
    assert runtime.active_profile.id == "fps"

    # Ушли из приложения — возвращаемся к ручному выбору, а не к первому.
    d.focused_app = "firefox"
    d._apply(runtime, reason="смена приложения")
    assert runtime.active_profile.id == "game"


def test_removed_pinned_profile_is_cleared(monkeypatch, tmp_path):
    _stub_apply(monkeypatch)
    d, runtime = _make_daemon(tmp_path)
    runtime.state.pinned_profile = "game"

    shorter = 'version = 1\n\n[[profile]]\nid = "default"\ndpi = 3200\n'
    Path(runtime.state.profiles_file).write_text(shorter, encoding="utf-8")
    d._load_profile_set(runtime)

    assert runtime.state.pinned_profile == ""
    d.focused_app = "firefox"
    d._apply(runtime, reason="смена приложения")
    assert runtime.active_profile.id == "default"


def test_activate_pins_profile(monkeypatch, tmp_path):
    _stub_apply(monkeypatch)
    d, runtime = _make_daemon(tmp_path)

    result = d._rpc_activate({"device": runtime.key, "profile": "game"})
    assert result["active_profile"] == "game"
    assert runtime.state.pinned_profile == "game"

    # Последующая смена фокуса не сбрасывает выбор.
    d.focused_app = "firefox"
    d._apply(runtime, reason="смена приложения")
    assert runtime.active_profile.id == "game"
