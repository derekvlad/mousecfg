# Device matrix

[Русский](../device-matrix.md) · **English**

Only what has actually been verified on hardware is recorded here. Each row is
the result of running `mousecfg` and, where possible, traffic analysis.

## Logitech G102 LIGHTSYNC (046d:c092)

Verified 2026-11-11, firmware `U1 52.00.B0010`, HID++ 4.2, serial `205C35585247`.
Macros re-checked after a reboot — 2026-11-12, systemd 262.

| What | Value |
|---|---|
| HID++ node | `/dev/hidraw2` (interface 1), the second node `/dev/hidraw1` is a regular mouse |
| Mode before the change | `On-Board` (host settings were ignored) |
| Mode after `mousecfg mode host` | `Host`, persists across device reopens |
| DPI | read/write work, `ADJUSTABLE_DPI 0x2201` |
| Report rate | 1/2/4/8 ms supported, read/write work |
| Onboard profiles | present, **1 slot**; `ONBOARD_PROFILES 0x8100` |
| Button divert | **none** (`REPROG_CONTROLS_V4 0x1B04` is absent) → remap only via evdev/uinput |
| LED | `RGB_EFFECTS 0x8071`: 1 zone (logo), effect ids `0x00 disabled, 0x01 static, 0x03 cycle, 0x04 wave, 0x0a breathe, 0x0d, 0x0e decomposition`; `PER_KEY_LIGHTING_V2 0x8081` (3 LEDs) |
| Hidden features | `0x1801, 0x18a1, 0x18b1, 0x1e22, 0x1eb0` — not used |

### Lighting (`RGB_EFFECTS 0x8071`)

The protocol was verified on a live device:

* `getInfo` (fn `0x00`, params `FF FF 00`) → 1 zone, capabilities `0x0004`;
  bit `0x0001` (readable) is clear, i.e. the current effect cannot be read.
* Zone information is the same function `0x00` with `(zone, FF, 00)`; offset
  `[2:5]` = `location` u16 BE + effect count; on the G102 `location = 2` (logo),
  7 effects.
* Effect description — fn `0x00` with `(zone, slot, 00)`.
* **Volatile effect write — fn `0x10`**: `zone + slot + 10 parameter bytes +
  0x01`. This is what the project uses; it does not write to non-volatile
  memory.
* **Before every effect write `setSwControl` is required — fn `0x50`** with
  `01 03 04` (subfn=set, mode=3 "full SW control", flags=`0x04`). Without it the
  firmware keeps drawing the onboard profile colour and `setZoneEffect` visually
  does nothing. The command is volatile (RAM until the mouse is disconnected).
  This is the answer to "lighting does not work"; Solaar uses the same trick
  (`rgb_power.SW_ACTIVE`). The persistent `ONBOARD_PROFILES 0x10 [02]` is not
  required to open control — verified.
* **Persistent NvConfig write — fn `0x30`** (a different function) is denied by
  the policy (`guard`).
* Verified: `static red/green/blue` switch correctly in sequence through the
  regular path (daemon + CLI); `breathe` turns on and holds until reconnect; an
  unavailable effect and an unknown parameter are rejected with an error.

### What "not writing to flash" gives us

* All settings the project applies are volatile HID++ 2.0 commands; after the
  mouse is disconnected they reset, and this is the expected behaviour.
* The only persistent operation is the one-time `On-Board → Host` switch
  (`0x8100/0x10`). It is performed only with `--allow-onboard-switch` and is
  recorded in the journal as `persistent-consented`.
* Denied and never sent: `0x8100/0x30`, `0x8100/0x60..0x80` (onboard profile
  writes), `0x1802` (reset), `0x1806/0x20`, `0x1e00`, `0x00c2/0x20` (firmware),
  `0x8071/0x30` (lighting NvConfig). Checked by the tests in
  `tests/test_guard.py`.

### What has been verified on a live device

