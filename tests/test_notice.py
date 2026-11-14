"""Предупреждение о низком заряде: решение (чистая логика) и отправка."""

from __future__ import annotations

import subprocess

import pytest

from mousecfg import notice


def test_warns_once_at_low_charge() -> None:
    monitor = notice.LowBatteryMonitor(threshold=15, rearm=25)

    first = monitor.update(12, charging=False)

    assert first is not None
    title, body = first
    assert "разряжена" in title
    assert "12" in body
    # Повторно, пока заряд не восстановился, молчим.
    assert monitor.update(10, charging=False) is None
    assert monitor.update(8, charging=False) is None


def test_rearms_only_after_charge_recovers() -> None:
    monitor = notice.LowBatteryMonitor(threshold=15, rearm=25)
    monitor.update(10, charging=False)

    assert monitor.update(20, charging=False) is None  # ниже порога взвода
    assert monitor.update(30, charging=False) is None   # восстановились
    assert monitor.update(12, charging=False) is not None  # снова предупреждаем


def test_charging_rearms_immediately() -> None:
    monitor = notice.LowBatteryMonitor(threshold=15, rearm=25)
    monitor.update(10, charging=False)

    assert monitor.update(11, charging=True) is None      # на кабеле
    assert monitor.update(12, charging=False) is not None  # сняли — предупреждаем снова


@pytest.mark.parametrize("percent", [None, 16, 50, 100, -1, 101])
def test_no_warning_above_threshold_or_without_reading(percent: int | None) -> None:
    monitor = notice.LowBatteryMonitor()

    assert monitor.update(percent, charging=False) is None


def test_unknown_charging_flag_still_warns() -> None:
    monitor = notice.LowBatteryMonitor()

    assert monitor.update(10, charging=None) is not None


def test_notify_returns_false_without_notify_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notice.shutil, "which", lambda _name: None)

    assert notice.notify("t", "b") is False


def test_notify_survives_missing_binary_or_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notice.shutil, "which", lambda _name: "/usr/bin/notify-send")

    def boom(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="notify-send", timeout=5.0)

    monkeypatch.setattr(notice.subprocess, "run", boom)

    assert notice.notify("t", "b") is False


def test_notify_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notice.shutil, "which", lambda _name: "/usr/bin/notify-send")

    class _Done:
        returncode = 0

    seen: dict[str, object] = {}

    def fake_run(command: list[str], **_kwargs: object) -> object:
        seen["command"] = command
        return _Done()

    monkeypatch.setattr(notice.subprocess, "run", fake_run)

    assert notice.notify("Mouse low", "12%") is True
    assert "--app-name=mousecfg" in seen["command"]  # type: ignore[operator]
    assert "Mouse low" in seen["command"]  # type: ignore[operator]
