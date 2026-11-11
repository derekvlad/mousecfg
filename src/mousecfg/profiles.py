"""Профили мыши: TOML-схема, хранилище и сопоставление с устройством.

Профиль целиком живёт на хосте. Поле `storage` существует только для
документирования: значение "onboard" отклоняется с ошибкой, потому что
проект не пишет во встроенную память мыши.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Символьные имена кнопок -> распространённые HID++/evdev control id.
# Список пополняется по мере проверки на железе; неизвестное имя — ошибка,
# а не «догадка».
BUTTON_ALIASES: dict[str, int] = {
    "left": 0x0050,
    "right": 0x0051,
    "middle": 0x0052,
    "side1": 0x0053,  # задняя боковая кнопка (Back)
    "side2": 0x0056,  # передняя боковая кнопка (Forward)
    "back": 0x0053,
    "forward": 0x0056,
    "dpi": 0x00C4,  # переключение DPI
}

_RATE_RE = re.compile(r"^(?P<value>\d+)(?P<unit>ms|hz)?$", re.IGNORECASE)

# Несколько удобных именованных цветов, чтобы писать в профиле «red», а не hex.
COLOR_NAMES: dict[str, int] = {
    "off": 0x000000,
    "black": 0x000000,
    "white": 0xFFFFFF,
    "red": 0xFF0000,
    "green": 0x00FF00,
    "blue": 0x0000FF,
    "yellow": 0xFFFF00,
    "cyan": 0x00FFFF,
    "magenta": 0xFF00FF,
    "orange": 0xFF8000,
    "purple": 0x8000FF,
    "pink": 0xFF40A0,
}

# Эффекты подсветки и их параметры. Копия таблицы из hidpp.device, но здесь
# она нужна без устройства: файл профилей проверяется и без мыши.
LIGHTING_EFFECTS: dict[str, dict[str, tuple[int, int]]] = {
    "disabled": {},
    "static": {"color": (0, 3), "ramp": (3, 1)},
    "pulse": {"color": (0, 3), "speed": (3, 1)},
    "cycle": {"period": (5, 2), "intensity": (7, 1)},
    "wave": {"period": (6, 2), "direction": (9, 1)},
    "breathe": {"color": (0, 3), "period": (3, 2), "form": (5, 1), "intensity": (6, 1)},
    "ripple": {"color": (0, 3), "period": (4, 2)},
    "decomposition": {"period": (6, 2), "intensity": (8, 1)},
}

# Человекочитаемые имена параметров -> байтовое смещение/размер (как на железе).
LIGHTING_PARAM_MAX = {"color": 0xFFFFFF, "period": 0xFFFF, "intensity": 0xFF, "form": 0xFF, "ramp": 0xFF, "speed": 0xFF, "direction": 0xFF}


def validate_lighting(lighting: Any, *, profile_id: str = "?") -> None:
    """Проверить блок подсветки профиля, не трогая устройство."""
    if lighting is None:
        return
    if not isinstance(lighting, dict):
        raise ProfileError(f"профиль {profile_id!r}: подсветка должна быть таблицей")
    effect = str(lighting.get("effect", "static")).lower()
    if effect not in LIGHTING_EFFECTS:
        known = ", ".join(sorted(LIGHTING_EFFECTS))
        raise ProfileError(f"профиль {profile_id!r}: неизвестный эффект подсветки {effect!r}; известные: {known}")
    if "brightness" in lighting:
        value = lighting["brightness"]
        if not isinstance(value, int) or not 0 <= value <= 100:
            raise ProfileError(f"профиль {profile_id!r}: яркость подсветки — целое 0..100")
    if "color" in lighting:
        color = lighting["color"]
        if isinstance(color, str):
            color = _parse_color(color)
        if not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ProfileError(f"профиль {profile_id!r}: цвет подсветки — #rrggbb или целое 0..0xffffff")
    zone = lighting.get("zone", 0)
    if not isinstance(zone, int) or zone < 0:
        raise ProfileError(f"профиль {profile_id!r}: зона подсветки должна быть неотрицательным целым")


def parse_color(value: Any) -> int | None:
    """'#rrggbb' / 'rrggbb' / 0xRRGGBB / 'red' -> целое 0xRRGGBB."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProfileError("цвет подсветки: булево значение недопустимо")
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFF:
            return value
        raise ProfileError(f"цвет подсветки {value:#x} вне диапазона 0..0xffffff")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in COLOR_NAMES:
            return COLOR_NAMES[text]
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 6:
            try:
                return int(text, 16)
            except ValueError as exc:
                raise ProfileError(f"цвет подсветки {value!r} не разбирается") from exc
        if len(text) == 3:
            try:
                return int(text[0] * 2 + text[1] * 2 + text[2] * 2, 16)
            except ValueError as exc:
                raise ProfileError(f"цвет подсветки {value!r} не разбирается") from exc
    raise ProfileError(f"цвет подсветки {value!r} не разбирается")


