"""Снимки onboard-конфигурации (бэкап) — только чтение и файлы.

Перед любой записью в память мыши проект обязан сохранить текущее
состояние. Восстановить потом можно двумя путями:

* записать снимок назад (`mousecfg onboard restore`),
* вручную в веб-конфигураторе (вкладка Onboard configuration → Import).

Снимок — это прочитанный кадр `0e/a5` (плюс, если получится, раскладка
`08/a5`) вместе с расшифрованными полями и отпечатком. Файл JSON, язык
полей не зависит от UI: его можно открыть глазами.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mousecfg import config
from mousecfg.i18n import t

from .framing import MkespnReply
from .parsers import DpiConfig, parse_dpi_config

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1


@dataclass
class Snapshot:
    """Сохранённая конфигурация одной мыши на момент времени."""

    created_at: float
    device: str
    serial: str
    usb_id: str
    model: str = ""
    # Полный кадр 0e/a5 в hex — по нему целиком восстанавливается запись.
    config_frame: str = ""
    # Раскладка кнопок (08/a5), если устройство её отдаёт.
    keys_frame: str = ""
    # Расшифровка только для человека (в запись не попадает).
    decoded: dict = field(default_factory=dict)
    version: int = SNAPSHOT_VERSION

    @property
    def config_bytes(self) -> bytes:
        return bytes.fromhex(self.config_frame)

    @property
    def keys_bytes(self) -> bytes:
        return bytes.fromhex(self.keys_frame) if self.keys_frame else b""

    def fingerprint(self) -> str:
        blob = self.config_frame + "|" + self.keys_frame
        return hashlib.sha256(blob.encode("ascii")).hexdigest()

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        dpi = self.decoded.get("dpi")
        levels = ", ".join(str(item) for item in self.decoded.get("dpi_levels", []))
        parts = [t(f"снимок от {when}", f"snapshot taken {when}")]
        if self.model:
            parts.append(self.model)
        if dpi:
            parts.append(t(f"DPI {dpi} ({levels})", f"DPI {dpi} ({levels})"))
        parts.append(f"sha256:{self.fingerprint()[:12]}")
        return "  ".join(parts)


def snapshot_from_reads(
    reply: MkespnReply,
    *,
    device: str = "",
    serial: str = "",
    usb_id: str = "",
    model: str = "",
    keys_reply: MkespnReply | None = None,
) -> Snapshot:
    """Собрать снимок из прочитанных кадров `0e/a5` (и, если есть, `08/a5`)."""
    parsed: DpiConfig = parse_dpi_config(reply)
    decoded = {
        "dpi": parsed.current_dpi,
        "dpi_levels": parsed.levels,
        "report_rate_hz": parsed.poll_rate_hz,
        "light_mode": parsed.light_mode,
        "dpi_count": parsed.dpi_count,
        "tail": parsed.tail,
        "raw": reply.data.hex(" "),
    }
    return Snapshot(
        created_at=time.time(),
        device=device,
        serial=serial,
        usb_id=usb_id,
        model=model,
        config_frame=bytes(reply.data).hex(),
        keys_frame=bytes(keys_reply.data).hex() if keys_reply is not None else "",
        decoded=decoded,
    )


def save(snapshot: Snapshot) -> Path:
    """Записать снимок в ~/.config/mousecfg/backups и обновить «последний»."""
    directory = config.backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(snapshot.created_at))
    slug = _slug(snapshot.usb_id or snapshot.serial or "mouse")
    payload = json.dumps(asdict(snapshot), ensure_ascii=False, indent=2)
    named = directory / f"onboard-{slug}-{stamp}.json"
    named.write_text(payload, encoding="utf-8")
    (directory / f"onboard-{slug}-last.json").write_text(payload, encoding="utf-8")
    logger.info(t("снимок onboard-конфигурации сохранён: %s", "onboard configuration snapshot saved: %s"), named)
    return named


def load_latest(*, usb_id: str = "", serial: str = "") -> Snapshot | None:
    """Последний снимок устройства (по usb_id/серийнику, иначе самый свежий)."""
    directory = config.backups_dir()
    if not directory.exists():
        return None
    candidates: list[Snapshot] = []
    for path in sorted(directory.glob("onboard-*.json")):
        if path.name.endswith("-last.json"):
            continue
        snapshot = _read(path)
        if snapshot is not None:
            candidates.append(snapshot)
    if not candidates:
        return None
    if usb_id or serial:
        matching = [
            item for item in candidates
            if (usb_id and item.usb_id == usb_id) or (serial and item.serial == serial)
        ]
        if matching:
            candidates = matching
    return max(candidates, key=lambda item: item.created_at)


def _read(path: Path) -> Snapshot | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if raw.get("version") != SNAPSHOT_VERSION:
        return None
    known = {name: raw[name] for name in Snapshot.__dataclass_fields__ if name in raw}
    try:
        return Snapshot(**known)
    except TypeError:
        return None


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text.lower())
    return "-".join(part for part in cleaned.split("-") if part)[:60] or "mouse"


__all__ = ["Snapshot", "load_latest", "save", "snapshot_from_reads"]
