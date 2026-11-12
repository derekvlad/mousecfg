#!/usr/bin/env bash
# mousecfg: установка «одной командой» для тех, кто не хочет разбираться.
#
# Скрипт делает всё, что можно сделать автоматически:
#   1. ставит системные зависимости (GTK4/libadwaita, python-gobject, evdev);
#   2. создаёт виртуальное окружение .venv и ставит пакет из этого каталога;
#   3. ставит udev-правила (нужен root — спросит пароль);
#   4. ставит значок в меню, иконку трея, systemd-юнит и симлинки команд;
#   5. включает и запускает демон.
#
# Ничего во встроенную память мыши не пишется: проект передаёт настройки
# волатильными HID++-командами.
#
# Использование:
#   ./install.sh                 # обычная установка
#   ./install.sh --no-deps       # не трогать системные пакеты
#   ./install.sh --no-udev       # пропустить udev (если правила уже стоят)
#   ./install.sh --no-start      # установить, но не включать демон
#   ./install.sh --dry-run       # только показать, что будет сделано
#   ./install.sh --uninstall     # удалить пользовательскую часть
#   ./install.sh --help
set -euo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
venv="${repo}/.venv"
bin_dir="${HOME}/.local/bin"
apps_dir="${HOME}/.local/share/applications"
autostart_dir="${HOME}/.config/autostart"
icons_dir="${HOME}/.local/share/icons/hicolor/scalable/apps"
units_dir="${HOME}/.config/systemd/user"
rules_dir="/etc/udev/rules.d"

with_deps=1
with_udev=1
start_daemon=1
dry_run=0
uninstall=0

# --- вывод ------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    c_reset=$'\033[0m'; c_bold=$'\033[1m'; c_dim=$'\033[2m'
    c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_step=$'\033[36m'
else
    c_reset=""; c_bold=""; c_dim=""; c_ok=""; c_warn=""; c_err=""; c_step=""
fi

step() { printf '%s==>%s %s%s%s\n' "$c_step" "$c_reset" "$c_bold" "$*" "$c_reset"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$c_ok" "$c_reset" "$*"; }
warn() { printf '    %s!%s %s\n' "$c_warn" "$c_reset" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$c_err" "$c_reset" "$*" >&2; exit 1; }

# Выполнить команду, уважая --dry-run.
run() {
    if [ "$dry_run" -eq 1 ]; then
        printf '    %s[dry-run]%s %s\n' "$c_dim" "$c_reset" "$*"
        return 0
    fi
    "$@"
}

usage() {
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# --- разбор аргументов ------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --no-deps)    with_deps=0 ;;
        --no-udev)    with_udev=0 ;;
        --no-start)   start_daemon=0 ;;
        --dry-run)    dry_run=1 ;;
        --uninstall)  uninstall=1 ;;
        -h|--help)    usage ;;
        *) die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
    shift
done

# --- sudo -------------------------------------------------------------------

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        run "$@"
    else
        command -v sudo >/dev/null 2>&1 || die "нужен sudo (или запустите от root): $*"
        run sudo "$@"
    fi
}

# --- менеджер пакетов -------------------------------------------------------

detect_pm() {
    for pm in pacman apt-get dnf zypper; do
        command -v "$pm" >/dev/null 2>&1 && { printf '%s' "$pm"; return; }
    done
}

deps_for() {
    case "$1" in
        pacman)  printf '%s' "python-gobject gtk4 libadwaita python-evdev" ;;
        apt-get) printf '%s' "python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-evdev" ;;
        dnf)     printf '%s' "python3-gobject gtk4 libadwaita python3-evdev" ;;
        zypper)  printf '%s' "python3-gobject gtk4 libadwaita python3-evdev" ;;
    esac
}

pkg_installed() {
    case "$pm" in
        pacman)  pacman -Qq "$1" >/dev/null 2>&1 ;;
        apt-get) dpkg -s "$1" >/dev/null 2>&1 ;;
        dnf)     rpm -q "$1" >/dev/null 2>&1 ;;
        zypper)  rpm -q "$1" >/dev/null 2>&1 ;;
        *)       return 1 ;;
    esac
}

install_pkg() {
    case "$pm" in
        pacman)  as_root pacman -S --needed --noconfirm "$@" ;;
        apt-get) as_root apt-get install -y "$@" ;;
        dnf)     as_root dnf install -y "$@" ;;
        zypper)  as_root zypper --non-interactive install "$@" ;;
    esac
}

# --- удаление ---------------------------------------------------------------

uninstall_files() {
    step "Удаление пользовательской части mousecfg"
    run rm -f "${apps_dir}/io.github.mousecfg.MouseCfg.desktop"
    run rm -f "${autostart_dir}/io.github.mousecfg.MouseCfg-tray.desktop"
    run rm -f "${icons_dir}/io.github.mousecfg.MouseCfg.svg"
    run rm -f "${units_dir}/mousecfg-daemon.service"
    for cmd in mousecfg mousecfg-daemon mousecfg-gui mousecfg-tray; do
        if [ -L "${bin_dir}/${cmd}" ]; then
            run rm -f "${bin_dir}/${cmd}"
        fi
    done
    if command -v systemctl >/dev/null 2>&1; then
        run systemctl --user disable --now mousecfg-daemon 2>/dev/null || true
        run systemctl --user daemon-reload || true
    fi
    warn "udev-правила в ${rules_dir} не трогались — удалите вручную, если нужно:"
    info "sudo rm -f ${rules_dir}/60-mousecfg.rules ${rules_dir}/70-mousecfg-input.rules ${rules_dir}/71-mousecfg-virtual.rules"
    ok "Готово. Каталог ${repo} и .venv оставлены как есть."
}

