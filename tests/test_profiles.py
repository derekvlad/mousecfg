"""Тесты профилей: схема, парсинг частоты, отказ от onboard-хранения."""

from __future__ import annotations

import pytest

from mousecfg import profiles

GOOD = """
version = 1

[[device]]
id = "logitech-g102"
serials = ["205C35585247"]

[[profile]]
id = "default"
name = "Default"
dpi = 1600
report_rate = "1000Hz"
[profile.buttons]
"side1" = "browser-back"

[[profile]]
id = "fps"
name = "FPS"
dpi = 800
match_apps = ["cs2", "steam_app_*"]
"""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1000Hz", 1), ("500hz", 2), ("1ms", 1), ("4", 4), (8, 8), (1000, 1)],
)
def test_parse_rate(value, expected):
    assert profiles.parse_rate(value) == expected


def test_parse_rate_rejects_garbage():
    with pytest.raises(profiles.ProfileError):
        profiles.parse_rate("быстро")
    with pytest.raises(profiles.ProfileError):
        profiles.parse_rate(333)


def test_parse_good_profile_set():
    parsed = profiles.parse_profiles(GOOD)
    assert parsed.device == "logitech-g102"
    assert [profile.id for profile in parsed.profiles] == ["default", "fps"]
    assert parsed.profiles[0].report_rate_ms == 1
    assert parsed.profiles[1].dpi == 800


def test_onboard_storage_is_rejected():
    text = GOOD + '\n[[profile]]\nid = "bad"\nstorage = "onboard"\n'
    with pytest.raises(profiles.ProfileError, match="onboard"):
        profiles.parse_profiles(text)


def test_unknown_button_is_rejected():
    text = GOOD.replace('"side1"', '"telepathy"')
    with pytest.raises(profiles.ProfileError, match="кнопка"):
        profiles.parse_profiles(text)


def test_wrong_schema_version_is_rejected():
    with pytest.raises(profiles.ProfileError, match="версия"):
        profiles.parse_profiles(GOOD.replace("version = 1", "version = 99"))


def test_selection_prefers_app_match_then_default():
    parsed = profiles.parse_profiles(GOOD)
    assert profiles.select(parsed, app="cs2").id == "fps"
    assert profiles.select(parsed, app="steam_app_42").id == "fps"
    assert profiles.select(parsed, app="firefox").id == "default"


def test_selection_uses_serial():
    text = """
version = 1
[[profile]]
id = "mine"
dpi = 800
match_serials = ["205C35585247"]
[[profile]]
id = "fallback"
dpi = 1600
"""
    parsed = profiles.parse_profiles(text)
    assert profiles.select(parsed, serial="205C35585247").id == "mine"
    assert profiles.select(parsed, serial="OTHER").id == "fallback"


def test_choose_keeps_manual_pick_between_focus_changes():
    """Ручной выбор не сбрасывается на первый профиль при смене приложения.

    Регрессия: профиль без `match_apps` выбирался заново на каждой смене
    фокуса, и `select` возвращал первый профиль в файле — выбор «прыгал»
    обратно. `choose` удерживает закреплённый профиль.
    """
    parsed = profiles.parse_profiles(GOOD)  # default (без условий), fps (match_apps)
    # Пользователь вручную выбрал профиль без условий — он должен остаться.
    assert profiles.choose(parsed, app="firefox", pinned_id="default").id == "default"
    # Профиль с match_apps всё равно важнее ручного выбора.
    assert profiles.choose(parsed, app="cs2", pinned_id="default").id == "fps"
    # Без закрепления — прежний автоматический выбор.
    assert profiles.choose(parsed, app="cs2").id == "fps"


def test_choose_ignores_pin_to_unknown_profile():
    parsed = profiles.parse_profiles(GOOD)
    assert profiles.choose(parsed, pinned_id="нет-такого").id == "default"


def test_add_profile_keeps_file_valid(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"base\"\ndpi = 800\n", encoding="utf-8")

    profiles.add_profile(
        path,
        profiles.Profile(id="extra", name="Ещё", dpi=2400, report_rate_ms=2, buttons={"side1": "ignore"}),
    )

    parsed = profiles.load_profile_file(path)
    assert [item.id for item in parsed.profiles] == ["base", "extra"]
    assert parsed.profiles[1].buttons == {"side1": "ignore"}


