"""GTK4 / libadwaita интерфейс mousecfg.

Приложение — тонкий клиент демона: вся работа с устройством идёт через
JSON-RPC, поэтому GUI ни при каких условиях не пишет в память мыши напрямую.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from mousecfg.gui.window import MainWindow  # noqa: E402

APP_ID = "io.github.mousecfg.MouseCfg"
APP_NAME = "mousecfg"


class MouseCfgApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.window: MainWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
            self.window.set_title(APP_NAME)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    app = MouseCfgApplication()
    return app.run(argv if argv is not None else sys.argv)
