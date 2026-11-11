"""Точка входа демона (`mousecfg-daemon`)."""

from __future__ import annotations

import argparse
import logging
import sys

from . import config
from .daemon import Daemon, DaemonConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mousecfg-daemon",
        description="Демон mousecfg: следит за мышью и применяет профили. Во флеш мыши ничего не пишет.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--dry-run", action="store_true", help="ничего не менять на устройстве")
    parser.add_argument("--allow-onboard-switch", action="store_true")
    parser.add_argument("--profiles-dir", default=None, help=f"каталог профилей (по умолчанию {config.profiles_dir()})")
    parser.add_argument("--no-buttons", action="store_true", help="не перехватывать кнопки (только DPI и rate)")
    parser.add_argument("--no-apps", action="store_true", help="не следить за активным приложением")
    args = parser.parse_args(argv)

    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    options = DaemonConfig(
        dry_run=args.dry_run,
        allow_onboard_switch=args.allow_onboard_switch,
        profiles_dir=(config.profiles_dir() if args.profiles_dir is None else __import__("pathlib").Path(args.profiles_dir)),
        watch_apps=not args.no_apps,
        apply_buttons=not args.no_buttons,
    )
    return Daemon(options).run()


if __name__ == "__main__":
    raise SystemExit(main())
