# mousecfg

Управление игровыми мышами в Linux, где **профили живут на хосте**, а не в памяти мыши.

Главное требование: при применении профиля в энергонезависимую память мыши
(onboard memory / «флеш») не пишется ничего. Все настройки передаются
волатильными HID++ командами и теряются при отключении мыши — демон
применяет их заново при подключении.

## Возможности

- HID++ 2.0: DPI, report rate, режим устройства, имя/прошивка (Logitech);
- демон `systemd --user`: сам находит мышь, применяет профиль при подключении
  и переключает его по активному приложению;
- профили в TOML: DPI, частота опроса, кнопки, макросы, привязка к серийнику
  или приложению;
- ремап кнопок и макросы через evdev/uinput — на стороне хоста, мышь о них
  не знает;
- GTK4/libadwaita-приложение как тонкий клиент демона;
- журнал каждой отправленной команды и белый список фич;
- CLI.

Чего проект не делает **никогда**: записи onboard-профилей, сброса устройства,
включения скрытых фич и прошивки. Запрещено в `hidpp/guard.py` и проверяется
тестами.

Единственная персистентная операция — однократный переход
`On-Board → Host` (`0x8100/0x10`). Он выполняется только с
`--allow-onboard-switch`, потому что без Host mode настройки с хоста
вообще не применяются.

## Установка

Проект использует PyGObject/GTK, которые в большинстве дистрибутивов
поставляются системно и не собираются в обычном venv. Поэтому окружение
создаётся с `--system-site-packages` — тогда `gi`/GTK видны из venv.

```sh
cd mousecfg
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev,input]"
```

После этого команды появляются в `.venv/bin/`:

```sh
.venv/bin/mousecfg --version
.venv/bin/mousecfg list
.venv/bin/mousecfg-gui          # GUI
.venv/bin/mousecfg-daemon -v    # демон на переднем плане
```

Если запускаете без установки, работает и `PYTHONPATH=src python3 -m mousecfg …`.

Установка пользовательской части (меню приложений, иконка, systemd-юнит,
симлинки команд в `~/.local/bin`):

```sh
sh scripts/install-user.sh
```

Скрипт ничего не требует от root: всё кладётся в домашний каталог. После него
mousecfg появляется в меню приложений и запускается как `mousecfg-gui`.

Зависимости на время работы:

```sh
# Debian/Ubuntu
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1   # GUI
sudo apt install python3-evdev                            # кнопки и макросы
# Arch
sudo pacman -S python-gobject gtk4 libadwaita python-evdev
```

Доступ к устройству и ввод:

```sh
sudo install -m 0644 data/60-mousecfg.rules /etc/udev/rules.d/
sudo install -m 0644 data/70-mousecfg-input.rules /etc/udev/rules.d/
sudo install -m 0644 data/71-mousecfg-virtual.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw
sudo udevadm trigger --subsystem-match=input
```

Первое правило даёт ACL на HID++ узел мыши (hidraw), второе — на
`/dev/input/event*` для ремапа кнопок, третье — на виртуальное устройство
`mousecfg virtual input`, через которое ретранслируются события. Root демону
не нужен: он работает
в пользовательской сессии.

Автозапуск демона (это уже сделал `scripts/install-user.sh`; ниже — вручную):

```sh
mkdir -p ~/.config/systemd/user
cp data/mousecfg-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mousecfg-daemon
```

Юнит берёт путь к проекту из `%h/mousecfg/.venv/bin/mousecfg-daemon`
(`%h` — домашний каталог). Если репозиторий лежит в другом месте или вы
ставили пакет иначе, поправьте `ExecStart`: `systemd --user` не наследует
`PATH` из профиля shell, поэтому путь должен быть абсолютным.

Проверка и управление:

```sh
systemctl --user status mousecfg-daemon
journalctl --user -u mousecfg-daemon -f
systemctl --user restart mousecfg-daemon
```