def test_add_profile_rejects_duplicate(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"base\"\ndpi = 800\n", encoding="utf-8")
    with pytest.raises(profiles.ProfileError, match="уже есть"):
        profiles.add_profile(path, profiles.Profile(id="base", dpi=1600))


def test_add_profile_refuses_dangling_macro(tmp_path):
    """Ссылка на несуществующий макрос ломает весь файл — запись должна быть отвергнута."""
    path = tmp_path / "profiles.toml"
    original = "version = 1\n\n[[profile]]\nid = \"base\"\ndpi = 800\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(profiles.ProfileError, match="nope"):
        profiles.add_profile(path, profiles.Profile(id="x", dpi=1600, buttons={"side2": {"macro": "nope"}}))

    assert path.read_text(encoding="utf-8") == original


def test_upsert_replaces_existing_profile(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n"
        "[[macro]]\nid = \"m\"\nsteps = [ { tap = \"a\" } ]\n",
        encoding="utf-8",
    )
    replaced = profiles.upsert_profile(
        path,
        profiles.Profile(id="one", name="Обновлён", dpi=1600, buttons={"side1": "copy"}),
    )
    assert replaced is True
    parsed = profiles.load_profile_file(path)
    assert parsed.profiles[0].dpi == 1600
    assert parsed.macros_raw and parsed.macros_raw[0]["id"] == "m"


