"""Фоновый клиент демона для GUI.

Сеть и файлы — не в главном потоке: любой вызов `call()` уходит в
отдельный поток, результат возвращается через GLib.idle_add.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from gi.repository import GLib

from mousecfg import ipc

logger = logging.getLogger(__name__)


class DaemonClient:
    def __init__(self) -> None:
        self.client = ipc.Client()
        self.connected = False
        self.last_error = ""

    # -- синхронные вызовы (только из рабочего потока) ------------------
    def call(self, method: str, **params):
        return self.client.call(method, **params)

    # -- асинхронные вызовы для UI --------------------------------------
    def call_async(
        self,
        method: str,
        *,
        on_done: Callable[[object], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **params,
    ) -> None:
        def worker() -> None:
            try:
                result = self.client.call(method, **params)
                self.connected = True
                self.last_error = ""
            except ipc.IpcError as exc:
                self.connected = False
                self.last_error = str(exc)
                if on_error is not None:
                    GLib.idle_add(on_error, str(exc))
                return
            if on_done is not None:
                GLib.idle_add(on_done, result)

        threading.Thread(target=worker, daemon=True, name=f"rpc-{method}").start()