| Check | How | Result |
|---|---|---|
| Policy denial | `device.request(0x8100, 0x60)`, `0x1802/0x00`, `0x1e00/0x00` | `DeniedCommand`, nothing is sent to the device |
| Zero persistent frames | `python3 scripts/capture-hid.py -- python3 -m mousecfg set-dpi 2400` | "Persistent operations: 0" |
| DPI volatile | `set-dpi` → read back → `set-dpi` | the value changes within the session (800↔1600↔3200) |
| Rate volatile | `set-rate` 1/2/4/8 ms | the value is accepted and read back |
| Daemon | `mousecfg daemon -v` + `mousecfg status` | finds the mouse, applies `default`, sees the focused application |
| Per-application profile | profile `browser` with `match_apps = ["Vivaldi-flatpak"]` | with Vivaldi focused the DPI became 3200, then returned to 1600 |
| GUI | `mousecfg-gui` with the daemon running | the window builds, the "Apply" button switches the profile (800↔1600) |
| evdev node permissions | `data/70-mousecfg-input.rules` + `udevadm trigger` | `/dev/input/event3` is accessible: `getfacl` shows `user:derek:rw-`, `mousecfg inputs` shows the mouse and its buttons |
| ACL survives `change` | `udevadm test --action={add,change}` on event3 with the rules from `data/` | with `ACTION!="remove"` the `uaccess` builtin is queued on both `add` and `change` (with `ACTION=="add"` — only on `add`; after a reboot the ACL did not appear) |
| Button remapping | `mousecfg run-profile examples/g102-buttons.toml --seconds …` | `EVIOCGRAB` is set up, events are relayed, `--seconds` turns interception off correctly |
| Lighting volatile | `mousecfg lighting --effect static --color '#ff0000' --brightness 50`, then `breathe` | the effect turns on, `static` darkens the colour (0x800000), `breathe` sends `intensity`; holds until reconnect; `fn 0x30` is rejected as `DeniedCommand` |
| Lighting takes SW control | `mousecfg lighting --effect static --color '#00ff00'` on a mouse in host mode | `setSwControl 01 03 04` is sent before `setZoneEffect`; red/green/blue switch correctly in sequence (without the claim the colour "stuck" to the onboard profile) |
| Tray and shortcut | `mousecfg-tray` + the StatusNotifierWatcher panel | the `mousecfg` icon registers; on niri the tray does not touch the portal and does not spawn stuck windows |
| Tray autostart | `sh scripts/install-user.sh` + `systemctl --user daemon-reload` | the unit `app-io.github.mousecfg.MouseCfg-tray@autostart.service` is generated from `~/.config/autostart` (absolute path in `Exec=`) and bound to `xdg-desktop-autostart.target`; the icon appears in the `noctalia` panel |
| Auto-switch to Host | `mousecfg mode host --allow-onboard-switch`, reboot, `systemctl --user restart mousecfg-daemon` | consent is in `~/.local/state/mousecfg/host-consent.json`; when the mouse reconnects in On-Board the daemon switches to `host` itself (the journal says "automatic switch"), `mousecfg status` shows `mode: host` |
| Shortcut via niri | `mousecfg keybinds --install` + `niri msg action load-config-file`, then `Alt+Shift+Right` | the active profile switches to the next one (`default`→`shooter`) |
| Profile is not reset | `mousecfg profile activate game`, then change the focused window | the profile stays `game` (it used to fall back to the first one, `default`); a profile with `match_apps` still wins |
| Macros after a reboot | after the reboot the 70 rule was reinstalled → `udevadm trigger --subsystem-match=input`, the daemon restarted, the side buttons pressed | `side2` → `ctrl+shift+F12`, `side1` → `ctrl+shift+F11`: the virtual node shows `LEFTCTRL`+`LEFTSHIFT`+`F12`/`F11` presses. The daemon reports `remap_active: true`, no errors in the journal |
| Macro recording in the GUI | in the profile dialog choose the `side1` button, "Record macro", press `Ctrl+Shift+T` | the step `ctrl+shift+T` appears in the list, the macro is saved with the id `profile-side1` and substituted into the button bindings |
| Recording is layout-independent | with a Russian layout record `abc`, `Shift+1`, `-`, `F4` | the steps are exactly `A B C`, `shift+1`, `KEY_MINUS`, `F4` (before, almost everything was lost by symbol) |
| End-to-end evdev check | virtual source → `Remapper` → virtual sink | movement passes through; `side1`→`ALT+LEFT`, `middle`→`CTRL+C`, `side2`→macro; the unbound `BTN_RIGHT` passes as is; p95 ≈ 0.4 ms |
| Virtual device | `data/71-mousecfg-virtual.rules` | `/dev/input/eventN` of the `mousecfg virtual input` device gets the session user's ACL |

### What needs manual verification

| Check | How | Expectation |
|---|---|---|
| Volatility across a reconnect | `python3 scripts/replug-test.py` (set values, then physically reconnect the mouse) | after the reconnect the values return to their defaults |
| Independent USB dump | `sudo python3 scripts/capture-hid.py --usbmon capture.pcap -- python3 -m mousecfg set-dpi 1600` | the traffic contains no `0x8100/0x60..0x80`, `0x1802` |
| `host` mode between sessions | disconnect/connect the mouse and check `mousecfg mode` | `host` is expected (mode switching is the only consented write) |

### Model limitations

* The hardware DPI button is handled by the mouse firmware and does not appear
  among the programmable buttons.
* Switching back to `On-Board` mode is not supported by the project: it would
  require writing a profile to mouse memory.

## YJX-CHIP AJAZZ 2.4G (a8a5:2255)

Verified 2026-11-13, serial `D1K_251010b271_A72V1`, `bcdDevice 2.71`.
This mouse speaks a closed vendor protocol: it has no HID++, so the project
manages it at the evdev level only.

| What | Value |
|---|---|
| USB | `a8a5:2255`, 3 interfaces: 0 — mouse, 1 — keyboard/consumer, 2 — vendor HID (usage page `0xFF01`, 64-byte in/out) |
| hidraw | `/dev/hidraw4/5/6`; the project **never opens** them — that would require reverse-engineering the vendor protocol and a separate udev rule |
| evdev | `/dev/input/event20` (`uniq` = serial), 8 buttons, two wheels (`REL_WHEEL` and `REL_HWHEEL`) |
| Protocol | vendor-specific; no HID++ → DPI, report rate, lighting and mode are unavailable |
| Per-application profiles | work; the device key is `serial:D1K_251010b271_A72V1` |
| Button remapping and macros | work through evdev/uinput: `data/70-mousecfg-input.rules` is enough, no hidraw rule needed |
| How it looks in the CLI | `mousecfg list` → `protocol: evdev (no HID++)` and `available: buttons and macros` |
| Daemon | attaches the mouse, logs `evdev: DPI, report rate and lighting are unavailable`, and `mousecfg devices` / `status` list it |
| Protocol commands | `get`, `set-dpi`, `set-rate`, `lighting`, `mode` return a clear error and send nothing |

### Limitations

* DPI, report rate and lighting cannot be changed: the protocol is closed and the
  project never writes to onboard memory. These settings are only available
  through the vendor software or the buttons on the mouse itself.
* Interface 2 (`usage page 0xFF01`) is not used and is never opened.
* The `On-Board → Host` switch, HID++ features and `RGB_EFFECTS` lighting do not
  apply to these mice — in `mousecfg judge` they remain HID++-only.
