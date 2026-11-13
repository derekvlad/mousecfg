# Упаковка для Arch (AUR)

> **Статус.** Регистрация в AUR сейчас закрыта, поэтому пакет ещё не
> опубликован. Здесь лежат готовые `PKGBUILD` — их можно собрать и поставить
> локально (`makepkg -si`) или опубликовать позже, когда регистрация откроется.

Два варианта пакета:

| Каталог | Пакет | Источник | Когда использовать |
| --- | --- | --- | --- |
| `mousecfg-git/` | `mousecfg-git` | последний коммит `main` | Начать проще всего: тег не нужен |
| `mousecfg/` | `mousecfg` | tar-архив тега `v$pkgver` | Стабильный релиз |

## Что кладёт пакет

Оба PKGBUILD раскладывают одно и то же:

* код и точки входа (`mousecfg`, `mousecfg-daemon`, `mousecfg-gui`,
  `mousecfg-tray`) → `/usr/lib/python<ver>/site-packages` и `/usr/bin`;
* правила udev → `/usr/lib/udev/rules.d/`;
* пользовательский сервис → `/usr/lib/systemd/user/mousecfg-daemon.service`
  (в `ExecStart=` подставляется `/usr/bin/mousecfg-daemon`);
* значок в меню → `/usr/share/applications/`;
* иконку → `/usr/share/icons/hicolor/scalable/apps/`;
* автозапуск трея → `/etc/xdg/autostart/`
  (в `Exec=` подставляется абсолютный `/usr/bin/mousecfg-tray` — иначе
  systemd-генератор XDG-autostart молча пропускает `.desktop`).

После установки нужен один шаг с root — перезагрузить правила udev (об этом
напомнит `.install`), иначе у демона не будет ACL на мышь.

## Проверка сборки локально

```sh
cd packaging/aur/mousecfg-git      # или packaging/aur/mousecfg
makepkg -f                         # собрать
makepkg -si                        # собрать и поставить
namcap mousecfg-git-*.pkg.tar.zst  # проверить зависимости (пакет namcap)
```

Для релизного пакета `sha256sums` в PKGBUILD — заглушка `'SKIP'`. Настоящую
сумму считает `updpkgsums` (из `pacman-contrib`) уже после того, как тег
запушен:

```sh
cd packaging/aur/mousecfg
updpkgsums
```

## Публикация в AUR

1. Заведите аккаунт на <https://aur.archlinux.org> и добавьте туда свой
   SSH-ключ (My Account → SSH Public Key).
2. Для релизного пакета — сначала тег. Перед тегом версия в `pyproject.toml`
   должна совпадать с `pkgver` (без `dev0`), иначе `mousecfg --version` будет
   врать:

   ```sh
   # pyproject.toml: version = "0.3.0"
   git commit -am "release 0.3.0"
   git tag -a v0.3.0 -m "v0.3.0"
   git push origin main v0.3.0
   cd packaging/aur/mousecfg && updpkgsums
   ```

3. Сгенерируйте `.SRCINFO` (AUR читает именно его):

   ```sh
   cd packaging/aur/mousecfg-git      # имя каталога = имя пакета
   makepkg --printsrcinfo > .SRCINFO
   ```

4. Клонируйте пустой репозиторий AUR и запушьте два файла:

   ```sh
   git clone ssh://aur@aur.archlinux.org/mousecfg-git.git
   cd mousecfg-git
   cp /path/to/mousecfg/packaging/aur/mousecfg-git/PKGBUILD .
   cp /path/to/mousecfg/packaging/aur/mousecfg-git/mousecfg-git.install .
   cp /path/to/mousecfg/packaging/aur/mousecfg-git/.SRCINFO .
   git add PKGBUILD .SRCINFO mousecfg-git.install
   git commit -m "Initial import"
   git push
   ```

   В AUR-репозиторий кладут **только** `PKGBUILD`, `.SRCINFO` и `.install`
   (`.install` — если он есть; имя файла обязано совпадать с `install=`).
   Каталоги `packaging/aur/...` из основного репозитория — это шаблон для
   копирования, AUR-репозиторий отдельный.

После пуша пакет появится на <https://aur.archlinux.org/packages/mousecfg-git>
и станет ставиться через `yay -S mousecfg-git`.

## Почему не Flatpak

Flatpak этому проекту плохо подходит, и это не вопрос упаковки:

* `/dev/hidraw*` в песочнице целиком не выдаётся — только `--device=all`;
* правила udev из Flatpak поставить нельзя, а без них нет ACL на мышь;
* пользовательский сервис systemd и заход трея в scope
  (`systemd-run --user --scope`) из песочницы не работают.

То есть пришлось бы получить полупесочное приложение, которому всё равно нужны
действия руками на хосте. AUR для такого инструмента — правильный путь.
