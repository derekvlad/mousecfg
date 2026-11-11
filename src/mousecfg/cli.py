"""Командный интерфейс mousecfg."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from pathlib import Path

from . import __version__, config, discovery, engine, ipc, profiles, session
from . import daemon as daemon_module
from .hidpp import guard
from .hidpp.protocol import DeviceError, FeatureUnsupportedError, ProtocolError, TransportError
from .input import detect as input_detect

LOG = logging.getLogger("mousecfg")


def _add_profile_edit_args(parser: argparse.ArgumentParser, *, require_id: bool) -> None:
    """Общие аргументы для `profile add` и `profile set`."""
    parser.add_argument("--profile-file", default=None, help="путь к .toml (по умолчанию — файл профилей устройства)")
    parser.add_argument("--id", required=require_id, help="id профиля")
    parser.add_argument("--name", default="", help="человекочитаемое имя")
    parser.add_argument("--dpi", type=int, default=None)
    parser.add_argument("--rate", default=None, help="'1000Hz', '1ms' или число")
    parser.add_argument(
        "--button",
        action="append",
        default=[],
        metavar="BUTTON=ACTION",
        help="назначение кнопки, например side1=browser-back или side2=macro:burst (можно повторять)",
    )
    parser.add_argument("--match-app", action="append", default=[], metavar="APP", help="привязать профиль к приложению")
    parser.add_argument("--match-serial", action="append", default=[], metavar="SERIAL", help="привязать профиль к серийнику")
    parser.add_argument("--lighting-effect", default=None, help="эффект подсветки: static, breathe, cycle, wave, disabled")
    parser.add_argument("--lighting-color", default=None, help="цвет подсветки: '#rrggbb' или имя (red, blue...)")
    parser.add_argument("--lighting-brightness", type=int, default=None, help="яркость подсветки 0..100")
    parser.add_argument("--lighting-period", type=int, default=None, help="период эффекта, мс")


def _build_parser() -> argparse.ArgumentParser:
    # Общие ключи доступны и после подкоманды: `mousecfg mode host --allow-onboard-switch`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0, help="подробный вывод (-vv — отладка)")
    common.add_argument("--device", metavar="HINT", help="путь к hidraw, vid:pid, serial или часть имени")
    common.add_argument("--json", action="store_true", help="машинный вывод")
    common.add_argument("--dry-run", action="store_true", help="не отправлять кадры, только показать их")
    common.add_argument(
        "--allow-onboard-switch",
        action="store_true",
        help="разрешить однократный переход устройства из On-Board в Host mode",
    )

    parser = argparse.ArgumentParser(
        prog="mousecfg",
        parents=[common],
        description="Профили игровых мышей на стороне хоста. Во встроенную память мыши ничего не пишется.",
    )
    parser.add_argument("--version", action="version", version=f"mousecfg {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", parents=[common], help="показать найденные HID++ устройства")
    sub.add_parser("judge", parents=[common], help="показать политику: что разрешено, что запрещено")
    sub.add_parser("get", parents=[common], help="прочитать имя, прошивку, DPI, report rate, режим")

    dpi = sub.add_parser("set-dpi", parents=[common], help="установить DPI (волатильно)")
    dpi.add_argument("dpi", type=int)
    dpi.add_argument("--sensor", type=int, default=0)

    rate = sub.add_parser("set-rate", parents=[common], help="установить частоту опроса (волатильно)")
    rate.add_argument("rate", help="'1000Hz', '1ms' или число")

    mode = sub.add_parser("mode", parents=[common], help="режим устройства: показать или переключить")
    mode.add_argument("value", nargs="?", choices=["host", "onboard"], help="без значения — только показать")

    light = sub.add_parser("lighting", parents=[common], help="подсветка мыши: показать зоны и эффекты")
    light.add_argument("--effect", help="включить эффект волатильно (static, breathe, cycle, wave, disabled...)")
    light.add_argument("--color", help="цвет: '#rrggbb', 'red' или 0xRRGGBB")
    light.add_argument("--brightness", type=int, help="яркость 0..100")
    light.add_argument("--period", type=int, help="период эффекта, мс")
    light.add_argument("--intensity", type=int, help="интенсивность 0..255")
    light.add_argument("--form", type=int, help="форма волны (для breathe)")
    light.add_argument("--zone", type=int, default=0, help="номер зоны подсветки")

    validate = sub.add_parser("profiles-check", parents=[common], help="проверить TOML-профили без устройства")
    validate.add_argument("path", nargs="?", default=None, help="файл или каталог (по умолчанию ~/.config/mousecfg/profiles)")

    profile = sub.add_parser("profile", parents=[common], help="создать, изменить или удалить профиль (файл на хосте)")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    profile_add = profile_sub.add_parser("add", parents=[common], help="добавить профиль")
    _add_profile_edit_args(profile_add, require_id=True)

    profile_set = profile_sub.add_parser("set", parents=[common], help="изменить существующий профиль")
    _add_profile_edit_args(profile_set, require_id=True)

    profile_rm = profile_sub.add_parser("remove", parents=[common], help="удалить профиль")
    profile_rm.add_argument("id", help="id профиля")
    profile_rm.add_argument("--profile-file", default=None, help="путь к .toml (по умолчанию — файл профилей устройства)")

    profile_show = profile_sub.add_parser("show", parents=[common], help="показать профили файла")
    profile_show.add_argument("id", nargs="?", help="id профиля (без него — все)")
    profile_show.add_argument("--profile-file", default=None, help="путь к .toml (по умолчанию — файл профилей устройства)")

    profile_activate = profile_sub.add_parser(
        "activate", parents=[common], help="переключить устройство на профиль (через демон; для хоткеев)"
    )
    profile_activate.add_argument("id", help="id профиля")

    profile_next = profile_sub.add_parser("next", parents=[common], help="следующий профиль по кругу")
    profile_next.add_argument("--prev", action="store_true", help="предыдущий вместо следующего")

    macro = sub.add_parser("macro", parents=[common], help="список и добавление макросов (исполняются на хосте)")
    macro_sub = macro.add_subparsers(dest="macro_command", required=True)

    macro_list = macro_sub.add_parser("list", parents=[common], help="показать макросы файла")
    macro_list.add_argument("--profile-file", default=None)

    macro_add = macro_sub.add_parser("add", parents=[common], help="добавить макрос")
    macro_add.add_argument("--profile-file", default=None, help="путь к .toml (по умолчанию — файл профилей устройства)")
    macro_add.add_argument("--id", required=True, help="id макроса")
    macro_add.add_argument("--name", default="")
    macro_add.add_argument("--interval-ms", type=int, default=40, help="пауза между шагами, мс")
    macro_add.add_argument(
        "--step",
        action="append",
        default=[],
        metavar="SPEC",
        help="шаг: 'ctrl+c', 'wait:120' или 'tap:enter:50' (можно повторять)",
    )
    macro_add.add_argument("--replace", action="store_true", help="заменить макрос с тем же id")

    apply_parser = sub.add_parser("apply", parents=[common], help="применить профиль из файла (волатильно)")
    apply_parser.add_argument("path", help="файл .toml с профилями")
    apply_parser.add_argument("--profile", help="id профиля (по умолчанию первый подходящий)")
    apply_parser.add_argument("--app", default="", help="имя активного приложения для match_apps")

    sub.add_parser("status", parents=[common], help="состояние демона: устройства, активное приложение")
    sub.add_parser("devices", parents=[common], help="устройства, которые видит демон")
    sub.add_parser("inputs", parents=[common], help="evdev-узлы мышей: где есть кнопки для ремапа")

    daemon = sub.add_parser("daemon", parents=[common], help="запустить демон на переднем плане")
    daemon.add_argument("--profiles-dir", default=None)
    daemon.add_argument("--no-buttons", action="store_true")
    daemon.add_argument("--no-apps", action="store_true")

    run_profile = sub.add_parser("run-profile", parents=[common], help="включить ремап кнопок профиля (без демона)")
    run_profile.add_argument("path", help="файл .toml с профилями")
    run_profile.add_argument("--profile", help="id профиля")
    run_profile.add_argument("--input", dest="input_path", help="event-узел мыши (по умолчанию — поиск автоматически)")
    run_profile.add_argument("--seconds", type=float, default=0.0, help="остановиться через N секунд (0 — до Ctrl+C)")

    return parser


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def _print_human(value: object) -> None:
    print(value)


def _dump(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for key, value in data.items():
        if isinstance(value, list):
            print(f"{key}:")
            for item in value:
                print(f"  - {item}")
        else:
            print(f"{key}: {value}")


def cmd_list(args: argparse.Namespace) -> int:
    nodes = discovery.discover(include_all=bool(args.verbose))
    if args.json:
        print(json.dumps([node.__dict__ for node in nodes], ensure_ascii=False, indent=2))
        return 0
    if not nodes:
        print("Устройства Logitech не найдены.")
        return 1
    print("Logitech HID++ устройства:")
    for node in nodes:
        marks = []
        if node.is_hidpp_candidate:
            marks.append("HID++ if01")
        if not marks:
            marks.append("не HID++ кандидат")
        print(f"  {node.describe()}  ({', '.join(marks)})")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    audit = guard.audit()
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0
    titles = {
        "volatile": "ВОЛАТИЛЬНО — можно применять (живёт в RAM до отключения мыши)",
        "persistent-consented": "ПЕРСИСТЕНТНО, С СОГЛАСИЯ — один раз, по явному разрешению",
        "persistent": "ЗАПРЕЩЕНО — пишет в энергонезависимую память устройства",
    }
    for key in ("volatile", "persistent-consented", "persistent"):
        print(f"\n{titles[key]}")
        for line in audit.get(key, []):
            print(f"  {line}")
    print("\nВсё остальное считается запрещённым: неизвестная команда не отправится никогда.")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        device = opened.device
        data: dict = {"device": opened.node.describe(), "serial": opened.node.serial}
        info = device.info()
        data["name"] = info.name
        data["kind"] = info.kind
        if info.protocol_version:
            data["protocol"] = f"HID++ {info.protocol_version[0]}.{info.protocol_version[1]}"
        data["firmware"] = info.firmware

        try:
            data["dpi"] = device.get_dpi()
            data["dpi_default"] = device.get_dpi_default()
            data["sensors"] = device.get_sensor_count()
        except (FeatureUnsupportedError, DeviceError) as exc:
            data["dpi"] = f"недоступно: {exc}"

        try:
            data["report_rate_ms"] = device.get_rate_ms()
            data["report_rate_supported_ms"] = device.supported_rates_ms()
        except (FeatureUnsupportedError, DeviceError) as exc:
            data["report_rate_ms"] = f"недоступно: {exc}"

        if device.has_feature(0x8100):
            data["mode"] = device.get_mode()
        else:
            data["mode"] = "фича onboard отсутствует"

        data["features"] = sorted(f"{feature:#06x}" for feature in device.discover_features())
        _dump(data, args.json)
    return 0


def cmd_set_dpi(args: argparse.Namespace) -> int:
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        before = opened.device.get_dpi(args.sensor)
        opened.device.set_dpi(args.dpi, args.sensor)
        if args.dry_run:
            _dump(
                {
                    "dpi_before": before,
                    "dpi_requested": args.dpi,
                    "sent": False,
                    "note": "[dry-run] кадр не отправлен: команда меняет состояние устройства",
                },
                args.json,
            )
            return 0
        after = opened.device.get_dpi(args.sensor)
        mode = opened.device.get_mode() if opened.device.has_feature(0x8100) else "?"
        _dump(
            {
                "dpi_before": before,
                "dpi_requested": args.dpi,
                "dpi_after": after,
                "mode": mode,
                "note": "значение волатильно: пропадёт при отключении мыши, демон применит его заново",
            },
            args.json,
        )
    return 0


def cmd_set_rate(args: argparse.Namespace) -> int:
    milliseconds = profiles.parse_rate(args.rate)
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        before = opened.device.get_rate_ms()
        opened.device.set_rate_ms(milliseconds)
        if args.dry_run:
            _dump(
                {
                    "rate_ms_before": before,
                    "rate_ms_requested": milliseconds,
                    "sent": False,
                    "note": "[dry-run] кадр не отправлен",
                },
                args.json,
            )
            return 0
        after = opened.device.get_rate_ms()
        _dump(
            {
                "rate_ms_before": before,
                "rate_ms_requested": milliseconds,
                "rate_ms_after": after,
                "supported_ms": opened.device.supported_rates_ms(),
            },
            args.json,
        )
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        device = opened.device
        current = device.get_mode()
        if args.value is None:
            _dump({"mode": current}, args.json)
            return 0

        if args.value == "host":
            if current == "host":
                _dump({"mode": current, "changed": False}, args.json)
                return 0
            if not args.allow_onboard_switch:
                print(
                    "Переход On-Board -> Host меняет конфигурацию устройства.\n"
                    "Это единственная потенциально персистентная операция в проекте.\n"
                    "Повторите с --allow-onboard-switch, если согласны.",
                    file=sys.stderr,
                )
                return 2
            device.set_host_mode()
            if args.dry_run:
                _dump(
                    {
                        "mode_before": current,
                        "sent": False,
                        "note": "[dry-run] переход не отправлен",
                    },
                    args.json,
                )
                return 0
            new_mode = device.get_mode()
            _dump(
                {
                    "mode_before": current,
                    "mode_after": new_mode,
                    "changed": new_mode != current,
                    "note": "переход выполнен один раз; дальше все настройки только в RAM",
                },
                args.json,
            )
            return 0

        print(
            "Возврат в On-Board mode проектом не поддерживается: он подразумевает запись профиля "
            "во встроенную память мыши.",
            file=sys.stderr,
        )
        return 2


def cmd_lighting(args: argparse.Namespace) -> int:
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        device = opened.device
        info = device.lighting_info()
        if not info.supported:
            print("Устройство не поддерживает подсветку RGB_EFFECTS (0x8071).", file=sys.stderr)
            return 1

        if args.effect is None:
            data: dict = {
                "zones": [
                    {
                        "index": zone.index,
                        "location": zone.location_name,
                        "effects": [
                            {
                                "id": f"{effect.effect_id:#04x}",
                                "name": effect.name,
                                "period": effect.period,
                            }
                            for effect in zone.effects
                        ],
                    }
                    for zone in info.zones
                ],
                "readable": info.readable,
                "note": "запись волатильна: подсветка не сохраняется в память мыши",
            }
            _dump(data, args.json)
            return 0

        effect_name = args.effect.lower()
        known_effects = {effect.name: effect.effect_id for zone in info.zones for effect in zone.effects}
        # устройство может знать больше эффектов, чем перечислено: пробуем по таблице
        effect_id = known_effects.get(effect_name)
        if effect_id is None:
            from .hidpp.device import LIGHTING_EFFECTS

            effect_id = next((eid for eid, (name, _) in LIGHTING_EFFECTS.items() if name == effect_name), None)
        if effect_id is None:
            print(
                f"Эффект {effect_name!r} неизвестен. Доступные: {', '.join(sorted(known_effects)) or 'нет'}",
                file=sys.stderr,
            )
            return 1

        params: dict[str, int] = {}
        if args.color is not None:
            params["color"] = profiles.parse_color(args.color) or 0
        if args.period is not None:
            params["period"] = args.period
        if args.intensity is not None:
            params["intensity"] = args.intensity
        if args.form is not None:
            params["form"] = args.form
        if args.brightness is not None:
            if not 0 <= args.brightness <= 100:
                print("Яркость должна быть 0..100", file=sys.stderr)
                return 1
            params["intensity"] = max(0, min(255, round(args.brightness * 255 / 100)))

        try:
            slot = device.set_lighting(effect_id, params, zone=args.zone)
        except (ProtocolError, ValueError) as exc:
            print(f"Не удалось включить подсветку: {exc}", file=sys.stderr)
            return 1

        if args.dry_run:
            _dump(
                {
                    "effect": effect_name,
                    "slot": slot,
                    "zone": args.zone,
                    "sent": False,
                    "note": "[dry-run] кадр не отправлен",
                },
                args.json,
            )
            return 0
        _dump(
            {
                "effect": effect_name,
                "slot": slot,
                "zone": args.zone,
                "params": params,
                "note": "волатильно: пропадёт при отключении мыши, демон применит заново",
            },
            args.json,
        )
    return 0


def cmd_profiles_check(args: argparse.Namespace) -> int:
    from pathlib import Path

    path = Path(args.path).expanduser() if args.path else config.profiles_dir()
    if path.is_dir():
        sets = profiles.load_dir(path)
    elif path.exists():
        sets = [profiles.load_profile_file(path)]
    else:
        print(f"Путь не найден: {path}", file=sys.stderr)
        return 1

    if not sets:
        print(f"В {path} нет файлов профилей (*.toml).")
        return 1

    for profile_set in sets:
        ids = ", ".join(profile.id for profile in profile_set.profiles)
        print(f"{profile_set.device or '<без устройства>'}: {ids}")
    print("Схема корректна: профили живут на хосте, storage=onboard отсутствует.")
    return 0


def _parse_button_spec(spec: str) -> tuple[str, object]:
    """Разобрать 'side1=browser-back' или 'side2=macro:burst'."""
    if "=" not in spec:
        raise ValueError(f"назначение кнопки должно быть BUTTON=ACTION, получено {spec!r}")
    name, _, action = spec.partition("=")
    name = name.strip()
    action = action.strip()
    if not name or not action:
        raise ValueError(f"пустое имя или действие в {spec!r}")
    if action.startswith("macro:"):
        return name, {"macro": action[len("macro:") :].strip()}
    if action.startswith("key:"):
        return name, {"key": action[len("key:") :].strip()}
    return name, action


def _collect_buttons(specs: list[str]) -> dict[str, object]:
    buttons: dict[str, object] = {}
    for spec in specs:
        name, action = _parse_button_spec(spec)
        buttons[name] = action
    return buttons


def _collect_lighting(args: argparse.Namespace) -> dict[str, object] | None:
    """Собрать блок подсветки из ключей --lighting-* (если они заданы)."""
    effect = getattr(args, "lighting_effect", None)
    color = getattr(args, "lighting_color", None)
    brightness = getattr(args, "lighting_brightness", None)
    period = getattr(args, "lighting_period", None)
    if effect is None and color is None and brightness is None and period is None:
        return None
    lighting: dict[str, object] = {}
    if effect is not None:
        lighting["effect"] = str(effect).lower()
    elif color is not None:
        lighting["effect"] = "static"
    if color is not None:
        lighting["color"] = profiles.parse_color(color)
    if brightness is not None:
        lighting["brightness"] = int(brightness)
    if period is not None:
        lighting["period"] = int(period)
    if not lighting:
        return None
    profiles.validate_lighting(lighting)
    return lighting



def cmd_profile_add(args: argparse.Namespace) -> int:
    return _profile_write(args, replace=False)


def cmd_profile_set(args: argparse.Namespace) -> int:
    return _profile_write(args, replace=True)


def _profile_write(args: argparse.Namespace, *, replace: bool) -> int:
    from pathlib import Path

    explicit_file = bool(args.profile_file)
    path = Path(args.profile_file).expanduser() if explicit_file else _device_profiles_file(args)
    if path is None:
        return 1

    try:
        buttons = _collect_buttons(args.button)
        lighting = _collect_lighting(args)
        profile = profiles.Profile(
            id=str(args.id),
            name=str(args.name),
            dpi=int(args.dpi) if args.dpi is not None else None,
            report_rate_ms=profiles.parse_rate(args.rate) if args.rate else None,
            buttons=buttons,
            match_apps=list(args.match_app),
            match_serials=list(args.match_serial),
            lighting=lighting,
        )
        profile.validate()
    except (profiles.ProfileError, ValueError) as exc:
        print(f"Ошибка в профиле: {exc}", file=sys.stderr)
        return 1

    # Если указан конкретный файл — правим только его, без участия демона:
    # демон работал бы с файлом устройства, а не с этим путём.
    client = ipc.Client()
    if not explicit_file and client.is_available():
        try:
            result = client.call(
                "profile-upsert",
                device=_resolve_device_key(getattr(args, "device", None)),
                id=profile.id,
                name=profile.name,
                dpi=profile.dpi,
                report_rate=profile.report_rate_ms,
                buttons=buttons,
                match_apps=profile.match_apps,
                match_serials=profile.match_serials,
                lighting=lighting,
            )
        except ipc.IpcError as exc:
            print(f"Демон отказал: {exc}", file=sys.stderr)
            _macro_hint(exc, buttons)
            return 1
        verb = "изменён" if result.get("replaced") else "добавлен"
        print(f"Профиль {profile.id!r} {verb} в {result.get('path')}")
        return 0

    # Демон не запущен (или задан явный файл) — правим файл напрямую.
    try:
        if replace:
            replaced = profiles.upsert_profile(path, profile)
            verb = "изменён" if replaced else "добавлен"
        else:
            profiles.add_profile(path, profile)
            verb = "добавлен"
    except profiles.ProfileError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        _macro_hint(exc, buttons)
        return 1
    print(f"Профиль {profile.id!r} {verb} в {path}")
    return 0


def _macro_hint(error: Exception, buttons: dict[str, object]) -> None:
    """Если ошибка про макрос — подсказать, как его добавить."""
    text = str(error)
    if "макрос" not in text and "macro" not in text:
        return
    referenced = [
        str(action.get("macro"))
        for action in buttons.values()
        if isinstance(action, dict) and action.get("macro")
    ]
    if referenced:
        print(
            "Подсказка: макросы описываются в том же файле блоком [[macro]] с steps. "
            f"Сейчас профиль ссылается на: {', '.join(referenced)}.",
            file=sys.stderr,
        )


def cmd_profile_remove(args: argparse.Namespace) -> int:
    from pathlib import Path

    explicit_file = bool(args.profile_file)
    path = Path(args.profile_file).expanduser() if explicit_file else _device_profiles_file(args)
    if path is None:
        return 1

    client = ipc.Client()
    if not explicit_file and client.is_available():
        try:
            result = client.call("profile-remove", device=_resolve_device_key(getattr(args, "device", None)), id=args.id)
        except ipc.IpcError as exc:
            print(f"Демон отказал: {exc}", file=sys.stderr)
            return 1
        print(f"Профиль {args.id!r} удалён из {result.get('path')}")
        return 0

    try:
        profiles.remove_profile(path, args.id)
    except profiles.ProfileError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Профиль {args.id!r} удалён из {path}")
    return 0


def cmd_profile_show(args: argparse.Namespace) -> int:
    from pathlib import Path

    path = Path(args.profile_file).expanduser() if args.profile_file else _device_profiles_file(args)
    if path is None:
        return 1
    if not path.exists():
        print(f"Файла профилей нет: {path}", file=sys.stderr)
        return 1

    try:
        profile_set = profiles.load_profile_file(path)
    except profiles.ProfileError as exc:
        print(f"Ошибка разбора: {exc}", file=sys.stderr)
        return 1

    selected = profile_set.profiles
    if args.id:
        selected = [item for item in selected if item.id == args.id]
        if not selected:
            print(f"Профиль {args.id!r} не найден", file=sys.stderr)
            return 1

    if args.json:
        payload = [
            {
                "id": item.id,
                "name": item.name,
                "dpi": item.dpi,
                "report_rate_ms": item.report_rate_ms,
                "buttons": item.buttons,
                "match_apps": item.match_apps,
                "match_serials": item.match_serials,
                "lighting": item.lighting,
            }
            for item in selected
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Файл: {path}")
    for item in selected:
        print(f"\n[{item.id}] {item.name or ''}".rstrip())
        if item.dpi:
            print(f"  dpi: {item.dpi}")
        if item.report_rate_ms:
            print(f"  report_rate: {item.report_rate_ms} мс")
        if item.match_apps:
            print(f"  match_apps: {', '.join(item.match_apps)}")
        if item.match_serials:
            print(f"  match_serials: {', '.join(item.match_serials)}")
        if item.lighting:
            bits = [str(item.lighting.get("effect", "static"))]
            if item.lighting.get("color") is not None:
                bits.append(f'#{int(item.lighting["color"]):06x}')
            if item.lighting.get("brightness") is not None:
                bits.append(f'{item.lighting["brightness"]}%')
            print(f"  подсветка: {', '.join(bits)}")
        for name, action in item.buttons.items():
            print(f"  кнопка {name}: {action}")
    return 0


def cmd_profile_activate(args: argparse.Namespace) -> int:
    """Переключить профиль через демон (используется хоткеем и треем)."""
    client = ipc.Client()
    if not client.is_available():
        print("Демон не запущен: активация профиля доступна только через него.", file=sys.stderr)
        return 1
    try:
        result = client.call(
            "profile-activate",
            device=_resolve_device_key(getattr(args, "device", None)),
            profile=str(args.id),
        )
    except ipc.IpcError as exc:
        print(f"Демон отказал: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(result), ensure_ascii=False, indent=2))
    else:
        print(f"Активный профиль: {result.get('active_profile')} — {result.get('result')}")
    return 0


def cmd_profile_next(args: argparse.Namespace) -> int:
    """Листать профили по кругу — то, что вешают на хоткей."""
    client = ipc.Client()
    if not client.is_available():
        print("Демон не запущен: листание профилей доступно только через него.", file=sys.stderr)
        return 1
    method = "profile-prev" if getattr(args, "prev", False) else "profile-next"
    try:
        result = client.call(
            method,
            device=_resolve_device_key(getattr(args, "device", None)),
        )
    except ipc.IpcError as exc:
        print(f"Демон отказал: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(result), ensure_ascii=False, indent=2))
    else:
        print(f"Активный профиль: {result.get('active_profile')} — {result.get('result')}")
    return 0


def _resolve_device_key(hint: str | None) -> str:
    """Превратить --device (путь/vid:pid/serial/имя) в ключ демона.

    Ключи вида `serial:…`. Если hint не задан — берём первое устройство.
    """
    client = ipc.Client()
    if not client.is_available():
        return ""
    try:
        devices = list(client.call("status").get("devices", []))
    except (ipc.IpcError, AttributeError):
        return ""
    if not devices:
        return ""
    if not hint:
        return str(devices[0]["key"])
    lowered = hint.lower()
    for item in devices:
        fields = [
            str(item.get("key", "")),
            str(item.get("serial", "")),
            str(item.get("name", "")),
            str(item.get("usb_id", "")),
            str(item.get("path", "")),
        ]
        if any(lowered == field.lower() or lowered in field.lower() for field in fields if field):
            return str(item["key"])
    return str(devices[0]["key"])


def _device_profiles_file(args: argparse.Namespace) -> Path | None:
    """Файл профилей устройства: у работающего демона или из обнаружения."""
    from pathlib import Path

    client = ipc.Client()
    if client.is_available():
        try:
            devices = list(client.call("status").get("devices", []))
        except (ipc.IpcError, AttributeError):
            devices = []
        key = _resolve_device_key(getattr(args, "device", None))
        for item in devices:
            if not key or item["key"] == key:
                if item.get("profiles_file"):
                    return Path(item["profiles_file"])
                break

    # Демона нет: определяем файл по имени устройства.
    nodes = discovery.discover()
    if not nodes:
        print("Устройство не найдено: укажите --profile-file", file=sys.stderr)
        return None
    node = nodes[0]
    return config.profiles_dir() / f"{daemon_module._slug(node.name)}.toml"


def _parse_macro_step(spec: str) -> dict:
    """Разобрать шаг макроса: 'ctrl+c', 'wait:120' или 'tap:enter:50'."""
    text = spec.strip()
    if not text:
        raise ValueError("пустой шаг макроса")
    lowered = text.lower()
    if lowered.startswith("wait:"):
        return {"wait": True, "delay_ms": int(text.split(":", 1)[1])}
    if lowered.startswith("tap:") or lowered.startswith("key:"):
        prefix, _, rest = text.partition(":")
        parts = rest.rsplit(":", 1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            return {prefix.lower(): parts[0], "delay_ms": int(parts[1])}
        return {prefix.lower(): rest}
    return {"tap": text}


def cmd_macro_list(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .input.macros import parse_macros

    path = Path(args.profile_file).expanduser() if args.profile_file else _device_profiles_file(args)
    if path is None:
        return 1
    if not path.exists():
        print(f"Файла профилей нет: {path}", file=sys.stderr)
        return 1
    parsed = profiles.load_profile_file(path)
    macro_dict = parse_macros(parsed.macros_raw)
    if args.json:
        print(json.dumps(
            [{"id": m.id, "name": m.name, "describe": m.describe()} for m in macro_dict.values()],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not macro_dict:
        print(f"В {path} макросов нет.")
        return 0
    print(f"Файл: {path}")
    for macro in macro_dict.values():
        title = f"{macro.id} — {macro.name}" if macro.name else macro.id
        print(f"  {title}: {macro.describe()}")
    return 0


def cmd_macro_add(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .input.macros import parse_macros

    explicit_file = bool(args.profile_file)
    path = Path(args.profile_file).expanduser() if explicit_file else _device_profiles_file(args)
    if path is None:
        return 1
    try:
        steps = [_parse_macro_step(spec) for spec in args.step]
    except (ValueError, IndexError) as exc:
        print(f"Ошибка в шаге: {exc}", file=sys.stderr)
        return 1
    if not steps:
        print("Нужен хотя бы один --step", file=sys.stderr)
        return 1

    raw = {"id": args.id, "name": args.name, "interval_ms": args.interval_ms, "steps": steps}
    try:
        parse_macros([raw])  # проверяем до записи
        profiles.upsert_macro(path, raw, replace=args.replace)
    except (profiles.ProfileError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Макрос {args.id!r} записан в {path}")

    # Перечитываем профили устройства, только если правили именно его файл.
    if not explicit_file:
        client = ipc.Client()
        if client.is_available():
            with contextlib.suppress(ipc.IpcError):
                client.call("reload")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    from pathlib import Path
    profile_set = profiles.load_profile_file(Path(args.path).expanduser())
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        if args.profile:
            profile = next((item for item in profile_set.profiles if item.id == args.profile), None)
            if profile is None:
                print(f"Профиль {args.profile!r} не найден", file=sys.stderr)
                return 1
        else:
            profile = profiles.select(profile_set, serial=opened.node.serial, app=args.app)
            if profile is None:
                print("Ни один профиль не подошёл под устройство/приложение", file=sys.stderr)
                return 1

        result = engine.apply_profile(opened.device, profile)
        if args.json:
            print(json.dumps(ipc.serialize(result), ensure_ascii=False, indent=2))
        else:
            _print_human(result.describe())
            plan = engine.plan_button_actions(profile)
            for name, action in plan.items():
                print(f"  кнопка {name}: {action}")
            print("  все применённые настройки волатильны; во флеш мыши ничего не записано")
    return 0 if result.ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    try:
        status = ipc.Client().call("status")
    except ipc.IpcError as exc:
        print(f"{exc}. Запустить: mousecfg daemon -v", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(status), ensure_ascii=False, indent=2))
        return 0
    print(f"демон: работает, dry-run={status['dry_run']}")
    print(f"источник активного окна: {status['focus_watcher'] or 'нет'}")
    print(f"активное приложение: {status['focused_app'] or '<нет>'}")
    if not status["devices"]:
        print("устройств под управлением нет")
    for device in status["devices"]:
        print(f"  {device['name']} | профиль: {device.get('active_profile') or '-'} | режим: {device.get('mode') or '-'}")
        if device.get("last_error"):
            print(f"    ошибка: {device['last_error']}")
        for name, action in (device.get("buttons_plan") or {}).items():
            print(f"    кнопка {name}: {action}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        devices = ipc.Client().call("list-devices")
    except ipc.IpcError as exc:
        print(f"{exc}. Запустить: mousecfg daemon -v", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(devices), ensure_ascii=False, indent=2))
        return 0
    if not devices:
        print("Демон не видит устройств.")
        return 1
    for device in devices:
        print(f"{device['key']}: {registry_line(device)}")
    return 0


def registry_line(device: dict) -> str:
    bits = [device.get("name") or device.get("usb_id") or "?"]
    if device.get("active_profile"):
        bits.append(f"профиль {device['active_profile']}")
    if device.get("mode"):
        bits.append(f"режим {device['mode']}")
    if device.get("input_path"):
        bits.append(device["input_path"])
    return " | ".join(bits)


def cmd_inputs(args: argparse.Namespace) -> int:
    infos = input_detect.iter_input_devices()
    pointers = [info for info in infos if info.path and info.buttons]
    if args.json:
        print(json.dumps(ipc.serialize([info.__dict__ for info in infos]), ensure_ascii=False, indent=2))
        return 0
    if not infos:
        print(
            "evdev-узлы недоступны: нет прав на /dev/input/event*. "
            "Выполните: sudo install -m 0644 data/70-mousecfg-input.rules /etc/udev/rules.d/ && "
            "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input",
            file=sys.stderr,
        )
        return 1
    print("Мыши (кандидаты для ремапа кнопок):")
    for info in pointers:
        print(f"  {info.describe()}")
    others = [info for info in infos if info not in pointers]
    if others and args.verbose:
        print("Прочие устройства ввода:")
        for info in others:
            print(f"  {info.describe()}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from pathlib import Path

    daemon_options = daemon_module.DaemonConfig(
        dry_run=args.dry_run,
        allow_onboard_switch=args.allow_onboard_switch,
        profiles_dir=Path(args.profiles_dir).expanduser() if args.profiles_dir else config.profiles_dir(),
        watch_apps=not args.no_apps,
        apply_buttons=not args.no_buttons,
    )
    return daemon_module.Daemon(daemon_options).run()


def cmd_run_profile(args: argparse.Namespace) -> int:
    import evdev

    from .input.actions import button_code, parse_action
    from .input.macros import parse_macros
    from .input.remapper import Remapper

    profile_set = profiles.load_profile_file(Path(args.path).expanduser())
    profile = (
        next((item for item in profile_set.profiles if item.id == args.profile), None)
        if args.profile
        else profiles.select(profile_set)
    )
    if profile is None:
        print("Профиль не найден", file=sys.stderr)
        return 1

    if args.input_path:
        info = next((item for item in input_detect.iter_input_devices() if item.path == args.input_path), None)
        if info is None:
            print(f"event-узел {args.input_path} недоступен", file=sys.stderr)
            return 1
    else:
        info = input_detect.require_pointer_device(name_hint=args.device or "")

    device = evdev.InputDevice(info.path)
    bindings = {button_code(name): parse_action(spec) for name, spec in profile.buttons.items()}
    macros = parse_macros(profile_set.macros_raw)
    remapper = Remapper(device, bindings, macros, dry_run=args.dry_run)

    print(f"Перехват кнопок профиля {profile.id} на {info.path}. Ctrl+C — остановить.")
    for name, spec in profile.buttons.items():
        print(f"  {name} -> {parse_action(spec).describe()}")
    if args.dry_run:
        print("  (dry-run: события не отправляются)")

    if args.seconds > 0:
        import threading

        threading.Timer(args.seconds, remapper.stop).start()

    try:
        remapper.run()
    except KeyboardInterrupt:
        remapper.stop()
    print(remapper.stats.summary())
    return 0


def _command_name(args: argparse.Namespace) -> str:
    if args.command == "profile":
        return f"profile-{args.profile_command}"
    if args.command == "macro":
        return f"macro-{args.macro_command}"
    return str(args.command)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    handler = {
        "list": cmd_list,
        "judge": cmd_judge,
        "get": cmd_get,
        "set-dpi": cmd_set_dpi,
        "set-rate": cmd_set_rate,
        "mode": cmd_mode,
        "lighting": cmd_lighting,
        "profiles-check": cmd_profiles_check,
        "apply": cmd_apply,
        "status": cmd_status,
        "devices": cmd_devices,
        "inputs": cmd_inputs,
        "daemon": cmd_daemon,
        "run-profile": cmd_run_profile,
        "profile-add": cmd_profile_add,
        "profile-set": cmd_profile_set,
        "profile-remove": cmd_profile_remove,
        "profile-show": cmd_profile_show,
        "profile-activate": cmd_profile_activate,
        "profile-next": cmd_profile_next,
        "macro-list": cmd_macro_list,
        "macro-add": cmd_macro_add,
    }[_command_name(args)]

    try:
        return handler(args)
    except session.DeviceNotFound as exc:
        print(exc, file=sys.stderr)
        return 1
    except guard.DeniedCommand as exc:
        print(f"Отказано политикой безопасности: {exc}", file=sys.stderr)
        return 3
    except ipc.IpcError as exc:
        print(f"IPC: {exc}", file=sys.stderr)
        return 1
    except LookupError as exc:
        print(f"Не найдено: {exc}", file=sys.stderr)
        return 1
    except (TransportError, ProtocolError, TimeoutError) as exc:
        print(f"Ошибка связи с устройством: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