Юнит привязан к `graphical-session.target`: демон стартует вместе с
графической сессией и перезапускается при падении. Демону нужны только
ACL на hidraw/uinput — root не требуется.

## Использование

```sh
mousecfg list                 # найти устройства
mousecfg judge                # политика: что разрешено, что запрещено
mousecfg get                  # имя, прошивка, DPI, report rate, режим
mousecfg set-dpi 1600         # волатильно
mousecfg set-rate 1000Hz      # волатильно
mousecfg lighting             # зоны и эффекты подсветки
mousecfg lighting --effect static --color '#00a0ff' --brightness 60
mousecfg mode host --allow-onboard-switch   # однократный переход в Host mode
mousecfg profiles-check       # проверить TOML-профили

mousecfg status               # состояние демона
mousecfg devices              # устройства под управлением демона
mousecfg inputs               # evdev-узлы мышей (для ремапа кнопок)
mousecfg profile next         # переключить на следующий профиль (для хоткея)
mousecfg daemon -v            # запустить демон на переднем плане
mousecfg run-profile examples/g102-buttons.toml   # ремап без демона
```

### Профили и макросы без правки TOML вручную

`profile` и `macro` меняют тот же файл профилей. Если демон запущен, правка
идёт через него: файл перечитывается и профиль применяется сразу. Файл нельзя
испортить — перед записью результат разбирается парсером, а ссылка на
несуществующий макрос отклоняется.

```sh
# создать профиль (можно повторять --button и --match-app)
mousecfg profile add --id fps --name "Шутеры" --dpi 800 --rate 1000Hz \
    --button side1=browser-back --button side2=macro:burst \
    --match-app cs2 --match-app "steam_app_*"

mousecfg profile set --id fps --dpi 1600        # изменить существующий
mousecfg profile show fps                       # показать (--json для машинного вывода)
mousecfg profile show                           # все профили файла
mousecfg profile remove fps

# макросы: шаги — 'ctrl+c', 'wait:120' (пауза), 'tap:enter:50' (клавиша + пауза)
mousecfg macro add --id burst --name "Серия" --interval-ms 40 \
    --step "ctrl+c" --step "wait:120" --step "tap:enter:50"
mousecfg macro add --id burst --step "tap:x" --replace   # заменить
mousecfg macro list                             # список макросов файла
```

Действие кнопки задаётся как `BUTTON=ACTION`, где ACTION — имя действия
(`browser-back`, `copy`, `ignore`), `macro:имя` или `key:ctrl+c`. Файл можно
указать явно через `--profile-file ПУТЬ` (не нужен запущенный демон).

### Подсветка

Подсветка меняется волатильно: эффект живёт в оперативной памяти мыши и
сбрасывается при отключении, как и DPI с частотой опроса. Команда только
читает и меняет рантайм-состояние — во встроенную память мыши не пишется
ничего (персистентная запись NvConfig запрещена политикой).

```sh
mousecfg lighting                          # какие зоны и эффекты знает мышь
mousecfg lighting --effect breathe --color '#00ff88' --brightness 70 --period 4000
mousecfg lighting --effect disabled        # выключить подсветку
```

Подсветку можно включить в профиль — тогда она применяется вместе с ним:

```sh
mousecfg profile add --id glow --dpi 1600 \
    --lighting-effect static --lighting-color blue --lighting-brightness 40
```

```toml
[[profile]]
id = "glow"
dpi = 1600
[profile.lighting]
effect = "breathe"
color = "#00ff88"
brightness = 70
period = 4000
```

### Трей и хоткей смены профиля

`mousecfg-tray` — небольшой процесс GTK3 + AppIndicator3: он показывает
текущий профиль и даёт переключить профиль, включить или выключить ремап
кнопок и открыть главное окно. Трей запускается отдельно от GUI, потому что
AppIndicator3 построен на GTK3, а окно — на GTK4.

