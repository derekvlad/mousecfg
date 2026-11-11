"""GTK4 / libadwaita интерфейс mousecfg."""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from mousecfg.gui.app import main as _main

    return _main(argv)