def test_upsert_adds_when_missing(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n", encoding="utf-8")
    replaced = profiles.upsert_profile(path, profiles.Profile(id="two", dpi=1600))
    assert replaced is False
    assert [item.id for item in profiles.load_profile_file(path).profiles] == ["one", "two"]


def test_upsert_accepts_existing_macro_reference(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n"
        "[[macro]]\nid = \"burst\"\nsteps = [ { tap = \"a\" } ]\n",
        encoding="utf-8",
    )
    profiles.upsert_profile(path, profiles.Profile(id="one", dpi=1600, buttons={"side2": {"macro": "burst"}}))
    assert profiles.load_profile_file(path).profiles[0].buttons == {"side2": {"macro": "burst"}}


def test_known_macro_ids_reads_file(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n"
        "[[macro]]\nid = \"a\"\nsteps = [ { tap = \"x\" } ]\n\n"
        "[[macro]]\nid = \"b\"\nsteps = [ { tap = \"y\" } ]\n",
        encoding="utf-8",
    )
    assert profiles.known_macro_ids(path) == {"a", "b"}
    assert profiles.known_macro_ids(tmp_path / "nope.toml") == set()


def test_remove_profile_keeps_rest(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n[[profile]]\nid = \"two\"\ndpi = 1600\n",
        encoding="utf-8",
    )
    profiles.remove_profile(path, "one")
    assert [item.id for item in profiles.load_profile_file(path).profiles] == ["two"]


def test_remove_missing_profile_is_rejected(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n", encoding="utf-8")
    with pytest.raises(profiles.ProfileError, match="не найден"):
        profiles.remove_profile(path, "two")


def test_remove_profile_keeps_macros(tmp_path):
    """Макросы — отдельные блоки: удаление профиля не должно их задевать."""
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n"
        "[[macro]]\nid = \"m\"\nsteps = [ { tap = \"a\" } ]\n",
        encoding="utf-8",
    )
    profiles.remove_profile(path, "one")
    text = path.read_text(encoding="utf-8")
    assert "[[macro]]" in text
    assert 'id = "m"' in text


def test_upsert_macro_adds_and_profiles_stay(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n", encoding="utf-8")

    replaced = profiles.upsert_macro(
        path,
        {"id": "burst", "name": "Серия", "interval_ms": 40, "steps": [{"tap": "a"}, {"wait": True, "delay_ms": 50}]},
    )

    assert replaced is False
    parsed = profiles.load_profile_file(path)
    assert [item.id for item in parsed.profiles] == ["one"]
    assert parsed.macros_raw[0]["id"] == "burst"


def test_upsert_macro_rejects_duplicate_without_replace(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[macro]]\nid = \"m\"\nsteps = [ { tap = \"a\" } ]\n", encoding="utf-8"
    )
    with pytest.raises(profiles.ProfileError, match="уже есть"):
        profiles.upsert_macro(path, {"id": "m", "steps": [{"tap": "b"}]})


def test_upsert_macro_replace_keeps_profiles(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n"
        "[[macro]]\nid = \"m\"\nsteps = [ { tap = \"a\" } ]\n",
        encoding="utf-8",
    )
    replaced = profiles.upsert_macro(path, {"id": "m", "steps": [{"tap": "z"}]}, replace=True)
    assert replaced is True
    parsed = profiles.load_profile_file(path)
    assert [item.id for item in parsed.profiles] == ["one"]
    assert parsed.macros_raw == [{"id": "m", "steps": [{"tap": "z"}]}]


def test_remove_macro_keeps_profiles(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n\n"
        "[[macro]]\nid = \"m\"\nsteps = [ { tap = \"a\" } ]\n",
        encoding="utf-8",
    )
    profiles.remove_macro(path, "m")
    text = path.read_text(encoding="utf-8")
    assert "[[macro]]" not in text
    assert "[[profile]]" in text


def test_remove_missing_macro_is_rejected(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n", encoding="utf-8")
    with pytest.raises(profiles.ProfileError, match="не найден"):
        profiles.remove_macro(path, "nope")


def test_render_macro_is_parseable(tmp_path):
    """render_macro -> текст -> парсер: шаги не теряют поля."""
    path = tmp_path / "profiles.toml"
    path.write_text("version = 1\n\n[[profile]]\nid = \"one\"\ndpi = 800\n", encoding="utf-8")
    raw = {
        "id": "m",
        "name": "Имя",
        "interval_ms": 25,
        "steps": [{"tap": "ctrl+c"}, {"wait": True, "delay_ms": 10}, {"tap": "enter", "delay_ms": 5}],
    }
    text = profiles.render_macro(raw)
    path.write_text(path.read_text(encoding="utf-8") + "\n" + text + "\n", encoding="utf-8")
    assert profiles.load_profile_file(path).macros_raw[0]["steps"] == raw["steps"]


LIGHTING = """
version = 1

[[profile]]
id = "glow"
dpi = 1600
[profile.lighting]
effect = "breathe"
color = "#00ff88"
brightness = 70
period = 4000
"""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("#ff0000", 0xFF0000), ("red", 0xFF0000), ("#f00", 0xFF0000), (0x00FF00, 0x00FF00), ("0000ff", 0x0000FF)],
)
def test_parse_color(value, expected):
    assert profiles.parse_color(value) == expected


def test_parse_color_rejects_garbage():
    with pytest.raises(profiles.ProfileError):
        profiles.parse_color("радуга")
    with pytest.raises(profiles.ProfileError):
        profiles.parse_color("#12345")
    with pytest.raises(profiles.ProfileError):
        profiles.parse_color(True)


def test_lighting_block_is_parsed():
    parsed = profiles.parse_profiles(LIGHTING)
    assert parsed.profiles[0].lighting == {
        "effect": "breathe",
        "color": 0x00FF88,
        "brightness": 70,
        "period": 4000,
    }


def test_unknown_lighting_effect_is_rejected():
    text = LIGHTING.replace('effect = "breathe"', 'effect = "strobe"')
    with pytest.raises(profiles.ProfileError, match="эффект"):
        profiles.parse_profiles(text)


def test_lighting_brightness_range_is_checked():
    text = LIGHTING.replace("brightness = 70", "brightness = 150")
    with pytest.raises(profiles.ProfileError, match="яркост"):
        profiles.parse_profiles(text)


def test_lighting_round_trips_through_render():
    profile = profiles.Profile(
        id="glow",
        dpi=1600,
        lighting={"effect": "static", "color": 0x123456, "brightness": 40},
    )
    text = profiles.render_profile(profile)
    assert "[profile.lighting]" in text
    parsed = profiles.parse_profiles("version = 1\n\n" + text + "\n")
    assert parsed.profiles[0].lighting == profile.lighting