Тот же процесс держит глобальные хоткеи через портал
`org.freedesktop.portal.GlobalShortcuts` (следующий и предыдущий профиль).
Портал выдаёт app id только по имени systemd-юнита, поэтому трей сам заходит
в scope `app-io.github.mousecfg.MouseCfg-tray.scope` через `systemd-run`. При
первом запуске портал показывает диалог, где надо назначить комбинации; дальше
он их помнит. Отключить хоткеи можно ключом `--no-shortcuts`.

```sh
mousecfg-tray                 # иконка в трее + хоткеи смены профиля
mousecfg-tray --no-shortcuts  # только трей, без портала
```

### Пример файла профилей

Пример профиля (`~/.config/mousecfg/profiles/<модель>.toml`):

```toml
version = 1

[[device]]
id = "logitech-g102-lightsync"
serials = ["205C35585247"]

[[profile]]
id = "default"
name = "Обычная работа"
dpi = 1600
report_rate = "1000Hz"

[[profile]]
id = "shooter"
name = "Шутеры"
dpi = 800
match_apps = ["cs2", "steam_app_*"]

[[profile]]
id = "desktop"
dpi = 1600
[profile.buttons]
"side1" = "browser-back"
"side2" = { macro = "burst" }

[[macro]]
id = "burst"
interval_ms = 30
steps = [
    { tap = "ctrl+c", delay_ms = 50 },
    { wait = true, delay_ms = 120 },
]
```

`storage = "onboard"` в профиле отвергается с ошибкой: проект хранит профили
только на хосте.

## Интерфейс GUI

```sh
source .venv/bin/activate    # или PYTHONPATH=src
mousecfg-gui                 # либо python3 -m mousecfg.gui.app
```

Приложение показывает режим устройства, текущие DPI и частоту опроса,
список профилей с кнопкой «Применить», назначения кнопок и макросы. Все
изменения уходят демону через UNIX-сокет: GUI не общается с мышью напрямую.

Профили можно создавать, редактировать и удалять прямо в окне: кнопка «+» в
заголовке группы «Профили» и кнопки «карандаш»/«корзина» в строке профиля.
Диалог профиля задаёт id, имя, DPI, частоту опроса, привязку к приложениям
(`match_apps`), назначения кнопок в виде `side1=copy, side2=macro:burst` и
подсветку профиля (эффект, цвет, яркость). Как и в CLI, запись без проверки
не проходит: ссылка на несуществующий макрос отклоняется, файл профилей
остаётся целым.

Группа «Подсветка» появляется у мышей, которые её поддерживают, и меняет
эффект волатильно. Кнопка «Записать» в заголовке группы «Макросы» открывает
запись: нажмите её, нажмите нужные клавиши (они покажутся списком) и
сохраните макрос — он тут же появится в профилях. Запись ведётся в самом
окне, поэтому не требует ни прав на `/dev/input`, ни группы `input`.

## Проверки «во флеш ничего не пишется»

```sh
# по журналу проекта: после команды не должно остаться персистентных кадров
python3 scripts/capture-hid.py -- python3 -m mousecfg set-dpi 1600

# волатильность: значения должны сброситься после физического переподключения
python3 scripts/replug-test.py

# самопроверка ремапа кнопок и макросов (нужны права на /dev/uinput и /dev/input)
PYTHONPATH=src python3 scripts/evdev-selftest.py
```

`evdev-selftest.py` поднимает виртуальную мышь, прогоняет через ремап
движение, привязанные и непривязанные кнопки и макрос — и проверяет, что
всё дошло до виртуального выхода. Реальная мышь для этого не нужна.

## Документация

- `docs/device-matrix.md` — что проверено на реальном железе;
- `docs/architecture.md` — слои и почему так;
- `docs/licensing.md` — лицензии и происхождение кода.

## Лицензия

GPL-3.0-or-later. Протокол HID++ 2.0 разбирался по публичной документации
и по поведению имеющегося на системе Solaar (в режиме «чтение справки»);
чужой код в проект не копируется, см. `docs/licensing.md`.