def _parse_color(value: Any) -> int:
    parsed = parse_color(value)
    assert parsed is not None
    return parsed


def brightness_to_intensity(brightness: int) -> int:
    """0..100 (яркость в профиле) -> 0..255 (байт intensity у устройства).

    Но если эффект понимает только color (static), параметра intensity у
    него нет — тогда яркость влияет только на выбор оттенка (см. engine).
    """
    return max(0, min(255, round(brightness * 255 / 100)))


class ProfileError(ValueError):
    pass


def parse_rate(value: Any) -> int:
    """Принять '1000Hz', '1ms', 1000 или 1 и вернуть миллисекунды."""
    if isinstance(value, bool):
        raise ProfileError("report_rate: булево значение недопустимо")
    if isinstance(value, int):
        if value <= 20:
            return value
        if value in (125, 250, 500, 1000):
            return 1000 // value
        raise ProfileError(f"report_rate: непонятное число {value}")
    if isinstance(value, str):
        match = _RATE_RE.match(value.strip())
        if not match:
            raise ProfileError(f"report_rate: непонятная запись {value!r}")
        number = int(match.group("value"))
        unit = (match.group("unit") or "ms").lower()
        if unit == "hz":
            if number not in (125, 250, 500, 1000):
                raise ProfileError(f"report_rate: {number} Гц не поддерживается")
            return 1000 // number
        return number
    raise ProfileError(f"report_rate: неподдерживаемый тип {type(value).__name__}")


@dataclass
class Profile:
    id: str
    name: str = ""
    dpi: int | None = None
    report_rate_ms: int | None = None
    buttons: dict[str, Any] = field(default_factory=dict)
    match_serials: list[str] = field(default_factory=list)
    match_apps: list[str] = field(default_factory=list)
    storage: str = "host"
    lighting: dict[str, Any] | None = None

    def validate(self) -> None:
        if self.storage != "host":
            raise ProfileError(
                f"профиль {self.id!r}: storage={self.storage!r}. Проект хранит профили на хосте "
                f"и не пишет во встроенную память мыши, поэтому 'onboard' не поддерживается"
            )
        if self.dpi is not None and not 0 < self.dpi < 0xFFFF:
            raise ProfileError(f"профиль {self.id!r}: DPI {self.dpi} вне диапазона")
        for name, action in self.buttons.items():
            if name not in BUTTON_ALIASES:
                raise ProfileError(
                    f"профиль {self.id!r}: неизвестная кнопка {name!r}; "
                    f"известные: {', '.join(sorted(BUTTON_ALIASES))}"
                )
            if not isinstance(action, (str, dict)):
                raise ProfileError(f"профиль {self.id!r}: кнопка {name!r} — ожидается строка или таблица")
        validate_lighting(self.lighting, profile_id=self.id)


@dataclass
class ProfileSet:
    version: int
    device: str = ""
    serials: list[str] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    macros_raw: list[dict] = field(default_factory=list)


def _macro_ids(macros_raw: list[dict]) -> set[str]:
    return {str(item.get("id")) for item in macros_raw if item.get("id")}


def _validate_button_macros(profile: Profile, macro_ids: set[str], macros_present: bool) -> None:
    for name, action in profile.buttons.items():
        referenced = None
        if isinstance(action, dict):
            referenced = action.get("macro")
            if referenced is None and "action" in action:
                nested = action["action"]
                if isinstance(nested, dict):
                    referenced = nested.get("macro")
        if referenced is None:
            continue
        if referenced not in macro_ids:
            hint = "макросы в файле не определены" if not macros_present else f"определены: {', '.join(sorted(macro_ids))}"
            raise ProfileError(f"профиль {profile.id!r}: кнопка {name!r} ссылается на макрос {referenced!r}, а его нет ({hint})")


