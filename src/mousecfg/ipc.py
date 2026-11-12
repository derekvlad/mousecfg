"""IPC между GUI и демоном: простые сообщения JSON Lines через UNIX-сокет.

Сокет живёт в рантайм-каталоге пользователя (`$XDG_RUNTIME_DIR/mousecfg`),
права — только у владельца. Никакого TCP и никакого root.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import config
from .i18n import t

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 1 << 20  # 1 МиБ на сообщение — с запасом
PROTOCOL_VERSION = 1


class IpcError(RuntimeError):
    pass


def _socket_is_alive(path: Path) -> bool:
    """Проверить, отвечает ли кто-нибудь на этом сокете."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def socket_path() -> Path:
    return config.socket_path()


def _send(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    sock.sendall(data)


def _receive_line(buffer: bytes, sock: socket.socket) -> tuple[bytes, bytes]:
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise IpcError(t("соединение закрыто", "connection closed"))
        buffer += chunk
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise IpcError(t("слишком большое сообщение", "message too large"))
    line, _, rest = buffer.partition(b"\n")
    return line, rest


class Client:
    """Клиент демона: одно соединение, один запрос-ответ за раз."""

    def __init__(self, path: Path | None = None, *, timeout: float = 30.0) -> None:
        self.path = path or socket_path()
        self.timeout = timeout

    def call(self, method: str, **params: Any) -> Any:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.path))
                _send(sock, {"v": PROTOCOL_VERSION, "method": method, "params": params})
                line, _ = _receive_line(b"", sock)
        except FileNotFoundError as exc:
            raise IpcError(t(f"демон не запущен (нет сокета {self.path})", f"daemon is not running (no socket {self.path})")) from exc
        except ConnectionRefusedError as exc:
            raise IpcError(t(f"демон не отвечает на {self.path}", f"daemon does not respond on {self.path}")) from exc
        except TimeoutError as exc:
            raise IpcError(t("демон не ответил вовремя", "daemon did not respond in time")) from exc

        try:
            reply = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise IpcError(t("ответ демона не разбирается", "cannot parse the daemon response")) from exc

        if reply.get("error"):
            raise IpcError(str(reply["error"]))
        return reply.get("result")

    def is_available(self) -> bool:
        """Быстрая проверка: отвечает ли демон на ping."""
        try:
            return bool(self.call("ping").get("pong"))
        except (IpcError, AttributeError):
            return False

    def subscribe(self, callback: Callable[[dict[str, Any]], None], *, timeout: float = 0.0) -> None:
        """Подписаться на события демона (блокирует вызывающий поток)."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout or None)
            sock.connect(str(self.path))
            _send(sock, {"v": PROTOCOL_VERSION, "method": "subscribe", "params": {}})
            buffer = b""
            while True:
                line, buffer = _receive_line(buffer, sock)
                if not line:
                    continue
                callback(json.loads(line.decode("utf-8")))


class Server:
    """Сервер демона: принимает подключения, отдаёт их обработчику."""

    def __init__(self, handler: Callable[[str, dict[str, Any]], Any], path: Path | None = None) -> None:
        self.handler = handler
        self.path = path or socket_path()
        self._sock: socket.socket | None = None
        self._bound_inode: int | None = None
        self._threads: list[threading.Thread] = []
        self._subscribers: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- публикация событий ---------------------------------------------
    def publish(self, event: dict[str, Any]) -> None:
        data = json.dumps({"event": event}, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._lock:
            alive: list[socket.socket] = []
            for sock in self._subscribers:
                try:
                    sock.sendall(data)
                    alive.append(sock)
                except OSError:
                    continue
            self._subscribers = alive

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- жизненный цикл -------------------------------------------------
    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if _socket_is_alive(self.path):
                raise IpcError(t(f"демон уже запущен и слушает {self.path}", f"daemon is already running and listening on {self.path}"))
            logger.info(t("убираю осиротевший сокет %s", "removing orphaned socket %s"), self.path)
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self.path.chmod(0o600)
        self._bound_inode = self.path.stat().st_ino
        self._sock.listen(8)
        logger.info(t("демон слушает %s", "daemon listens on %s"), self.path)

    def serve_forever(self) -> None:
        if self._sock is None:
            raise IpcError(t("сервер не запущен", "server is not running"))
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            thread = threading.Thread(target=self._client_loop, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        # Убираем сокет только если он наш: иначе можно удалить файл
        # другого, только что запущенного демона.
        try:
            if self._bound_inode is not None and self.path.stat().st_ino == self._bound_inode:
                self.path.unlink()
        except (FileNotFoundError, OSError):
            pass
        self._bound_inode = None
        with self._lock:
            for sock in self._subscribers:
                with contextlib.suppress(OSError):
                    sock.close()
            self._subscribers = []

    # -- обработка соединения -------------------------------------------
    def _client_loop(self, conn: socket.socket) -> None:
        buffer = b""
        try:
            line, buffer = _receive_line(buffer, conn)
            request = json.loads(line.decode("utf-8"))
            method = str(request.get("method", ""))
            params = dict(request.get("params", {}))

            if method == "subscribe":
                with self._lock:
                    self._subscribers.append(conn)
                _send(conn, {"result": {"subscribed": True, "v": PROTOCOL_VERSION}})
                # Держим соединение, пока клиент не отвалится.
                while not self._stop.is_set():
                    try:
                        if not conn.recv(4096):
                            break
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                return

            try:
                result = self.handler(method, params)
                _send(conn, {"result": result})
            except Exception as exc:  # noqa: BLE001 — ошибки уходят клиенту как есть
                logger.warning(t("ошибка обработки %s: %s", "failed to handle %s: %s"), method, exc)
                _send(conn, {"error": str(exc)})
        except (IpcError, ValueError) as exc:
            logger.debug(t("соединение закрыто: %s", "connection closed: %s"), exc)
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def serialize(value: Any) -> Any:
    """Привести значение к JSON-совместимому виду."""
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serialize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def compact(items: Iterable[Any]) -> list[Any]:
    return [serialize(item) for item in items]
