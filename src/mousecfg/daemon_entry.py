"""Точка входа демона (`mousecfg-daemon`)."""

from __future__ import annotations

import argparse
import logging
import sys

from . import config
from .daemon import Daemon, DaemonConfig
from .i18n import t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mousecfg-daemon",
        description=t("Демон mousecfg: следит за мышью и применяет профили. Во флеш мыши ничего не пишет.",
            "mousecfg daemon: watches the mouse and applies profiles. Nothing is written to onboard memory."),
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--dry-run", action="store_true", help=t("ничего не менять на устройстве", "do not change anything on the device"))
    parser.add_argument("--allow-onboard-switch", action="store_true")
    parser.add_argument("--profiles-dir", default=None, help=t(f"каталог профилей (по умолчанию {config.profiles_dir()})", f"profiles directory (default {config.profiles_dir()})"))
    parser.add_argument("--no-buttons", action="store_true", help=t("не перехватывать кнопки (только DPI и rate)", "do not intercept buttons (DPI and rate only)"))
    parser.add_argument("--no-apps", action="store_true", help=t("не следить за активным приложением", "do not watch the focused application"))
    parser.add_argument(
        "--no-auto-host",
        action="store_true",
        help=t("не повторять переход On-Board → Host автоматически (даже если согласие сохранено)",
            "do not repeat the On-Board → Host switch automatically (even if consent was recorded)"),
    )
    args = parser.parse_args(argv)

    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    options = DaemonConfig(
        dry_run=args.dry_run,
        allow_onboard_switch=args.allow_onboard_switch,
        profiles_dir=(config.profiles_dir() if args.profiles_dir is None else __import__("pathlib").Path(args.profiles_dir)),
        watch_apps=not args.no_apps,
        apply_buttons=not args.no_buttons,
        auto_host=not args.no_auto_host,
    )
    return Daemon(options).run()


if __name__ == "__main__":
    raise SystemExit(main())