if [ "$uninstall" -eq 1 ]; then
    uninstall_files
    exit 0
fi

# --- 0. проверки окружения --------------------------------------------------

step "Проверка окружения"
command -v python3 >/dev/null 2>&1 || die "не найден python3"
info "python3: $(python3 --version 2>&1)"
info "каталог проекта: ${repo}"
[ -f "${repo}/pyproject.toml" ] || die "не вижу pyproject.toml — запустите install.sh из корня репозитория"

pm=$(detect_pm || true)
if [ -n "${pm}" ]; then
    info "менеджер пакетов: ${pm}"
else
    warn "неизвестный менеджер пакетов — системные зависимости придётся поставить вручную"
fi

if ! python3 -c 'import venv' >/dev/null 2>&1; then
    die "модуль venv недоступен. Debian/Ubuntu: sudo apt install python3-venv"
fi
ok "окружение готово"

# --- 1. системные зависимости ----------------------------------------------

if [ "$with_deps" -eq 1 ] && [ -n "${pm}" ]; then
    step "Системные зависимости (GTK4/libadwaita, python-gobject, evdev)"
    deps=$(deps_for "$pm")
    missing=""
    for dep in $deps; do
        pkg_installed "$dep" || missing="${missing} ${dep}"
    done
    missing=${missing# }
    if [ -z "${missing}" ]; then
        ok "всё уже установлено"
    else
        info "не хватает: ${missing}"
        # shellcheck disable=SC2086
        install_pkg ${missing}
        ok "зависимости установлены"
    fi
elif [ "$with_deps" -eq 1 ]; then
    warn "пропускаю установку зависимостей (менеджер пакетов не распознан)"
else
    info "системные зависимости пропущены (--no-deps)"
fi

# --- 2. виртуальное окружение ----------------------------------------------

step "Виртуальное окружение Python"
if [ -x "${venv}/bin/python" ]; then
    ok "уже есть: ${venv}"
else
    # --system-site-packages нужен, чтобы из venv были видны системные
    # PyGObject/GTK: они не собираются в обычном venv.
    run python3 -m venv --system-site-packages "${venv}"
    ok "создано: ${venv}"
fi
run "${venv}/bin/python" -m pip install --quiet --upgrade pip
info "устанавливаю пакет mousecfg (без dev-зависимостей)"
run "${venv}/bin/pip" install --quiet -e "${repo}[input]"
ok "пакет установлен"

# --- 3. udev-правила (root) --------------------------------------------------

if [ "$with_udev" -eq 1 ]; then
    step "Правила udev (доступ к hidraw и /dev/input)"
    as_root install -m 0644 "${repo}/data/60-mousecfg.rules" "${rules_dir}/60-mousecfg.rules"
    as_root install -m 0644 "${repo}/data/70-mousecfg-input.rules" "${rules_dir}/70-mousecfg-input.rules"
    as_root install -m 0644 "${repo}/data/71-mousecfg-virtual.rules" "${rules_dir}/71-mousecfg-virtual.rules"
    as_root udevadm control --reload
    as_root udevadm trigger --subsystem-match=hidraw
    as_root udevadm trigger --subsystem-match=input
    ok "правила установлены, udev перезапущен"
else
    info "udev пропущен (--no-udev)"
fi

# --- 4. пользовательская часть ---------------------------------------------

step "Значок в меню, иконка трея, systemd-юнит"
if [ "$dry_run" -eq 1 ]; then
    sh "${repo}/scripts/install-user.sh" --dry-run --quiet
else
    sh "${repo}/scripts/install-user.sh" --quiet
fi
ok "пользовательские файлы установлены"

# --- 5. демон ----------------------------------------------------------------

if [ "$start_daemon" -eq 1 ]; then
    step "Запуск демона"
    if command -v systemctl >/dev/null 2>&1; then
        run systemctl --user daemon-reload || true
        run systemctl --user enable --now mousecfg-daemon
        ok "демон включён и запущен"
    else
        warn "systemctl не найден — запустите демон вручную: ${venv}/bin/mousecfg-daemon -v"
    fi
else
    info "демон не включён (--no-start)"
fi

# --- итог --------------------------------------------------------------------

cat <<EOF

${c_ok}${c_bold}Готово!${c_reset}

  Запустить приложение:   mousecfg-gui
                          (или значок «mousecfg» в меню приложений)
  Состояние демона:       systemctl --user status mousecfg-daemon
  Журнал демона:          journalctl --user -u mousecfg-daemon -f
  Найти мышь:             mousecfg list

Если команда mousecfg не находится — откройте новый терминал или добавьте
${bin_dir} в PATH.

Первый запуск: мышь может быть в режиме On-Board. Чтобы настройки с хоста
применялись, один раз переведите её в Host mode:

  mousecfg mode host --allow-onboard-switch

Согласие на этот однократный переход запоминается по устройству. Если
прошивка после перезагрузки снова вернёт мышь в On-Board, демон повторит
переход сам.
EOF