def parse_profiles(text: str, *, source: str = "<string>") -> ProfileSet:
    data = tomllib.loads(text)
    version = data.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ProfileError(f"{source}: версия схемы {version}, поддерживается {SCHEMA_VERSION}")

    device_section = data.get("device", {})
    if isinstance(device_section, list):
        device_section = device_section[0] if device_section else {}
    device = str(device_section.get("id", ""))
    serials = [str(item) for item in device_section.get("serials", [])]

    profiles: list[Profile] = []
    for raw in data.get("profile", []):
        rate = raw.get("report_rate")
        profile = Profile(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            dpi=int(raw["dpi"]) if "dpi" in raw else None,
            report_rate_ms=parse_rate(rate) if rate is not None else None,
            buttons=dict(raw.get("buttons", {})),
            match_serials=[str(item) for item in raw.get("match_serials", [])],
            match_apps=[str(item) for item in raw.get("match_apps", [])],
            storage=str(raw.get("storage", "host")),
            lighting=_parse_lighting(raw.get("lighting")),
        )
        profile.validate()
        profiles.append(profile)

    if not profiles:
        raise ProfileError(f"{source}: нет ни одного профиля")

    macros_raw = list(data.get("macro", []))
    macro_ids = _macro_ids(macros_raw)
    for profile in profiles:
        _validate_button_macros(profile, macro_ids, bool(macros_raw))

    return ProfileSet(version=version, device=device, serials=serials, profiles=profiles, macros_raw=macros_raw)


def load_profile_file(path: Path) -> ProfileSet:
    return parse_profiles(path.read_text(encoding="utf-8"), source=str(path))


def load_dir(directory: Path) -> list[ProfileSet]:
    if not directory.exists():
        return []
    return [load_profile_file(path) for path in sorted(directory.glob("*.toml"))]


def matches(profile: Profile, *, serial: str = "", app: str = "") -> bool:
    serial_ok = not profile.match_serials or (serial and serial in profile.match_serials)
    app_ok = not profile.match_apps or bool(app) and _app_matches(app, profile.match_apps)
    return bool(serial_ok and app_ok)


