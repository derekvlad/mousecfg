#!/usr/bin/env sh
# Установка пользовательской части mousecfg: .desktop, иконка, systemd-юнит.
# Root не требуется — всё кладётся в домашний каталог.
# udev-правила ставятся отдельно (нужен root), см. README или install.sh.
#
# Ключи:
#   --dry-run   только показать, что будет сделано
#   --quiet     не печатать итоговую подсказку (когда зовут из install.sh)
set -eu

dry_run=0
quiet=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --quiet)   quiet=1 ;;
        *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
    esac
done

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
data="${here}/data"
venv="${here}/.venv"

bin_dir="${HOME}/.local/bin"
apps_dir="${HOME}/.local/share/applications"
autostart_dir="${HOME}/.config/autostart"
icons_dir="${HOME}/.local/share/icons/hicolor/scalable/apps"
units_dir="${HOME}/.config/systemd/user"

# Обёртка, уважающая --dry-run.
run_cmd() {
    if [ "$dry_run" -eq 1 ]; then
        printf '    [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

run_cmd mkdir -p "${apps_dir}" "${autostart_dir}" "${icons_dir}" "${units_dir}" "${bin_dir}"

run_cmd install -m 0644 "${data}/io.github.mousecfg.MouseCfg.desktop" "${apps_dir}/"
# Трей кладём и в autostart: он поднимает иконку в панели и хоткеи смены профиля.
run_cmd install -m 0644 "${data}/io.github.mousecfg.MouseCfg-tray.desktop" "${autostart_dir}/"
run_cmd install -m 0644 "${data}/icons/hicolor/scalable/apps/io.github.mousecfg.MouseCfg.svg" "${icons_dir}/"

# Юнит: подставляем абсолютный путь к entry point этого venv. systemd --user
# не наследует PATH, а `%h/mousecfg/...` подходит только для каталога по
# умолчанию — поэтому путь вычисляем здесь, где он точно известен.
if [ -x "${venv}/bin/mousecfg-daemon" ]; then
    if [ "$dry_run" -eq 1 ]; then
        echo "    [dry-run] записать ${units_dir}/mousecfg-daemon.service с ExecStart=${venv}/bin/mousecfg-daemon"
    else
        sed "s#^ExecStart=.*#ExecStart=${venv}/bin/mousecfg-daemon#" \
            "${data}/mousecfg-daemon.service" > "${units_dir}/mousecfg-daemon.service"
        chmod 0644 "${units_dir}/mousecfg-daemon.service"
    fi
else
    echo "! не вижу ${venv}/bin/mousecfg-daemon — создайте окружение (install.sh или README)" >&2
    run_cmd install -m 0644 "${data}/mousecfg-daemon.service" "${units_dir}/"
fi

# Симлинки на entry points venv, если он есть рядом с репозиторием.
if [ -x "${here}/.venv/bin/mousecfg" ]; then
    run_cmd ln -sf "${here}/.venv/bin/mousecfg" "${bin_dir}/mousecfg"
    run_cmd ln -sf "${here}/.venv/bin/mousecfg-daemon" "${bin_dir}/mousecfg-daemon"
    run_cmd ln -sf "${here}/.venv/bin/mousecfg-gui" "${bin_dir}/mousecfg-gui"
    run_cmd ln -sf "${here}/.venv/bin/mousecfg-tray" "${bin_dir}/mousecfg-tray"
    [ "$quiet" -eq 1 ] || echo "симлинки команд: ${bin_dir}"
fi

if [ "$dry_run" -eq 0 ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "${apps_dir}" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
fi

if [ "$quiet" -eq 0 ]; then
    echo "Готово. Дальше:"
    echo "  systemctl --user enable --now mousecfg-daemon"
    echo "  mousecfg-gui    # или запуск из меню приложений"
fi
