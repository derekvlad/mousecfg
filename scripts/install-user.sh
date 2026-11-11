#!/usr/bin/env sh
# Установка пользовательской части mousecfg: .desktop, иконка, systemd-юнит.
# Root не требуется — всё кладётся в домашний каталог.
# udev-правила ставятся отдельно (нужен root), см. README.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
data="${here}/data"

bin_dir="${HOME}/.local/bin"
apps_dir="${HOME}/.local/share/applications"
icons_dir="${HOME}/.local/share/icons/hicolor/scalable/apps"
units_dir="${HOME}/.config/systemd/user"

mkdir -p "${apps_dir}" "${icons_dir}" "${units_dir}" "${bin_dir}"

install -m 0644 "${data}/io.github.mousecfg.MouseCfg.desktop" "${apps_dir}/"
install -m 0644 "${data}/icons/hicolor/scalable/apps/io.github.mousecfg.MouseCfg.svg" "${icons_dir}/"
install -m 0644 "${data}/mousecfg-daemon.service" "${units_dir}/"

# Симлинки на entry points venv, если он есть рядом с репозиторием.
if [ -x "${here}/.venv/bin/mousecfg" ]; then
    ln -sf "${here}/.venv/bin/mousecfg" "${bin_dir}/mousecfg"
    ln -sf "${here}/.venv/bin/mousecfg-daemon" "${bin_dir}/mousecfg-daemon"
    ln -sf "${here}/.venv/bin/mousecfg-gui" "${bin_dir}/mousecfg-gui"
    echo "симлинки команд: ${bin_dir}"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${apps_dir}" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

echo "Готово. Дальше:"
echo "  systemctl --user enable --now mousecfg-daemon"
echo "  mousecfg-gui    # или запуск из меню приложений"
