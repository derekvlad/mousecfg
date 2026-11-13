"""Командный интерфейс mousecfg."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from pathlib import Path

from . import __version__, config, consent, discovery, engine, ipc, profiles, session
from . import daemon as daemon_module
from .hidpp import guard
from .hidpp.protocol import DeviceError, FeatureUnsupportedError, ProtocolError, TransportError
from .i18n import t
from .input import detect as input_detect
from .vendor.guard import VendorRefused

LOG = logging.getLogger("mousecfg")


def _add_profile_edit_args(parser: argparse.ArgumentParser, *, require_id: bool) -> None:
    """Общие аргументы для `profile add` и `profile set`."""
    parser.add_argument("--profile-file", default=None, help=t("путь к .toml (по умолчанию — файл профилей устройства)", "path to a .toml file (default: the device profiles file)"))
    parser.add_argument("--id", required=require_id, help=t("id профиля", "profile id"))
    parser.add_argument("--name", default="", help=t("человекочитаемое имя", "human-readable name"))
    parser.add_argument("--dpi", type=int, default=None)
    parser.add_argument("--rate", default=None, help=t("'1000Hz', '1ms' или число", "'1000Hz', '1ms' or a number"))
    parser.add_argument(
        "--button",
        action="append",
        default=[],
        metavar="BUTTON=ACTION",
        help=t(
            "назначение кнопки, например side1=browser-back или side2=macro:burst (можно повторять)",
            "button binding, e.g. side1=browser-back or side2=macro:burst (repeatable)",
        ),
    )
    parser.add_argument("--match-app", action="append", default=[], metavar="APP", help=t("привязать профиль к приложению", "bind the profile to an application"))
    parser.add_argument("--match-serial", action="append", default=[], metavar="SERIAL", help=t("привязать профиль к серийнику", "bind the profile to a serial number"))
    parser.add_argument("--lighting-effect", default=None, help=t("эффект подсветки: static, breathe, cycle, wave, disabled", "lighting effect: static, breathe, cycle, wave, disabled"))
    parser.add_argument("--lighting-color", default=None, help=t("цвет подсветки: '#rrggbb' или имя (red, blue...)", "lighting colour: '#rrggbb' or a name (red, blue...)"))
    parser.add_argument("--lighting-brightness", type=int, default=None, help=t("яркость подсветки 0..100", "lighting brightness 0..100"))
    parser.add_argument("--lighting-period", type=int, default=None, help=t("период эффекта, мс", "effect period, ms"))


def _build_parser() -> argparse.ArgumentParser:
    # Общие ключи доступны и после подкоманды: `mousecfg mode host --allow-onboard-switch`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0, help=t("подробный вывод (-vv — отладка)", "verbose output (-vv — debug)"))
    common.add_argument("--device", metavar="HINT", help=t("путь к hidraw, vid:pid, serial или часть имени", "hidraw path, vid:pid, serial or part of the name"))
    common.add_argument("--json", action="store_true", help=t("машинный вывод", "machine-readable output"))
    common.add_argument("--dry-run", action="store_true", help=t("не отправлять кадры, только показать их", "do not send frames, only show them"))
    common.add_argument(
        "--allow-onboard-switch",
        action="store_true",
        help=t(
            "разрешить однократный переход устройства из On-Board в Host mode",
            "allow a one-time switch of the device from On-Board to Host mode",
        ),
    )

    parser = argparse.ArgumentParser(
        prog="mousecfg",
        parents=[common],
        description=t(
            "Профили игровых мышей на стороне хоста. Во встроенную память мыши ничего не пишется.",
            "Host-side profiles for gaming mice. Nothing is written to onboard memory.",
        ),
    )
    parser.add_argument("--version", action="version", version=f"mousecfg {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", parents=[common], help=t("показать мыши, их протокол и доступные настройки", "show mice, their protocol and available settings"))
    sub.add_parser("judge", parents=[common], help=t("показать политику: что разрешено, что запрещено", "show the policy: what is allowed and what is denied"))
    sub.add_parser("get", parents=[common], help=t("прочитать имя, прошивку, DPI, report rate, режим", "read name, firmware, DPI, report rate and mode"))

    vendor = sub.add_parser(
        "vendor",
        parents=[common],
        help=t(
            "прочитать вендорский канал мыши (только чтение, без записи в память)",
            "read the mouse vendor channel (read-only, nothing is written to memory)",
        ),
    )
    vendor.add_argument(
        "--probe",
        action="store_true",
        help=t("снять сырые дампы читающих команд", "dump the raw replies of the read commands"),
    )

    dpi = sub.add_parser("set-dpi", parents=[common], help=t("установить DPI (волатильно)", "set DPI (volatile)"))
    dpi.add_argument("dpi", type=int)
    dpi.add_argument("--sensor", type=int, default=0)

    rate = sub.add_parser("set-rate", parents=[common], help=t("установить частоту опроса (волатильно)", "set the report rate (volatile)"))
    rate.add_argument("rate", help=t("'1000Hz', '1ms' или число", "'1000Hz', '1ms' or a number"))

    mode = sub.add_parser("mode", parents=[common], help=t("режим устройства: показать или переключить", "device mode: show or switch"))
    mode.add_argument("value", nargs="?", choices=["host", "onboard"], help=t("без значения — только показать", "without a value, only show it"))

    light = sub.add_parser("lighting", parents=[common], help=t("подсветка мыши: показать зоны и эффекты", "mouse lighting: show zones and effects"))
    light.add_argument("--effect", help=t("включить эффект волатильно (static, breathe, cycle, wave, disabled...)", "enable an effect volatilely (static, breathe, cycle, wave, disabled...)"))
    light.add_argument("--color", help=t("цвет: '#rrggbb', 'red' или 0xRRGGBB", "colour: '#rrggbb', 'red' or 0xRRGGBB"))
    light.add_argument("--brightness", type=int, help=t("яркость 0..100", "brightness 0..100"))
    light.add_argument("--period", type=int, help=t("период эффекта, мс", "effect period, ms"))
    light.add_argument("--intensity", type=int, help=t("интенсивность 0..255", "intensity 0..255"))
    light.add_argument("--form", type=int, help=t("форма волны (для breathe)", "waveform (for breathe)"))
    light.add_argument("--zone", type=int, default=0, help=t("номер зоны подсветки", "lighting zone index"))

    validate = sub.add_parser("profiles-check", parents=[common], help=t("проверить TOML-профили без устройства", "validate TOML profiles without a device"))
    validate.add_argument("path", nargs="?", default=None, help=t("файл или каталог (по умолчанию ~/.config/mousecfg/profiles)", "file or directory (default ~/.config/mousecfg/profiles)"))

    profile = sub.add_parser("profile", parents=[common], help=t("создать, изменить или удалить профиль (файл на хосте)", "create, change or delete a profile (a file on the host)"))
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    profile_add = profile_sub.add_parser("add", parents=[common], help=t("добавить профиль", "add a profile"))
    _add_profile_edit_args(profile_add, require_id=True)

    profile_set = profile_sub.add_parser("set", parents=[common], help=t("изменить существующий профиль", "change an existing profile"))
    _add_profile_edit_args(profile_set, require_id=True)

    profile_rm = profile_sub.add_parser("remove", parents=[common], help=t("удалить профиль", "delete a profile"))
    profile_rm.add_argument("id", help=t("id профиля", "profile id"))
    profile_rm.add_argument("--profile-file", default=None, help=t("путь к .toml (по умолчанию — файл профилей устройства)", "path to a .toml file (default: the device profiles file)"))

    profile_show = profile_sub.add_parser("show", parents=[common], help=t("показать профили файла", "show the profiles in the file"))
    profile_show.add_argument("id", nargs="?", help=t("id профиля (без него — все)", "profile id (without it, all profiles)"))
    profile_show.add_argument("--profile-file", default=None, help=t("путь к .toml (по умолчанию — файл профилей устройства)", "path to a .toml file (default: the device profiles file)"))

    profile_activate = profile_sub.add_parser(
        "activate", parents=[common], help=t("переключить устройство на профиль (через демон; для хоткеев)", "switch the device to a profile (via the daemon; used by shortcuts)")
    )
    profile_activate.add_argument("id", help=t("id профиля", "profile id"))

    profile_next = profile_sub.add_parser("next", parents=[common], help=t("следующий профиль по кругу", "next profile in a circle"))
    profile_next.add_argument("--prev", action="store_true", help=t("предыдущий вместо следующего", "the previous one instead of the next"))

    macro = sub.add_parser("macro", parents=[common], help=t("список и добавление макросов (исполняются на хосте)", "list and add macros (executed on the host)"))
    macro_sub = macro.add_subparsers(dest="macro_command", required=True)

    macro_list = macro_sub.add_parser("list", parents=[common], help=t("показать макросы файла", "show the macros in the file"))
    macro_list.add_argument("--profile-file", default=None)

    macro_add = macro_sub.add_parser("add", parents=[common], help=t("добавить макрос", "add a macro"))
    macro_add.add_argument("--profile-file", default=None, help=t("путь к .toml (по умолчанию — файл профилей устройства)", "path to a .toml file (default: the device profiles file)"))
    macro_add.add_argument("--id", required=True, help=t("id макроса", "macro id"))
    macro_add.add_argument("--name", default="")
    macro_add.add_argument("--interval-ms", type=int, default=40, help=t("пауза между шагами, мс", "delay between steps, ms"))
    macro_add.add_argument(
        "--step",
        action="append",
        default=[],
        metavar="SPEC",
        help=t(
            "шаг: 'ctrl+c', 'wait:120' или 'tap:enter:50' (можно повторять)",
            "step: 'ctrl+c', 'wait:120' or 'tap:enter:50' (repeatable)",
        ),
    )
    macro_add.add_argument("--replace", action="store_true", help=t("заменить макрос с тем же id", "replace the macro with the same id"))

    apply_parser = sub.add_parser("apply", parents=[common], help=t("применить профиль из файла (волатильно)", "apply a profile from a file (volatile)"))
    apply_parser.add_argument("path", help=t("файл .toml с профилями", "a .toml file with profiles"))
    apply_parser.add_argument("--profile", help=t("id профиля (по умолчанию первый подходящий)", "profile id (default: the first matching one)"))
    apply_parser.add_argument("--app", default="", help=t("имя активного приложения для match_apps", "focused application name for match_apps"))

    sub.add_parser("status", parents=[common], help=t("состояние демона: устройства, активное приложение", "daemon state: devices, focused application"))
    sub.add_parser("devices", parents=[common], help=t("устройства, которые видит демон", "devices the daemon sees"))
    sub.add_parser("inputs", parents=[common], help=t("evdev-узлы мышей: где есть кнопки для ремапа", "mouse evdev nodes: which have buttons to remap"))

    keybinds = sub.add_parser(
        "keybinds",
        parents=[common],
        help=t(
            "показать хоткеи смены профиля для конфига композитора (niri и др.)",
            "show profile-switching shortcuts for the compositor config (niri and others)",
        ),
    )
    keybinds.add_argument(
        "--install",
        action="store_true",
        help=t("добавить блок в конфиг niri (с резервной копией)", "add the block to the niri config (with a backup)"),
    )

    daemon = sub.add_parser("daemon", parents=[common], help=t("запустить демон на переднем плане", "run the daemon in the foreground"))
    daemon.add_argument("--profiles-dir", default=None)
    daemon.add_argument("--no-buttons", action="store_true")
    daemon.add_argument("--no-apps", action="store_true")
    daemon.add_argument(
        "--no-auto-host",
        action="store_true",
        help=t(
            "не повторять переход On-Board → Host автоматически (даже если согласие сохранено)",
            "do not repeat the On-Board → Host switch automatically (even if consent was recorded)",
        ),
    )

    run_profile = sub.add_parser("run-profile", parents=[common], help=t("включить ремап кнопок профиля (без демона)", "enable button remapping for the profile (without the daemon)"))
    run_profile.add_argument("path", help=t("файл .toml с профилями", "a .toml file with profiles"))
    run_profile.add_argument("--profile", help=t("id профиля", "profile id"))
    run_profile.add_argument("--input", dest="input_path", help=t("event-узел мыши (по умолчанию — поиск автоматически)", "mouse event device (by default it is autodetected)"))
    run_profile.add_argument("--seconds", type=float, default=0.0, help=t("остановиться через N секунд (0 — до Ctrl+C)", "stop after N seconds (0 — until Ctrl+C)"))

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
    mice = discovery.find_mice()
    if args.json:
        from dataclasses import asdict

        payload = []
        for mouse in mice:
            item = asdict(mouse)
            item["capabilities"] = sorted(mouse.capabilities)
            payload.append(item)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not mice:
        print(t("Мыши не найдены.", "No mice found."))
        return 1

    names = {
        "dpi": "DPI",
        "report_rate": t("частота опроса", "report rate"),
        "lighting": t("подсветка", "lighting"),
        "mode": "Host/On-Board",
        "buttons": t("кнопки и макросы", "buttons and macros"),
        discovery.VENDOR_READ_CAPABILITY: t("чтение вендорского канала", "vendor channel read"),
    }
    print(t("Мыши:", "Mice:"))
    for mouse in mice:
        print(f"  {mouse.describe()}")
        capabilities = ", ".join(names.get(item, item) for item in sorted(mouse.capabilities))
        print(t(f"    доступно: {capabilities}", f"    available: {capabilities}"))
        if not mouse.supports_hidpp:
            print(
                t(
                    "    без HID++: профили, кнопки и макросы — через evdev; DPI/частота/подсветка недоступны",
                    "    no HID++: profiles, buttons and macros go through evdev; DPI/report rate/lighting are unavailable",
                )
            )
        if mouse.vendor_path:
            print(
                t(
                    f"    вендорский канал: {mouse.vendor_path} — только чтение, см. mousecfg vendor",
                    f"    vendor channel: {mouse.vendor_path} — read-only, see mousecfg vendor",
                )
            )

    if args.verbose:
        print(t("\nВсе hidraw-узлы:", "\nAll hidraw nodes:"))
        for node in discovery.discover(include_all=True):
            marks = ["HID++ if01"] if node.is_hidpp_candidate else [t("не HID++ кандидат", "not a HID++ candidate")]
            if node.vendor_channel:
                marks.append(t("вендорский канал", "vendor channel"))
            print(f"  {node.describe()}  ({', '.join(marks)})")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    audit = guard.audit()
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0
    titles = {
        "volatile": t(
            "ВОЛАТИЛЬНО — можно применять (живёт в RAM до отключения мыши)",
            "VOLATILE — safe to apply (lives in RAM until the mouse is disconnected)",
        ),
        "persistent-consented": t(
            "ПЕРСИСТЕНТНО, С СОГЛАСИЯ — один раз, по явному разрешению",
            "PERSISTENT, WITH CONSENT — once, on explicit permission",
        ),
        "persistent": t(
            "ЗАПРЕЩЕНО — пишет в энергонезависимую память устройства",
            "DENIED — writes to the non-volatile device memory",
        ),
    }
    for key in ("volatile", "persistent-consented", "persistent"):
        print(f"\n{titles[key]}")
        for line in audit.get(key, []):
            print(f"  {line}")
    print(t("\nВсё остальное считается запрещённым: неизвестная команда не отправится никогда.",
            "\nEverything else is considered denied: an unknown command is never sent."))
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
            data["dpi"] = t(f"недоступно: {exc}", f"unavailable: {exc}")

        try:
            data["report_rate_ms"] = device.get_rate_ms()
            data["report_rate_supported_ms"] = device.supported_rates_ms()
        except (FeatureUnsupportedError, DeviceError) as exc:
            data["report_rate_ms"] = t(f"недоступно: {exc}", f"unavailable: {exc}")

        if device.has_feature(0x8100):
            data["mode"] = device.get_mode()
        else:
            data["mode"] = t("фича onboard отсутствует", "the onboard feature is missing")

        data["features"] = sorted(f"{feature:#06x}" for feature in device.discover_features())
        _dump(data, args.json)
    return 0


def cmd_vendor(args: argparse.Namespace) -> int:
    """Прочитать вендорский канал: идентификация и read-only дампы."""
    from .vendor.device import VendorDevice, VendorError

    mouse = discovery.mouse_for_hint(args.device) if args.device else None
    if mouse is None and args.device is None:
        mouse = next((item for item in discovery.find_mice() if item.vendor_path), None)
    if mouse is None:
        hint = t(f" по подсказке {args.device!r}", f" matching {args.device!r}") if args.device else ""
        print(t(f"Мышь{hint} не найдена.", f"No mouse{hint} found."), file=sys.stderr)
        return 1
    if not mouse.vendor_path:
        print(
            t(
                f"{mouse.name}: вендорского канала нет — читать нечего",
                f"{mouse.name}: there is no vendor channel — nothing to read",
            ),
            file=sys.stderr,
        )
        return 1

    try:
        with VendorDevice(mouse.vendor_path) as device:
            probe = device.probe(deep=bool(args.probe))
    except (VendorError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.json:
        from dataclasses import asdict

        print(json.dumps(asdict(probe), ensure_ascii=False, indent=2))
        return 0

    print(t(f"Мышь: {mouse.name}  [{mouse.usb_id}]", f"Mouse: {mouse.name}  [{mouse.usb_id}]"))
    print(t(f"Вендорский канал: {probe.path} (только чтение)", f"Vendor channel: {probe.path} (read-only)"))
    print(t(f"Диалект: {probe.dialect}", f"Dialect: {probe.dialect}"))
    if probe.identification:
        print(t(f"Идентификация: {probe.identification}", f"Identification: {probe.identification}"))
    if probe.version:
        print(t(f"Версия прошивки: {probe.version}", f"Firmware version: {probe.version}"))
    if probe.responses:
        print(t("Ответы на читающие команды:", "Replies to the read commands:"))
        for response in probe.responses:
            print(f"  {response.label}:")
            print(f"    request: {response.request}")
            print(f"    reply:   {response.reply}")
            if response.text:
                print(t(f"    текст:   {response.text}", f"    text:    {response.text}"))
    for note in probe.notes:
        print(f"  ! {note}")
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
                    "note": t(
                        "[dry-run] кадр не отправлен: команда меняет состояние устройства",
                        "[dry-run] frame not sent: the command changes the device state",
                    ),
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
                "note": t(
                    "значение волатильно: пропадёт при отключении мыши, демон применит его заново",
                    "the value is volatile: it is lost when the mouse is disconnected, and the daemon applies it again",
                ),
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
                    "note": t("[dry-run] кадр не отправлен", "[dry-run] frame not sent"),
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
            if not args.allow_onboard_switch:
                print(
                    t(
                        "Переход On-Board -> Host меняет конфигурацию устройства.\n"
                        "Это единственная потенциально персистентная операция в проекте.\n"
                        "Повторите с --allow-onboard-switch, если согласны.",
                        "Switching On-Board -> Host changes the device configuration.\n"
                        "This is the only potentially persistent operation in the project.\n"
                        "Repeat with --allow-onboard-switch if you agree.",
                    ),
                    file=sys.stderr,
                )
                return 2

            # Согласие запоминаем, как только оно выражено, — даже если мышь
            # уже в Host: иначе при следующей перезагрузке (когда прошивка
            # вернёт On-Board) демону нечего будет повторить. Согласие дано
            # явно и на конкретное устройство.
            if not args.dry_run:
                consent.record(consent.key_for_node(opened.node))

            if current == "host":
                _dump(
                    {
                        "mode": current,
                        "changed": False,
                        "consent": t("запомнено", "recorded")
                        if not args.dry_run
                        else t("[dry-run] не записано", "[dry-run] not recorded"),
                    },
                    args.json,
                )
                return 0

            device.set_host_mode()
            if args.dry_run:
                _dump(
                    {
                        "mode_before": current,
                        "sent": False,
                        "note": t("[dry-run] переход не отправлен", "[dry-run] switch not sent"),
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
                    "note": t(
                        "переход выполнен один раз; дальше все настройки только в RAM",
                        "the switch was performed once; from now on all settings live only in RAM",
                    ),
                },
                args.json,
            )
            return 0

        print(
            t(
                "Возврат в On-Board mode проектом не поддерживается: он подразумевает запись профиля "
                "во встроенную память мыши.",
                "Switching back to On-Board mode is not supported by the project: it would mean writing a profile "
                "to the mouse onboard memory.",
            ),
            file=sys.stderr,
        )
        return 2


def cmd_lighting(args: argparse.Namespace) -> int:
    with session.open_device(args.device, allow_onboard_switch=args.allow_onboard_switch, dry_run=args.dry_run) as opened:
        device = opened.device
        info = device.lighting_info()
        if not info.supported:
            print(t("Устройство не поддерживает подсветку RGB_EFFECTS (0x8071).", "The device does not support RGB_EFFECTS lighting (0x8071)."), file=sys.stderr)
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
                "note": t(
                    "запись волатильна: подсветка не сохраняется в память мыши",
                    "the write is volatile: lighting is not saved to mouse memory",
                ),
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
                t(
                    f"Эффект {effect_name!r} неизвестен. Доступные: {', '.join(sorted(known_effects)) or 'нет'}",
                    f"Unknown effect {effect_name!r}. Available: {', '.join(sorted(known_effects)) or 'none'}",
                ),
                file=sys.stderr,
            )
            return 1

        from .hidpp.device import LIGHTING_EFFECTS

        schema = LIGHTING_EFFECTS.get(effect_id, (None, {}))[1]
        params: dict[str, int] = {}
        if args.color is not None and "color" in schema:
            params["color"] = profiles.parse_color(args.color) or 0
        if args.period is not None and "period" in schema:
            params["period"] = args.period
        if args.intensity is not None and "intensity" in schema:
            params["intensity"] = args.intensity
        if args.form is not None and "form" in schema:
            params["form"] = args.form
        if args.brightness is not None:
            if not 0 <= args.brightness <= 100:
                print(t("Яркость должна быть 0..100", "Brightness must be 0..100"), file=sys.stderr)
                return 1
            intensity = max(0, min(255, round(args.brightness * 255 / 100)))
            if "intensity" in schema:
                params["intensity"] = intensity
            elif effect_name == "static" and "color" in schema:
                # У static яркости как байта нет: притемняем сам цвет.
                from .engine import _scale_color

                params["color"] = _scale_color(params.get("color", 0xFFFFFF), args.brightness)
            else:
                print(
                    t(
                        f"Эффект {effect_name!r} не поддерживает параметр яркости",
                        f"Effect {effect_name!r} does not support a brightness parameter",
                    ),
                    file=sys.stderr,
                )
                return 1

        try:
            slot = device.set_lighting(effect_id, params, zone=args.zone)
        except (ProtocolError, ValueError) as exc:
            print(t(f"Не удалось включить подсветку: {exc}", f"Failed to enable lighting: {exc}"), file=sys.stderr)
            return 1

        if args.dry_run:
            _dump(
                {
                    "effect": effect_name,
                    "slot": slot,
                    "zone": args.zone,
                    "sent": False,
                    "note": t("[dry-run] кадр не отправлен", "[dry-run] frame not sent"),
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
                "note": t(
                    "волатильно: пропадёт при отключении мыши, демон применит заново",
                    "volatile: it is lost when the mouse is disconnected, and the daemon applies it again",
                ),
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
        print(t(f"Путь не найден: {path}", f"Path not found: {path}"), file=sys.stderr)
        return 1

    if not sets:
        print(t(f"В {path} нет файлов профилей (*.toml).", f"No profile files (*.toml) in {path}."))
        return 1

    for profile_set in sets:
        ids = ", ".join(profile.id for profile in profile_set.profiles)
        print(f"{profile_set.device or t('<без устройства>', '<no device>')}: {ids}")
    print(t("Схема корректна: профили живут на хосте, storage=onboard отсутствует.", "The schema is valid: profiles live on the host and storage=onboard is absent."))
    return 0


def _parse_button_spec(spec: str) -> tuple[str, object]:
    """Разобрать 'side1=browser-back' или 'side2=macro:burst'."""
    if "=" not in spec:
        raise ValueError(t(f"назначение кнопки должно быть BUTTON=ACTION, получено {spec!r}", f"a button binding must be BUTTON=ACTION, got {spec!r}"))
    name, _, action = spec.partition("=")
    name = name.strip()
    action = action.strip()
    if not name or not action:
        raise ValueError(t(f"пустое имя или действие в {spec!r}", f"empty name or action in {spec!r}"))
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
        print(t(f"Ошибка в профиле: {exc}", f"Profile error: {exc}"), file=sys.stderr)
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
            print(t(f"Демон отказал: {exc}", f"The daemon refused: {exc}"), file=sys.stderr)
            _macro_hint(exc, buttons)
            return 1
        verb = t("изменён", "updated") if result.get("replaced") else t("добавлен", "added")
        print(t(f"Профиль {profile.id!r} {verb} в {result.get('path')}", f"Profile {profile.id!r} {verb} in {result.get('path')}"))
        return 0

    # Демон не запущен (или задан явный файл) — правим файл напрямую.
    try:
        if replace:
            replaced = profiles.upsert_profile(path, profile)
            verb = t("изменён", "updated") if replaced else t("добавлен", "added")
        else:
            profiles.add_profile(path, profile)
            verb = t("добавлен", "added")
    except profiles.ProfileError as exc:
        print(t(f"Ошибка: {exc}", f"Error: {exc}"), file=sys.stderr)
        _macro_hint(exc, buttons)
        return 1
    print(t(f"Профиль {profile.id!r} {verb} в {path}", f"Profile {profile.id!r} {verb} in {path}"))
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
            t(
                "Подсказка: макросы описываются в том же файле блоком [[macro]] с steps. "
                f"Сейчас профиль ссылается на: {', '.join(referenced)}.",
                "Hint: macros are defined in the same file as a [[macro]] block with steps. "
                f"The profile currently references: {', '.join(referenced)}.",
            ),
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
            print(t(f"Демон отказал: {exc}", f"The daemon refused: {exc}"), file=sys.stderr)
            return 1
        print(t(f"Профиль {args.id!r} удалён из {result.get('path')}", f"Profile {args.id!r} deleted from {result.get('path')}"))
        return 0

    try:
        profiles.remove_profile(path, args.id)
    except profiles.ProfileError as exc:
        print(t(f"Ошибка: {exc}", f"Error: {exc}"), file=sys.stderr)
        return 1
    print(t(f"Профиль {args.id!r} удалён из {path}", f"Profile {args.id!r} deleted from {path}"))
    return 0


def cmd_profile_show(args: argparse.Namespace) -> int:
    from pathlib import Path

    path = Path(args.profile_file).expanduser() if args.profile_file else _device_profiles_file(args)
    if path is None:
        return 1
    if not path.exists():
        print(t(f"Файла профилей нет: {path}", f"There is no profiles file: {path}"), file=sys.stderr)
        return 1

    try:
        profile_set = profiles.load_profile_file(path)
    except profiles.ProfileError as exc:
        print(t(f"Ошибка разбора: {exc}", f"Parse error: {exc}"), file=sys.stderr)
        return 1

    selected = profile_set.profiles
    if args.id:
        selected = [item for item in selected if item.id == args.id]
        if not selected:
            print(t(f"Профиль {args.id!r} не найден", f"Profile {args.id!r} not found"), file=sys.stderr)
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

    print(t(f"Файл: {path}", f"File: {path}"))
    for item in selected:
        print(f"\n[{item.id}] {item.name or ''}".rstrip())
        if item.dpi:
            print(f"  dpi: {item.dpi}")
        if item.report_rate_ms:
            print(t(f"  report_rate: {item.report_rate_ms} мс", f"  report_rate: {item.report_rate_ms} ms"))
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
            print(t(f"  подсветка: {', '.join(bits)}", f"  lighting: {', '.join(bits)}"))
        for name, action in item.buttons.items():
            print(t(f"  кнопка {name}: {action}", f"  button {name}: {action}"))
    return 0


def cmd_profile_activate(args: argparse.Namespace) -> int:
    """Переключить профиль через демон (используется хоткеем и треем)."""
    client = ipc.Client()
    if not client.is_available():
        print(t("Демон не запущен: активация профиля доступна только через него.", "The daemon is not running: profile activation is only available through it."), file=sys.stderr)
        return 1
    try:
        result = client.call(
            "profile-activate",
            device=_resolve_device_key(getattr(args, "device", None)),
            profile=str(args.id),
        )
    except ipc.IpcError as exc:
        print(t(f"Демон отказал: {exc}", f"The daemon refused: {exc}"), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(result), ensure_ascii=False, indent=2))
    else:
        print(t(f"Активный профиль: {result.get('active_profile')} — {result.get('result')}", f"Active profile: {result.get('active_profile')} — {result.get('result')}"))
    return 0


def cmd_profile_next(args: argparse.Namespace) -> int:
    """Листать профили по кругу — то, что вешают на хоткей."""
    client = ipc.Client()
    if not client.is_available():
        print(t("Демон не запущен: листание профилей доступно только через него.", "The daemon is not running: profile cycling is only available through it."), file=sys.stderr)
        return 1
    method = "profile-prev" if getattr(args, "prev", False) else "profile-next"
    try:
        result = client.call(
            method,
            device=_resolve_device_key(getattr(args, "device", None)),
        )
    except ipc.IpcError as exc:
        print(t(f"Демон отказал: {exc}", f"The daemon refused: {exc}"), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(result), ensure_ascii=False, indent=2))
    else:
        print(t(f"Активный профиль: {result.get('active_profile')} — {result.get('result')}", f"Active profile: {result.get('active_profile')} — {result.get('result')}"))
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
    mice = discovery.find_mice()
    if not mice:
        print(t("Устройство не найдено: укажите --profile-file", "Device not found: specify --profile-file"), file=sys.stderr)
        return None
    node = mice[0]
    return config.profiles_dir() / f"{daemon_module._slug(node.name)}.toml"


def _parse_macro_step(spec: str) -> dict:
    """Разобрать шаг макроса: 'ctrl+c', 'wait:120' или 'tap:enter:50'."""
    text = spec.strip()
    if not text:
        raise ValueError(t("пустой шаг макроса", "empty macro step"))
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
        print(t(f"Файла профилей нет: {path}", f"There is no profiles file: {path}"), file=sys.stderr)
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
        print(t(f"В {path} макросов нет.", f"No macros in {path}."))
        return 0
    print(t(f"Файл: {path}", f"File: {path}"))
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
        print(t(f"Ошибка в шаге: {exc}", f"Step error: {exc}"), file=sys.stderr)
        return 1
    if not steps:
        print(t("Нужен хотя бы один --step", "At least one --step is required"), file=sys.stderr)
        return 1

    raw = {"id": args.id, "name": args.name, "interval_ms": args.interval_ms, "steps": steps}
    try:
        parse_macros([raw])  # проверяем до записи
        profiles.upsert_macro(path, raw, replace=args.replace)
    except (profiles.ProfileError, ValueError) as exc:
        print(t(f"Ошибка: {exc}", f"Error: {exc}"), file=sys.stderr)
        return 1
    print(t(f"Макрос {args.id!r} записан в {path}", f"Macro {args.id!r} written to {path}"))

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
                print(t(f"Профиль {args.profile!r} не найден", f"Profile {args.profile!r} not found"), file=sys.stderr)
                return 1
        else:
            profile = profiles.select(profile_set, serial=opened.node.serial, app=args.app)
            if profile is None:
                print(t("Ни один профиль не подошёл под устройство/приложение", "No profile matched the device/application"), file=sys.stderr)
                return 1

        result = engine.apply_profile(opened.device, profile)
        if args.json:
            print(json.dumps(ipc.serialize(result), ensure_ascii=False, indent=2))
        else:
            _print_human(result.describe())
            plan = engine.plan_button_actions(profile)
            for name, action in plan.items():
                print(t(f"  кнопка {name}: {action}", f"  button {name}: {action}"))
            print(t("  все применённые настройки волатильны; во флеш мыши ничего не записано", "  all applied settings are volatile; nothing was written to mouse flash"))
    return 0 if result.ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    try:
        status = ipc.Client().call("status")
    except ipc.IpcError as exc:
        print(t(f"{exc}. Запустить: mousecfg daemon -v", f"{exc}. Start it with: mousecfg daemon -v"), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(status), ensure_ascii=False, indent=2))
        return 0
    print(t(f"демон: работает, dry-run={status['dry_run']}", f"daemon: running, dry-run={status['dry_run']}"))
    print(
        t(
            f"источник активного окна: {status['focus_watcher'] or t('нет', 'none')}",
            f"focused window source: {status['focus_watcher'] or t('нет', 'none')}",
        )
    )
    print(
        t(
            f"активное приложение: {status['focused_app'] or t('<нет>', '<none>')}",
            f"focused application: {status['focused_app'] or t('<нет>', '<none>')}",
        )
    )
    if not status["devices"]:
        print(t("устройств под управлением нет", "no managed devices"))
    for device in status["devices"]:
        print(t(
            f"  {device['name']} | профиль: {device.get('active_profile') or '-'} | режим: {device.get('mode') or '-'}",
            f"  {device['name']} | profile: {device.get('active_profile') or '-'} | mode: {device.get('mode') or '-'}",
        ))
        if device.get("protocol") == "evdev":
            print(t(
                "    управление: evdev (без HID++; DPI, частота и подсветка недоступны)",
                "    managed via: evdev (no HID++; DPI, report rate and lighting are unavailable)",
            ))
        if "vendor-read" in (device.get("capabilities") or []):
            print(t(
                "    вендорский канал: доступен только для чтения, см. mousecfg vendor",
                "    vendor channel: read-only, see mousecfg vendor",
            ))
        if device.get("last_error"):
            print(t(f"    ошибка: {device['last_error']}", f"    error: {device['last_error']}"))
        for name, action in (device.get("buttons_plan") or {}).items():
            print(t(f"    кнопка {name}: {action}", f"    button {name}: {action}"))
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        devices = ipc.Client().call("list-devices")
    except ipc.IpcError as exc:
        print(t(f"{exc}. Запустить: mousecfg daemon -v", f"{exc}. Start it with: mousecfg daemon -v"), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(ipc.serialize(devices), ensure_ascii=False, indent=2))
        return 0
    if not devices:
        print(t("Демон не видит устройств.", "The daemon sees no devices."))
        return 1
    for device in devices:
        print(f"{device['key']}: {registry_line(device)}")
    return 0


def registry_line(device: dict) -> str:
    bits = [device.get("name") or device.get("usb_id") or "?"]
    if device.get("protocol") == "evdev":
        bits.append("evdev")
    if device.get("active_profile"):
        bits.append(t(f"профиль {device['active_profile']}", f"profile {device['active_profile']}"))
    if device.get("mode"):
        bits.append(t(f"режим {device['mode']}", f"mode {device['mode']}"))
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
            t(
                "evdev-узлы недоступны: нет прав на /dev/input/event*. "
                "Выполните: sudo install -m 0644 data/70-mousecfg-input.rules /etc/udev/rules.d/ && "
                "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input",
                "evdev devices are unavailable: no permissions for /dev/input/event*. "
                "Run: sudo install -m 0644 data/70-mousecfg-input.rules /etc/udev/rules.d/ && "
                "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input",
            ),
            file=sys.stderr,
        )
        return 1
    print(t("Мыши (кандидаты для ремапа кнопок):", "Mice (candidates for button remapping):"))
    for info in pointers:
        print(f"  {info.describe()}")
    others = [info for info in infos if info not in pointers]
    if others and args.verbose:
        print(t("Прочие устройства ввода:", "Other input devices:"))
        for info in others:
            print(f"  {info.describe()}")
    return 0


def cmd_keybinds(args: argparse.Namespace) -> int:
    """Показать (и при желании поставить) хоткеи смены профиля.

    Портал GlobalShortcuts работает не везде; на niri хоткей вешает сам
    композитор. Команда печатает готовый блок binds и умеет дописать его в
    конфиг niri, сохранив резервную копию.
    """
    from . import hotkeys

    compositor = hotkeys.current_compositor() or t("неизвестен", "unknown")
    block = hotkeys.niri_keybinds()
    if args.json:
        print(
            json.dumps(
                {
                    "compositor": compositor,
                    "portal_supported": hotkeys.portal_supported(),
                    "config": str(hotkeys.niri_config_path()),
                    "block": block,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if hotkeys.portal_supported():
        print(t(f"Композитор {compositor}: портал GlobalShortcuts доступен, трей поднимет хоткеи сам.", f"Compositor {compositor}: the GlobalShortcuts portal is available, the tray will set up the shortcuts itself."))
        return 0

    path = hotkeys.niri_config_path()
    print(t(f"Композитор {compositor}: портал GlobalShortcuts недоступен, хоткей вешает сам {compositor}.", f"Compositor {compositor}: the GlobalShortcuts portal is unavailable, so {compositor} binds the shortcut itself."))
    print(t(f"Вставьте это внутрь блока binds {{ ... }} в {path}:\n", f"Insert this inside the binds {{ ... }} block in {path}:\n"))
    print(block)

    if not args.install:
        print(t("\nПроверить и применить: niri msg action load-config-file", "\nCheck and apply: niri msg action load-config-file"))
        return 0

    if not path.exists():
        print(t(f"\nФайл {path} не найден: хоткеи не установлены.", f"\nFile {path} not found: shortcuts were not installed."), file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")
    if hotkeys.niri_keybinds_present(text):
        print(t("\nХоткеи mousecfg уже есть в конфиге — ничего не меняю.", "\nmousecfg shortcuts are already present in the config — nothing to change."))
        return 0
    backup = path.with_suffix(path.suffix + ".bak.mousecfg")
    backup.write_text(text, encoding="utf-8")
    # Дописываем блок в конец файла: niri принимает несколько блоков binds
    # в одном файле и объединяет их.
    suffix = "\n" if text.endswith("\n") else "\n\n"
    path.write_text(text + suffix + "binds {\n" + block + "\n}\n", encoding="utf-8")
    print(t(f"\nДобавлено в {path} (резервная копия: {backup}).", f"\nAdded to {path} (backup: {backup})."))
    print(t("Применить: niri msg action load-config-file", "Apply: niri msg action load-config-file"))
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from pathlib import Path

    daemon_options = daemon_module.DaemonConfig(
        dry_run=args.dry_run,
        allow_onboard_switch=args.allow_onboard_switch,
        profiles_dir=Path(args.profiles_dir).expanduser() if args.profiles_dir else config.profiles_dir(),
        watch_apps=not args.no_apps,
        apply_buttons=not args.no_buttons,
        auto_host=not args.no_auto_host,
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
        print(t("Профиль не найден", "Profile not found"), file=sys.stderr)
        return 1

    if args.input_path:
        info = next((item for item in input_detect.iter_input_devices() if item.path == args.input_path), None)
        if info is None:
            print(t(f"event-узел {args.input_path} недоступен", f"event device {args.input_path} is unavailable"), file=sys.stderr)
            return 1
    else:
        info = input_detect.require_pointer_device(name_hint=args.device or "")

    device = evdev.InputDevice(info.path)
    bindings = {button_code(name): parse_action(spec) for name, spec in profile.buttons.items()}
    macros = parse_macros(profile_set.macros_raw)
    remapper = Remapper(device, bindings, macros, dry_run=args.dry_run)

    print(t(f"Перехват кнопок профиля {profile.id} на {info.path}. Ctrl+C — остановить.", f"Intercepting the buttons of profile {profile.id} on {info.path}. Ctrl+C to stop."))
    for name, spec in profile.buttons.items():
        print(f"  {name} -> {parse_action(spec).describe()}")
    if args.dry_run:
        print(t("  (dry-run: события не отправляются)", "  (dry-run: events are not sent)"))

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
        "vendor": cmd_vendor,
        "set-dpi": cmd_set_dpi,
        "set-rate": cmd_set_rate,
        "mode": cmd_mode,
        "lighting": cmd_lighting,
        "profiles-check": cmd_profiles_check,
        "apply": cmd_apply,
        "status": cmd_status,
        "devices": cmd_devices,
        "inputs": cmd_inputs,
        "keybinds": cmd_keybinds,
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
    except session.UnsupportedProtocol as exc:
        print(exc, file=sys.stderr)
        return 1
    except session.DeviceNotFound as exc:
        print(exc, file=sys.stderr)
        return 1
    except (guard.DeniedCommand, VendorRefused) as exc:
        print(t(f"Отказано политикой безопасности: {exc}", f"Denied by the safety policy: {exc}"), file=sys.stderr)
        return 3
    except ipc.IpcError as exc:
        print(f"IPC: {exc}", file=sys.stderr)
        return 1
    except LookupError as exc:
        print(t(f"Не найдено: {exc}", f"Not found: {exc}"), file=sys.stderr)
        return 1
    except (TransportError, ProtocolError, TimeoutError) as exc:
        print(t(f"Ошибка связи с устройством: {exc}", f"Device communication error: {exc}"), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(t(f"Ошибка: {exc}", f"Error: {exc}"), file=sys.stderr)
        return 2
