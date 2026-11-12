"""Демон: следит за мышью и применяет профили.

Работает в пользовательской сессии (`systemd --user`), root не нужен.
Никакие команды в обход `SafePlane` здесь не появляются: демон использует
тот же слой, что и CLI.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import appwatch, config, consent, engine, ipc, profiles, registry, session
from .hidpp.protocol import ProtocolError, TransportError
from .i18n import t
from .input.actions import ActionError, parse_action
from .input.detect import InputDeviceInfo, find_pointer_devices
from .input.macros import parse_macros
from .input.remapper import Remapper

logger = logging.getLogger(__name__)

# Раз в секунду смотрим, не сменилось ли активное приложение.
FOCUS_POLL_SECONDS = 0.5
RECONNECT_SECONDS = 1.0
# Если event-узел мыши не нашёлся при подключении (ACL появился позже, узел
# ещё не создан), пробуем поднять ремап заново с этой периодичностью.
REMAP_RETRY_SECONDS = 5.0


@dataclass
class DeviceRuntime:
    """Состояние одной мыши: профиль, открытые ресурсы, ремап."""

    key: str
    state: registry.DeviceState
    profile_set: profiles.ProfileSet | None = None
    active_profile: profiles.Profile | None = None
    remapper: Remapper | None = None
    remapper_thread: threading.Thread | None = None
    input_info: InputDeviceInfo | None = None
    dpi: int | None = None
    report_rate_ms: int | None = None
    # Пользователь выключил ремап через трей: тогда смена профиля не поднимает
    # перехват заново, пока его не включат обратно.
    remap_disabled: bool = False
    # Время последней попытки поднять ремап, когда event-узел не нашёлся
    # (ACL или сам узел появляются чуть позже запуска демона).
    remap_retry_at: float = 0.0
    last_error: str = ""
    # Последняя ошибка связана с поиском event-узла (а не с профилями):
    # когда узел наконец находится, её можно просто очистить.
    input_error: bool = False

    @property
    def profile_ids(self) -> list[str]:
        if self.profile_set is None:
            return []
        return [profile.id for profile in self.profile_set.profiles]

    def stop(self) -> None:
        if self.remapper is not None:
            self.remapper.stop()
        if self.remapper_thread is not None:
            self.remapper_thread.join(timeout=3.0)
            self.remapper_thread = None
        self.remapper = None


@dataclass
class DaemonConfig:
    dry_run: bool = False
    allow_onboard_switch: bool = False
    profiles_dir: Path = field(default_factory=config.profiles_dir)
    watch_apps: bool = True
    apply_buttons: bool = True
    # Автоматически повторять переход On-Board → Host при подключении мыши,
    # если пользователь уже давал на это явное согласие
    # (`mousecfg mode host --allow-onboard-switch` запоминает его в consent.json).
    auto_host: bool = True


class Daemon:
    def __init__(self, options: DaemonConfig | None = None) -> None:
        self.options = options or DaemonConfig()
        self.state: dict[str, registry.DeviceState] = registry.load_state()
        self.devices: dict[str, DeviceRuntime] = {}
        self.server = ipc.Server(self._handle_rpc)
        self.watcher = appwatch.detect_watcher() if self.options.watch_apps else None
        self.focused_app = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._rpc_queue: list[tuple[dict, threading.Event]] = []

    # -- запуск ---------------------------------------------------------
    def run(self) -> int:
        self.server.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop())
        rpc_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        rpc_thread.start()

        logger.info(t("демон запущен: профили из %s", "daemon started: profiles from %s"), self.options.profiles_dir)
        try:
            while not self._stop.is_set():
                self._tick()
                time.sleep(RECONNECT_SECONDS if not self.devices else FOCUS_POLL_SECONDS)
        finally:
            self.stop()
        return 0

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        with self._lock:
            for runtime in self.devices.values():
                runtime.stop()
        self.server.stop()

    # -- основной цикл --------------------------------------------------
    def _tick(self) -> None:
        try:
            nodes = session.discovery.logitech_hidpp_nodes()
        except Exception as exc:  # noqa: BLE001
            logger.warning(t("обнаружение устройств упало: %s", "device discovery failed: %s"), exc)
            return

        present = {registry.device_key(serial=node.serial, usb_id=node.usb_id, usb_address=node.usb_address) for node in nodes}
        with self._lock:
            for key in list(self.devices):
                if key not in present:
                    logger.info(t("устройство %s отключено", "device %s disconnected"), key)
                    self.devices.pop(key).stop()

        for node in nodes:
            key = registry.device_key(serial=node.serial, usb_id=node.usb_id, usb_address=node.usb_address)
            if key in self.devices:
                continue
            self._attach(node, key)

        if self.watcher is not None and self.options.watch_apps:
            self._check_focus()

        if self.options.apply_buttons:
            self._retry_pending_remaps()

    def _retry_pending_remaps(self) -> None:
        """Повторно поднять ремап там, где event-узел не нашёлся сразу.

        При старте демона вместе с графической сессией ACL на
        `/dev/input/event*` может появиться позже (logind шлёт change-события
        уже после запуска сервиса). Без повтора мышь осталась бы без кнопок
        и макросов до переподключения.
        """
        now = time.monotonic()
        for runtime in list(self.devices.values()):
            if runtime.profile_set is None:
                continue
            if runtime.remapper is not None or runtime.remap_disabled:
                continue
            if runtime.input_info is not None:
                continue
            if now < runtime.remap_retry_at:
                continue
            self._start_remap(runtime)

    def _allow_switch_for(self, key: str) -> bool:
        """Разрешён ли переход On-Board → Host для этого устройства.

        Либо демон запущен с `--allow-onboard-switch` (согласие на текущий
        запуск), либо пользователь уже давал согласие ранее и оно запомнено.
        """
        if self.options.allow_onboard_switch:
            return True
        if not self.options.auto_host:
            return False
        return consent.granted(key)

    def _switch_to_host(self, device: object, *, name: str) -> str:
        """Повторить переход On-Board → Host по ранее данному согласию.

        Возвращает фактический режим устройства после попытки. Ошибка не
        фатальна: мышь останется в On-Board, и об этом будет предупреждение.
        """
        try:
            device.set_host_mode()
            new_mode = device.get_mode()
        except (TransportError, ProtocolError, TimeoutError, OSError) as exc:
            logger.warning(t("%s: не удалось перейти в Host mode: %s", "%s: failed to switch to Host mode: %s"), name, exc)
            return "onboard"
        if new_mode == "host":
            logger.info(
                t(
                    "%s: автоматический переход On-Board → Host по ранее данному согласию",
                    "%s: automatic On-Board → Host switch based on previously given consent",
                ),
                name,
            )
        return new_mode

    def _attach(self, node: session.discovery.HidrawNode, key: str) -> None:
        try:
            opened = session.open_device(
                node.path,
                allow_onboard_switch=self._allow_switch_for(key),
                dry_run=self.options.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(t("не удалось открыть %s: %s", "failed to open %s: %s"), node.path, exc)
            return

        try:
            info = opened.device.info()
            mode = opened.device.get_mode() if opened.device.has_feature(0x8100) else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(t("не удалось прочитать %s: %s", "failed to read %s: %s"), node.path, exc)
            opened.close()
            return

        # Прошивка не всегда помнит Host mode между сессиями (G102, например,
        # возвращается в On-Board после перезагрузки). Если пользователь уже
        # раз давал согласие на переход, повторяем его здесь — иначе настройки
        # с хоста будут игнорироваться до ручного `mousecfg mode host`.
        if mode == "onboard" and self._allow_switch_for(key):
            mode = self._switch_to_host(opened.device, name=info.name or node.name)

        dpi = None
        rate_ms = None
        lighting_supported = False
        try:
            dpi = opened.device.get_dpi()
            rate_ms = opened.device.get_rate_ms()
        except Exception as exc:  # noqa: BLE001
            logger.debug(t("DPI/rate недоступны у %s: %s", "DPI/rate are unavailable on %s: %s"), node.path, exc)
        try:
            lighting_supported = opened.device.has_feature(0x8071)
        except Exception as exc:  # noqa: BLE001
            logger.debug(t("подсветка недоступна у %s: %s", "lighting is unavailable on %s: %s"), node.path, exc)
        finally:
            opened.close()

        state = self.state.get(key) or registry.DeviceState(key=key)
        state.serial = node.serial
        state.name = info.name or node.name
        state.usb_id = node.usb_id
        state.usb_address = node.usb_address
        state.path = node.path
        state.mode = mode
        state.lighting_supported = lighting_supported
        state.seen_at = time.time()
        state.profiles_file = state.profiles_file or str(self.options.profiles_dir / f"{_slug(state.name)}.toml")
        self.state[key] = state

        runtime = DeviceRuntime(key=key, state=state, dpi=dpi, report_rate_ms=rate_ms)
        self._load_profile_set(runtime)
        self.devices[key] = runtime

        logger.info(t("подключено: %s (%s)", "connected: %s (%s)"), state.name, state.mode or t("режим неизвестен", "mode unknown"))
        if mode == "onboard":
            logger.warning(
                t("%s в режиме On-Board: настройки с хоста могут игнорироваться. "
                "Переход: mousecfg mode host --allow-onboard-switch",
                "%s is in On-Board mode: host settings may be ignored. "
                "Switch with: mousecfg mode host --allow-onboard-switch"),
                state.name,
            )

        self._apply(runtime, reason="подключение")
        if self.options.apply_buttons:
            self._start_remap(runtime)
        registry.save_state(self.state)
        self.server.publish({"type": "device-added", "device": ipc.serialize(asdict(state))})

    def _load_profile_set(self, runtime: DeviceRuntime) -> None:
        path = Path(runtime.state.profiles_file)
        if not path.exists():
            runtime.profile_set = None
            runtime.active_profile = None
            return
        try:
            runtime.profile_set = profiles.load_profile_file(path)
        except (profiles.ProfileError, ValueError, OSError) as exc:
            runtime.last_error = t(f"профили: {exc}", f"profiles: {exc}")
            logger.warning("%s: %s", path, exc)
            return
        # Ручной выбор мог указывать на профиль, который удалили из файла:
        # тогда снимаем закрепление, чтобы оно не блокировало обычный выбор.
        pinned = runtime.state.pinned_profile
        if pinned and profiles.get(runtime.profile_set, pinned) is None:
            runtime.state.pinned_profile = ""

    def _apply(self, runtime: DeviceRuntime, *, reason: str) -> None:
        if runtime.profile_set is None:
            return
        chosen = profiles.choose(
            runtime.profile_set,
            serial=runtime.state.serial,
            app=self.focused_app,
            pinned_id=runtime.state.pinned_profile,
        )
        if chosen is None:
            logger.debug(t("%s: нет подходящего профиля", "%s: no matching profile"), runtime.state.name)
            return
        if (
            runtime.active_profile is not None
            and runtime.active_profile.id == chosen.id
            and reason == "смена приложения"
        ):
            return

        try:
            with session.open_device(
                runtime.state.path,
                allow_onboard_switch=self._allow_switch_for(runtime.key),
                dry_run=self.options.dry_run,
            ) as opened:
                result = engine.apply_profile(opened.device, chosen)
        except (TransportError, ProtocolError, TimeoutError, OSError) as exc:
            runtime.last_error = str(exc)
            logger.warning(t("%s: не удалось применить профиль %s: %s", "%s: failed to apply profile %s: %s"), runtime.state.name, chosen.id, exc)
            return

        runtime.active_profile = chosen
        runtime.state.active_profile = chosen.id
        if "dpi" in result.applied:
            runtime.dpi = int(result.applied["dpi"])
        if "report_rate_ms" in result.applied:
            runtime.report_rate_ms = int(result.applied["report_rate_ms"])
        registry.save_state(self.state)
        logger.info("%s: %s (%s)", runtime.state.name, result.describe(), reason)
        self.server.publish({"type": "profile-applied", "device": runtime.key, "result": result.describe()})
        if self.options.apply_buttons:
            self._restart_remap(runtime)

    # -- кнопки и макросы -----------------------------------------------
    def _start_remap(self, runtime: DeviceRuntime) -> None:
        """Найти event-узел, проверить привязки и включить перехват.

        event-узел запоминается всегда, даже если текущий профиль без кнопок:
        иначе при переключении на профиль с кнопками перехват уже не поднять.
        """
        if runtime.profile_set is None or runtime.remap_disabled:
            return

        try:
            info = self._find_input(runtime)
        except LookupError as exc:
            message = str(exc)
            # Повторяем попытку, но не засоряем журнал: одно и то же
            # сообщение пишем один раз, дальше — только в debug.
            if runtime.last_error != message:
                logger.warning("%s: %s", runtime.state.name, message)
            else:
                logger.debug("%s: %s", runtime.state.name, message)
            runtime.last_error = message
            runtime.input_error = True
            runtime.remap_retry_at = time.monotonic() + REMAP_RETRY_SECONDS
            return

        runtime.input_info = info
        runtime.state.input_path = info.path
        runtime.remap_retry_at = 0.0
        # Узел нашёлся — прошлая ошибка про права больше не актуальна.
        if runtime.input_error:
            runtime.last_error = ""
            runtime.input_error = False
        registry.save_state(self.state)

        chosen = runtime.active_profile or profiles.choose(
            runtime.profile_set, serial=runtime.state.serial, pinned_id=runtime.state.pinned_profile
        )
        if chosen is None or not chosen.buttons:
            return

        try:
            from .input.actions import button_code

            for name, spec in chosen.buttons.items():
                button_code(name)
                parse_action(spec)
        except ActionError as exc:
            runtime.last_error = str(exc)
            logger.warning("%s: %s", runtime.state.name, exc)
            return

        self._restart_remap(runtime)

    def _restart_remap(self, runtime: DeviceRuntime) -> None:
        """Перезапустить перехват с новым набором кнопок."""
        runtime.stop()
        if runtime.profile_set is None or runtime.remap_disabled:
            return
        chosen = runtime.active_profile
        if chosen is None or not chosen.buttons:
            return

        info = runtime.input_info
        if info is None:
            return

        try:
            import evdev
        except ImportError as exc:  # pragma: no cover — зависит от системы
            runtime.last_error = t("модуль evdev не установлен: pip install evdev", "the evdev module is not installed: pip install evdev")
            logger.warning("%s: %s", runtime.state.name, exc)
            return

        try:
            device = evdev.InputDevice(info.path)
        except OSError as exc:
            runtime.last_error = t(f"не удалось открыть {info.path}: {exc}", f"failed to open {info.path}: {exc}")
            logger.warning(t("не удалось открыть %s: %s", "failed to open %s: %s"), info.path, exc)
            return

        from .input.actions import button_code

        bindings = {button_code(name): parse_action(spec) for name, spec in chosen.buttons.items()}
        macros = parse_macros(runtime.profile_set.macros_raw)

        remapper = Remapper(device, bindings, macros, dry_run=self.options.dry_run)
        runtime.remapper = remapper

        def run() -> None:
            try:
                remapper.run()
            except Exception as exc:  # noqa: BLE001
                logger.warning(t("перехват остановлен: %s", "interception stopped: %s"), exc)
                runtime.last_error = str(exc)

        thread = threading.Thread(target=run, daemon=True, name=f"remap-{runtime.key}")
        thread.start()
        runtime.remapper_thread = thread
        logger.info(t("%s: кнопки профиля %s активны (%s)", "%s: buttons of profile %s are active (%s)"), runtime.state.name, chosen.id, info.path)

    def _find_input(self, runtime: DeviceRuntime) -> InputDeviceInfo:
        candidates = find_pointer_devices(serial=runtime.state.serial)
        if not candidates:
            candidates = find_pointer_devices(name_hint=_name_hint(runtime.state.name))
        if not candidates:
            raise LookupError(
                t("не найден event-узел мыши для ремапа кнопок. "
                "Проверьте права на /dev/input/event* (нужно правило udev или группа input)",
                "mouse event device for button remapping was not found. "
                "Check permissions on /dev/input/event* (a udev rule or the input group is required)")
            )
        return candidates[0]

    # -- активное приложение --------------------------------------------
    def _check_focus(self) -> None:
        assert self.watcher is not None
        try:
            focused = self.watcher.focused()
        except Exception as exc:  # noqa: BLE001
            logger.debug(t("опрос фокуса упал: %s", "focus polling failed: %s"), exc)
            return
        app = focused.app_id if focused else ""
        if app == self.focused_app:
            return
        self.focused_app = app
        logger.debug(t("активное приложение: %s", "focused application: %s"), app or t("<нет>", "<none>"))
        for runtime in list(self.devices.values()):
            if runtime.profile_set is None:
                continue
            try:
                self._apply(runtime, reason="смена приложения")
            except Exception as exc:  # noqa: BLE001
                logger.warning(t("смена профиля по приложению упала: %s", "per-application profile switch failed: %s"), exc)

    # -- RPC ------------------------------------------------------------
    def _handle_rpc(self, method: str, params: dict) -> object:
        logger.debug("RPC %s %s", method, params)
        if method == "ping":
            return {"pong": True, "version": ipc.PROTOCOL_VERSION}
        if method == "status":
            return {
                "focus_watcher": getattr(self.watcher, "name", ""),
                "focused_app": self.focused_app,
                "dry_run": self.options.dry_run,
                "devices": [self._describe(runtime) for runtime in self.devices.values()],
            }
        if method == "list-devices":
            with self._lock:
                return [ipc.serialize(asdict(runtime.state)) for runtime in self.devices.values()]
        if method == "list-input-devices":
            from .input.detect import iter_input_devices

            return [ipc.serialize(info) for info in iter_input_devices()]
        if method == "list-macros":
            return {"macros": self._all_macros()}
        if method == "macro-upsert":
            return self._rpc_macro_upsert(params)
        if method == "macro-remove":
            return self._rpc_macro_remove(params)
        if method == "apply-profile":
            return self._rpc_apply(params)
        if method == "set-dpi":
            return self._rpc_set(params, "dpi")
        if method == "set-rate":
            return self._rpc_set(params, "report_rate_ms")
        if method == "set-lighting":
            return self._rpc_set_lighting(params)
        if method == "profile-activate":
            return self._rpc_activate(params)
        if method == "profile-next":
            return self._rpc_cycle_profile(params, step=1)
        if method == "profile-prev":
            return self._rpc_cycle_profile(params, step=-1)
        if method == "set-remap":
            return self._rpc_set_remap(params)
        if method == "reload":
            for runtime in self.devices.values():
                self._load_profile_set(runtime)
                self._apply(runtime, reason="перезагрузка профилей")
            return {"reloaded": len(self.devices)}
        if method == "profile-upsert":
            return self._rpc_profile_upsert(params)
        if method == "profile-remove":
            return self._rpc_profile_remove(params)
        if method == "profile-export":
            return self._rpc_profile_export(params)
        raise ipc.IpcError(t(f"неизвестный метод {method!r}", f"unknown method {method!r}"))

    def _describe(self, runtime: DeviceRuntime) -> dict:
        data = ipc.serialize(asdict(runtime.state))
        data["last_error"] = runtime.last_error
        data["remap_active"] = runtime.remapper is not None
        data["remap_enabled"] = not runtime.remap_disabled
        data["profiles"] = runtime.profile_ids
        data["profile_details"] = self._profile_details(runtime)
        data["dpi"] = runtime.dpi
        data["report_rate_ms"] = runtime.report_rate_ms
        data["lighting_supported"] = self._lighting_supported(runtime)
        if runtime.active_profile is not None:
            data["buttons_plan"] = engine.plan_button_actions(runtime.active_profile)
        return data

    def _lighting_supported(self, runtime: DeviceRuntime) -> bool:
        """Есть ли у устройства фича подсветки (без открытия hidraw каждый тик).

        Наличие фичи определяется один раз при подключении. Значение храним
        в state, чтобы `status` был дешёвым.
        """
        return bool(getattr(runtime.state, "lighting_supported", False))

    def _profile_details(self, runtime: DeviceRuntime) -> list[dict]:
        if runtime.profile_set is None:
            return []
        return [
            {
                "id": profile.id,
                "name": profile.name,
                "dpi": profile.dpi,
                "report_rate_ms": profile.report_rate_ms,
                "buttons": ipc.serialize(profile.buttons),
                "match_apps": list(profile.match_apps),
                "match_serials": list(profile.match_serials),
                "lighting": ipc.serialize(profile.lighting),
            }
            for profile in runtime.profile_set.profiles
        ]

    def _rpc_apply(self, params: dict) -> dict:
        key = str(params.get("device", ""))
        profile_id = str(params.get("profile", ""))
        with self._lock:
            runtime = self.devices.get(key)
        if runtime is None:
            raise ipc.IpcError(t(f"устройство {key!r} не подключено", f"device {key!r} is not connected"))
        if runtime.profile_set is None:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет файла профилей {runtime.state.profiles_file}", f"{runtime.state.name}: no profiles file {runtime.state.profiles_file}"))

        chosen = next((item for item in runtime.profile_set.profiles if item.id == profile_id), None)
        if chosen is None:
            raise ipc.IpcError(t(f"профиль {profile_id!r} не найден в {runtime.state.profiles_file}", f"profile {profile_id!r} not found in {runtime.state.profiles_file}"))

        with session.open_device(
            runtime.state.path,
            allow_onboard_switch=self._allow_switch_for(runtime.key),
            dry_run=self.options.dry_run,
        ) as opened:
            result = engine.apply_profile(opened.device, chosen)
        runtime.active_profile = chosen
        runtime.state.active_profile = chosen.id
        runtime.state.pinned_profile = chosen.id
        if "dpi" in result.applied:
            runtime.dpi = int(result.applied["dpi"])
        if "report_rate_ms" in result.applied:
            runtime.report_rate_ms = int(result.applied["report_rate_ms"])
        registry.save_state(self.state)
        if self.options.apply_buttons:
            self._restart_remap(runtime)
        self.server.publish({"type": "profile-applied", "device": key, "result": result.describe()})
        return {"result": result.describe(), "applied": ipc.serialize(result.applied)}

    def _rpc_set(self, params: dict, what: str) -> dict:
        key = str(params.get("device", ""))
        value = int(params["value"])
        with self._lock:
            runtime = self.devices.get(key)
        if runtime is None:
            raise ipc.IpcError(t(f"устройство {key!r} не подключено", f"device {key!r} is not connected"))

        with session.open_device(
            runtime.state.path,
            allow_onboard_switch=self._allow_switch_for(runtime.key),
            dry_run=self.options.dry_run,
        ) as opened:
            if what == "dpi":
                opened.device.set_dpi(value)
                read_back = opened.device.get_dpi()
            else:
                opened.device.set_rate_ms(value)
                read_back = opened.device.get_rate_ms()
        return {what: read_back, "requested": value}

    def _rpc_set_remap(self, params: dict) -> dict:
        """Включить/выключить ремап кнопок (для трея)."""
        runtime = self._runtime_or_error(str(params.get("device", "")))
        enabled = bool(params.get("enabled", True))
        runtime.remap_disabled = not enabled
        if enabled:
            self._start_remap(runtime)
        else:
            runtime.stop()
        return {"remap_active": runtime.remapper is not None, "enabled": enabled}

    def _rpc_set_lighting(self, params: dict) -> dict:
        """Включить подсветку волатильно (fn 0x10; NvConfig не трогается)."""
        runtime = self._runtime_or_error(str(params.get("device", "")))
        effect = str(params.get("effect", "static")).lower()
        params_block = {
            "effect": effect,
            "zone": int(params.get("zone", 0)),
        }
        for key in ("color", "brightness", "period", "intensity", "form", "ramp", "speed", "direction"):
            if params.get(key) is not None:
                params_block[key] = params[key]
        profiles.validate_lighting(params_block)

        from .hidpp.device import LIGHTING_EFFECTS

        effects_by_name = {name: effect_id for effect_id, (name, _) in LIGHTING_EFFECTS.items()}
        effect_id = effects_by_name.get(effect)
        if effect_id is None:
            raise ipc.IpcError(t(f"эффект подсветки {effect!r} неизвестен", f"unknown lighting effect {effect!r}"))

        schema = LIGHTING_EFFECTS[effect_id][1]
        device_params: dict[str, int] = {}
        if "color" in params_block and "color" in schema:
            device_params["color"] = profiles.parse_color(params_block["color"]) or 0
        brightness = params_block.get("brightness")
        if brightness is not None:
            brightness = int(brightness)
            if "intensity" in schema:
                device_params["intensity"] = max(0, min(255, round(brightness * 255 / 100)))
            elif effect == "static" and "color" in schema:
                # У static яркости как байта нет: притемняем сам цвет.
                from .engine import _scale_color

                device_params["color"] = _scale_color(
                    device_params.get("color", 0xFFFFFF), brightness
                )
        for key in ("period", "intensity", "form", "ramp", "speed", "direction"):
            if key in params_block and key in schema:
                device_params[key] = int(params_block[key])

        with session.open_device(
            runtime.state.path,
            allow_onboard_switch=self._allow_switch_for(runtime.key),
            dry_run=self.options.dry_run,
        ) as opened:
            slot = opened.device.set_lighting(effect_id, device_params, zone=params_block["zone"])
        return {"effect": effect, "slot": slot, "zone": params_block["zone"], "params": device_params}

    def _rpc_activate(self, params: dict) -> dict:
        """Переключить устройство на профиль по id (для хоткея/трея)."""
        runtime = self._runtime_or_error(str(params.get("device", "")))
        profile_id = str(params.get("profile", ""))
        if runtime.profile_set is None:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет файла профилей {runtime.state.profiles_file}", f"{runtime.state.name}: no profiles file {runtime.state.profiles_file}"))
        chosen = next((item for item in runtime.profile_set.profiles if item.id == profile_id), None)
        if chosen is None:
            raise ipc.IpcError(t(f"профиль {profile_id!r} не найден", f"profile {profile_id!r} not found"))

        with session.open_device(
            runtime.state.path,
            allow_onboard_switch=self._allow_switch_for(runtime.key),
            dry_run=self.options.dry_run,
        ) as opened:
            result = engine.apply_profile(opened.device, chosen)
        runtime.active_profile = chosen
        runtime.state.active_profile = chosen.id
        runtime.state.pinned_profile = chosen.id
        if "dpi" in result.applied:
            runtime.dpi = int(result.applied["dpi"])
        if "report_rate_ms" in result.applied:
            runtime.report_rate_ms = int(result.applied["report_rate_ms"])
        registry.save_state(self.state)
        if self.options.apply_buttons:
            self._restart_remap(runtime)
        self.server.publish({"type": "profile-applied", "device": runtime.key, "result": result.describe()})
        return {"active_profile": chosen.id, "result": result.describe()}

    def _rpc_cycle_profile(self, params: dict, *, step: int) -> dict:
        """Следующий/предыдущий профиль устройства (листание по кругу)."""
        runtime = self._runtime_or_error(str(params.get("device", "")))
        profile_ids = runtime.profile_ids
        if not profile_ids:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет профилей", f"{runtime.state.name}: no profiles"))
        current = runtime.state.active_profile or profile_ids[0]
        index = (
            (profile_ids.index(current) + step) % len(profile_ids) if current in profile_ids else 0
        )
        return self._rpc_activate({"device": runtime.key, "profile": profile_ids[index]})


    def _all_macros(self) -> list[dict]:
        result: list[dict] = []
        for runtime in self.devices.values():
            if runtime.profile_set is None:
                continue
            for macro in parse_macros(runtime.profile_set.macros_raw).values():
                result.append(
                    {
                        "id": macro.id,
                        "name": macro.name,
                        "describe": macro.describe(),
                        "steps": [step.describe() for step in macro.steps],
                    }
                )
        return result

    def _rpc_macro_upsert(self, params: dict) -> dict:
        """Добавить или заменить макрос в файле профилей устройства.

        Макрос — это данные на хосте; в мышь ничего не пишется. Шаги приходят
        как список словарей {'tap'|'key'|'wait', 'delay_ms'} — ровно тот вид,
        который отдаёт запись макроса в GUI.
        """
        runtime = self._runtime_or_error(str(params.get("device", "")))
        if runtime.profile_set is None:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет файла профилей {runtime.state.profiles_file}", f"{runtime.state.name}: no profiles file {runtime.state.profiles_file}"))
        raw = {
            "id": str(params["id"]),
            "name": str(params.get("name", "")),
            "interval_ms": int(params.get("interval_ms") or 40),
            "steps": list(params.get("steps") or []),
        }
        if not raw["steps"]:
            raise ipc.IpcError(t("макрос без шагов не сохраняется", "a macro without steps is not saved"))
        try:
            parse_macros([raw])  # проверяем до записи
        except (ActionError, ValueError) as exc:
            raise ipc.IpcError(str(exc)) from exc

        path = Path(runtime.state.profiles_file)
        try:
            replaced = profiles.upsert_macro(path, raw, replace=bool(params.get("replace", True)))
        except profiles.ProfileError as exc:
            raise ipc.IpcError(str(exc)) from exc
        self._load_profile_set(runtime)
        return {"id": raw["id"], "replaced": replaced, "path": str(path)}

    def _rpc_macro_remove(self, params: dict) -> dict:
        runtime = self._runtime_or_error(str(params.get("device", "")))
        if runtime.profile_set is None:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет файла профилей {runtime.state.profiles_file}", f"{runtime.state.name}: no profiles file {runtime.state.profiles_file}"))
        path = Path(runtime.state.profiles_file)
        try:
            profiles.remove_macro(path, str(params["id"]))
        except (profiles.ProfileError, OSError) as exc:
            raise ipc.IpcError(str(exc)) from exc
        self._load_profile_set(runtime)
        return {"removed": str(params["id"]), "path": str(path)}

    # -- редактирование профилей ----------------------------------------
    def _runtime_or_error(self, key: str) -> DeviceRuntime:
        with self._lock:
            runtime = self.devices.get(key)
        if runtime is None:
            raise ipc.IpcError(t(f"устройство {key!r} не подключено", f"device {key!r} is not connected"))
        return runtime

    def _rpc_profile_upsert(self, params: dict) -> dict:
        """Добавить или заменить профиль прямо в файле профилей устройства.

        Ничего не пишется в мышь: меняется только TOML на хосте. После
        записи файл перечитывается и активный профиль применяется заново.
        """
        runtime = self._runtime_or_error(str(params.get("device", "")))
        if runtime.profile_set is None:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет файла профилей {runtime.state.profiles_file}", f"{runtime.state.name}: no profiles file {runtime.state.profiles_file}"))

        profile = profiles.Profile(
            id=str(params["id"]),
            name=str(params.get("name", "")),
            dpi=int(params["dpi"]) if params.get("dpi") is not None else None,
            report_rate_ms=(
                profiles.parse_rate(params["report_rate"]) if params.get("report_rate") is not None else None
            ),
            buttons=dict(params.get("buttons") or {}),
            match_apps=[str(item) for item in params.get("match_apps") or []],
            match_serials=[str(item) for item in params.get("match_serials") or []],
            lighting=profiles.parse_lighting(params.get("lighting")) if params.get("lighting") else None,
        )

        path = Path(runtime.state.profiles_file)
        try:
            replaced = profiles.upsert_profile(path, profile)
        except profiles.ProfileError as exc:
            raise ipc.IpcError(str(exc)) from exc

        self._load_profile_set(runtime)
        self._apply(runtime, reason="правка профиля")
        return {"id": profile.id, "replaced": replaced, "path": str(path)}

    def _rpc_profile_remove(self, params: dict) -> dict:
        runtime = self._runtime_or_error(str(params.get("device", "")))
        if runtime.profile_set is None:
            raise ipc.IpcError(t(f"у {runtime.state.name}: нет файла профилей {runtime.state.profiles_file}", f"{runtime.state.name}: no profiles file {runtime.state.profiles_file}"))
        path = Path(runtime.state.profiles_file)
        try:
            profiles.remove_profile(path, str(params["id"]))
        except (profiles.ProfileError, OSError) as exc:
            raise ipc.IpcError(str(exc)) from exc
        self._load_profile_set(runtime)
        self._apply(runtime, reason="удаление профиля")
        return {"removed": str(params["id"]), "path": str(path)}

    def _rpc_profile_export(self, params: dict) -> dict:
        """Отдать содержимое файла профилей (для резервной копии/просмотра)."""
        runtime = self._runtime_or_error(str(params.get("device", "")))
        path = Path(runtime.state.profiles_file)
        if not path.exists():
            raise ipc.IpcError(t(f"файл профилей не найден: {path}", f"profiles file not found: {path}"))
        return {"path": str(path), "text": path.read_text(encoding="utf-8")}


def _slug(name: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in name)
    return "-".join(part for part in cleaned.split("-") if part) or "device"


def _name_hint(name: str) -> str:
    """Из «Logitech G102 LIGHTSYNC Gaming Mouse» сделать «G102»."""
    for token in name.split():
        if any(char.isdigit() for char in token):
            return token
    return name