def _app_matches(app: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("*"):
            if app.startswith(pattern[:-1]):
                return True
        elif app.lower() == pattern.lower():
            return True
    return False


def select(profile_set: ProfileSet, *, serial: str = "", app: str = "") -> Profile | None:
    """Выбрать самый конкретный подходящий профиль.

    Профиль без условий — общий; профиль с match_apps конкретнее, чем без
    них, поэтому при совпадении приложения он побеждает. Порядок в файле
    разрешает ничьи: побеждает первый из равных.
    """
    best: Profile | None = None
    best_score = -1
    for profile in profile_set.profiles:
        score = 0
        if profile.match_serials:
            if not serial or serial not in profile.match_serials:
                continue
            score += 1
        if profile.match_apps:
            if not app or not _app_matches(app, profile.match_apps):
                continue
            score += 2
        if score > best_score:
            best, best_score = profile, score
    return best


def get(profile_set: ProfileSet, profile_id: str) -> Profile | None:
    """Найти профиль по id ('' или отсутствующий id — None)."""
    if not profile_id:
        return None
    return next((profile for profile in profile_set.profiles if profile.id == profile_id), None)


def choose(
    profile_set: ProfileSet, *, serial: str = "", app: str = "", pinned_id: str = ""
) -> Profile | None:
    """Выбрать профиль с учётом выбора пользователя.

    Профиль, совпавший по приложению (`match_apps`), всегда важнее ручного
    выбора: он описывает именно это приложение. Но если приложение ничему не
    совпало, удерживается профиль, который пользователь выбрал вручную
    (`pinned_id`) — иначе автоматический пересчёт на каждой смене фокуса
    сбрасывал бы выбор на первый профиль в файле. Без ручного выбора
    поведение прежнее — обычный `select`.
    """
    matched = select(profile_set, serial=serial, app=app)
    if matched is not None and matched.match_apps:
        return matched
    pinned = get(profile_set, pinned_id)
    return pinned if pinned is not None else matched


def _write_checked(path: Path, text: str) -> None:
    """Записать TOML только если он разбирается и не ломает ссылки на макросы.

    Файл профилей — единственный источник настроек: одна опечатка в нём
    обнуляет все профили устройства. Поэтому сначала проверяем результат
    парсером, и лишь потом пишем на диск. Файл без единого профиля допустим
    (например, после удаления последнего), но синтаксис всё равно проверяем.
    """
    tomllib.loads(text)
    if _has_profile_block(text):
        parse_profiles(text, source=str(path))
    path.write_text(text, encoding="utf-8")


def add_profile(path: Path, profile: Profile) -> None:
    """Дописать профиль в TOML-файл, сохранив существующее содержимое."""
    profile.validate()
    text = path.read_text(encoding="utf-8") if path.exists() else f"version = {SCHEMA_VERSION}\n"
    if _profile_id_in(text, profile.id):
        raise ProfileError(f"профиль {profile.id!r} уже есть в {path}")
    _write_checked(path, text.rstrip("\n") + "\n\n" + render_profile(profile) + "\n")


def upsert_profile(path: Path, profile: Profile) -> bool:
    """Добавить профиль или заменить существующий с тем же id.

    Возвращает True, если профиль был заменён, и False, если добавлен.
    Макросы и остальные блоки файла не трогаются — проект не пишет в мышь,
    но конфигурацию пользователя тоже не переписывает целиком.
    """
    profile.validate()
    text = path.read_text(encoding="utf-8") if path.exists() else f"version = {SCHEMA_VERSION}\n"
    if _profile_id_in(text, profile.id):
        blocks = _split_blocks(text)
        replaced = False
        output: list[str] = []
        for block in blocks:
            if not replaced and block.lstrip().startswith("[[profile]]") and _block_id(block) == profile.id:
                output.append(render_profile(profile))
                replaced = True
            else:
                output.append(block.rstrip("\n"))
        _write_checked(path, "\n\n".join(output).rstrip("\n") + "\n")
        return True
    _write_checked(path, text.rstrip("\n") + "\n\n" + render_profile(profile) + "\n")
    return False


def find_profile(profile_set: ProfileSet, profile_id: str) -> Profile | None:
    return next((item for item in profile_set.profiles if item.id == profile_id), None)


def known_macro_ids(path: Path) -> set[str]:
    """Id макросов, уже определённых в файле (для проверки ссылок)."""
    if not path.exists():
        return set()
    return _macro_ids(list(tomllib.loads(path.read_text(encoding="utf-8")).get("macro", [])))


def upsert_macro(path: Path, raw: dict, *, replace: bool = False) -> bool:
    """Добавить макрос в файл или заменить существующий с тем же id.

    Возвращает True, если макрос был заменён. Макросы — отдельные блоки
    [[macro]], поэтому профили не трогаются.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else f"version = {SCHEMA_VERSION}\n"
    macro_id = str(raw["id"])
    existing = _macro_ids(list(tomllib.loads(text).get("macro", [])))
    if macro_id in existing:
        if not replace:
            raise ProfileError(f"макрос {macro_id!r} уже есть в {path} (добавьте --replace, чтобы заменить)")
        blocks = _split_blocks(text)
        output: list[str] = []
        replaced = False
        for block in blocks:
            if not replaced and block.lstrip().startswith("[[macro]]") and _block_id(block) == macro_id:
                output.append(render_macro(raw))
                replaced = True
            else:
                output.append(block.rstrip("\n"))
        _write_checked(path, "\n\n".join(output).rstrip("\n") + "\n")
        return True
    _write_checked(path, text.rstrip("\n") + "\n\n" + render_macro(raw) + "\n")
    return False


def remove_macro(path: Path, macro_id: str) -> None:
    text = path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    remaining = [
        block
        for block in blocks
        if not (block.lstrip().startswith("[[macro]]") and _block_id(block) == macro_id)
    ]
    if len(remaining) == len(blocks):
        raise ProfileError(f"макрос {macro_id!r} не найден в {path}")
    _write_checked(path, "\n\n".join(block.rstrip("\n") for block in remaining).rstrip("\n") + "\n")


def remove_profile(path: Path, profile_id: str) -> None:
    """Удалить блок профиля из файла. Если профиль последний — файл остаётся пустым."""
    text = path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    remaining = [block for block in blocks if _block_id(block) != profile_id]
    if len(remaining) == len(blocks):
        raise ProfileError(f"профиль {profile_id!r} не найден в {path}")
    _write_checked(path, "\n\n".join(block.rstrip("\n") for block in remaining).rstrip("\n") + "\n")


def render_profile(profile: Profile) -> str:
    lines = ["[[profile]]", f'id = "{profile.id}"']
    if profile.name:
        lines.append(f'name = "{profile.name}"')
    if profile.dpi is not None:
        lines.append(f"dpi = {profile.dpi}")
    if profile.report_rate_ms is not None:
        lines.append(f'report_rate = "{_rate_text(profile.report_rate_ms)}"')
    if profile.match_apps:
        joined = ", ".join(f'"{item}"' for item in profile.match_apps)
        lines.append(f"match_apps = [{joined}]")
    if profile.match_serials:
        joined = ", ".join(f'"{item}"' for item in profile.match_serials)
        lines.append(f"match_serials = [{joined}]")
    if profile.lighting is not None:
        lines.append("[profile.lighting]")
        for name, value in _render_lighting_fields(profile.lighting).items():
            lines.append(f"{name} = {value}")
    if profile.buttons:
        lines.append("[profile.buttons]")
        for name, action in profile.buttons.items():
            lines.append(f'"{name}" = {_render_action(action)}')
    return "\n".join(lines)


def parse_lighting(raw: Any) -> dict[str, Any] | None:
    """Публичная обёртка для разбора блока подсветки (используется в RPC)."""
    return _parse_lighting(raw)


def _parse_lighting(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileError("подсветка: ожидается таблица [profile.lighting]")
    parsed: dict[str, Any] = {}
    if "effect" in raw:
        parsed["effect"] = str(raw["effect"]).lower()
    if "color" in raw:
        parsed["color"] = _parse_color(raw["color"])
    if "brightness" in raw:
        parsed["brightness"] = int(raw["brightness"])
    if "zone" in raw:
        parsed["zone"] = int(raw["zone"])
    for key in ("period", "intensity", "form", "ramp", "speed", "direction"):
        if key in raw:
            parsed[key] = int(raw[key])
    validate_lighting(parsed)
    return parsed


def _render_lighting_fields(lighting: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    if "effect" in lighting:
        fields["effect"] = f'"{lighting["effect"]}"'
    if "color" in lighting:
        fields["color"] = f'"#{int(lighting["color"]):06x}"'
    if "brightness" in lighting:
        fields["brightness"] = str(int(lighting["brightness"]))
    if "zone" in lighting:
        fields["zone"] = str(int(lighting["zone"]))
    for key in ("period", "intensity", "form", "ramp", "speed", "direction"):
        if key in lighting:
            fields[key] = str(int(lighting[key]))
    return fields



def _render_action(action: object) -> str:
    if isinstance(action, dict):
        if "macro" in action:
            return "{ macro = " + f'"{action["macro"]}"' + " }"
        if "key" in action:
            return "{ key = " + f'"{action["key"]}"' + " }"
    return f'"{action}"'


def render_macro(raw: dict) -> str:
    """Отобразить макрос (как он есть в TOML-словаре) обратно в текст.

    Макросы — это данные; мы их не переписываем по частям, а сохраняем
    структуру шагов как есть, чтобы не потерять поля (note и т. п.).
    """
    lines = ["[[macro]]", f'id = "{raw["id"]}"']
    if raw.get("name"):
        lines.append(f'name = "{raw["name"]}"')
    if raw.get("interval_ms") is not None:
        lines.append(f'interval_ms = {int(raw["interval_ms"])}')
    steps = raw.get("steps", [])
    rendered_steps = []
    for step in steps:
        rendered_steps.append(_render_macro_step(step))
    if rendered_steps:
        lines.append("steps = [")
        for item in rendered_steps:
            lines.append(f"    {item},")
        lines.append("]")
    return "\n".join(lines)


def _render_macro_step(step: object) -> str:
    if not isinstance(step, dict):
        return f'{{ tap = "{step}" }}'
    parts = []
    for key in ("tap", "key"):
        if key in step:
            parts.append(f'{key} = "{step[key]}"')
    if "wait" in step and "tap" not in step and "key" not in step:
        parts.append("wait = true")
    if step.get("delay_ms") is not None:
        parts.append(f'delay_ms = {int(step["delay_ms"])}')
    if step.get("note"):
        parts.append(f'note = "{step["note"]}"')
    return "{ " + ", ".join(parts) + " }"


def _rate_text(milliseconds: int) -> str:
    if milliseconds in (1, 2, 4, 8):
        return "1ms" if milliseconds == 1 else f"{milliseconds}ms"
    return f"{milliseconds}ms"


def _split_blocks(text: str) -> list[str]:
    """Разбить файл на смысловые блоки: шапка, [[profile]], [[macro]], [[device]]."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("[[") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _block_id(block: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("id"):
            return stripped.split("=", 1)[1].strip().strip('"')
    return None


def _profile_id_in(text: str, profile_id: str) -> bool:
    for block in _split_blocks(text):
        if block.lstrip().startswith("[[profile]]") and _block_id(block) == profile_id:
            return True
    return False


def _has_profile_block(text: str) -> bool:
    return any(block.lstrip().startswith("[[profile]]") for block in _split_blocks(text))
