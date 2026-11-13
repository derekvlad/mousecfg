# Architecture

[Русский](../architecture.md) · **English**

## The requirement everything follows from

Mouse settings must be applied **volatilely**: profiles are stored on the host
and nothing is written to the mouse onboard memory. Everything else follows.

## Layers

```
GUI (GTK4/libadwaita)   CLI (mousecfg)   tray (GTK3 + AppIndicator3)
        \                      /              /
         JSON-RPC over a UNIX socket
                  |
          daemon (systemd --user)
          ├── discovery    — find mice in sysfs: HID++ and evdev-only
          ├── vendor       — YJX vendor channel: read identification and dumps
          ├── profiles     — TOML schema, storage, matching
          ├── apply        — apply a profile, retry on hotplug
          ├── appwatch     — focused application → profile selection
          ├── lighting     — volatile lighting effects
          └── evdev/uinput — button remapping and macros
                  |
          hidpp: transport → safeplane → guard → device
```

There are two levels of protocol support. HID++ (`hidpp/`) provides DPI, report
rate, mode and lighting. Mice with a closed vendor protocol (e.g. AJAZZ 2.4G)
are managed through evdev only: `discovery.find_mice()` groups the hidraw
interfaces of one device, marks the protocol and finds the `event` node.
For `protocol = "evdev"` the daemon never opens hidraw, and
`engine.apply_profile(None, …)` skips protocol settings with a clear
explanation; profiles, buttons and macros keep working.

`vendor/` is the third layer, read-only. Mice built on the YJX controller have
a separate HID interface (`usage page 0xFF01`, 64-byte reports); the daemon
never opens it and it plays no part in profiles. On an explicit command
(`mousecfg vendor`) the project reads the identification and the raw replies of
the read commands from there. Before every `os.write` the frame is checked by
`vendor/guard.py`: only the listed read commands are allowed, while write
frames (including `0d 00`, which clears memory) are refused — the project never
writes to onboard flash.

`hidpp/guard.py` and `hidpp/safeplane.py` are the core of the requirement. Every
send goes through `SafePlane.send()`: first the command is classified
(`VOLATILE` / `PERSISTENT_CONSENTED` / `PERSISTENT`), then it is written to the
journal, and only then `os.write` to hidraw happens. An unknown pair (feature,
function) is treated as a memory write and is never sent.

The exception is `send_by_index()`: feature indices are assigned by the device
itself, so during enumeration (ROOT knock and FEATURE_SET) the (feature id,
function) pair is not known in advance. Those frames can only read, and there
are two of them.

## Why not libratbag

`ratbagd` works with the "profile written to the device" model: applying
settings ends with a write to onboard memory. Verified on a G102: `ratbagctl`
shows a macro stored in the device itself. That is why `libratbag` is used only
as a read-only reference while researching the protocol.

## Protocol

HID++ 2.0 is implemented directly: short (0x10) and long (0x11) frames,
Software ID `0x09` (free from Solaar `0x0B`, OpenRGB `0x07`, G HUB `0x0D`),
device index `0xFF` for wired USB. Replies from other clients and device
notifications are recognised and skipped.

Opcodes were checked against the public behaviour of Solaar (`1.1.20` is
installed on the system) — as a protocol reference, without copying code (see
`docs/en/licensing.md`).

## Reliability

* The stream of regular mouse input reports (report id `0x01`, hundreds of
  frames per second at 1000 Hz) is filtered in the transport: otherwise it
  "eats" the reply timeout window.
* `BUSY` from the device is a temporary code; the command is retried after a
  pause.
* Changing the report rate causes a USB reconnect: the daemon must tolerate the
  device disappearing and reappearing.
* An orphaned UNIX socket after a daemon crash is detected: before bind the
  server checks whether anyone answers on the socket file.
* If the mouse event device is not yet available on connect (the ACL appears
  late), the daemon periodically retries raising button remapping instead of
  waiting for the device to reconnect.

## Device mode and consent

Host mode is a prerequisite for host settings to apply at all. It is the only
persistent operation in the project (`0x8100/0x10`, `persistent-consented`). The
firmware does not always remember the mode between sessions: after a reboot a
G102 returns to On-Board.

The user gives consent to the switch explicitly — `mousecfg mode
host --allow-onboard-switch`. The device key is then written to
`~/.local/state/mousecfg/host-consent.json`, and on subsequent connections the
daemon repeats the switch itself if it sees `onboard`. This is not a new write:
the user has already agreed to this operation, and the daemon merely brings the
device into the state without which profiles do not work. Without a record the
daemon does not touch the mode (checked by `tests/test_daemon_host_mode.py`).
The auto-switch can be disabled with `--no-auto-host`.

## Daemon

The daemon is the only component that holds the state "which mouse uses which
profile". Clients (CLI, GUI) only ask:

| Method | What it does |
|---|---|
| `ping`, `status` | daemon state, focused application, devices |
| `list-devices`, `list-input-devices`, `list-macros` | inventory |
| `apply-profile` | apply a profile by id |
| `set-dpi`, `set-rate`, `set-lighting` | change one setting volatilely |
| `profile-activate`, `profile-next`, `profile-prev` | switch profile (tray and shortcuts) |
| `macro-upsert`, `macro-remove` | save/delete a macro (writes from the GUI) |
| `set-remap` | enable/disable button remapping (tray) |
| `reload` | re-read TOML files and apply again |
| `subscribe` | event stream (`device-added`, `profile-applied`) |

The transport is JSON Lines over the UNIX socket
`$XDG_RUNTIME_DIR/mousecfg/daemon.sock` with `0600` permissions. D-Bus is not
used: a minimal set of dependencies is wanted, and a socket in the user runtime
directory is already restricted by access permissions.

A profile is chosen by the "most specific match": serial number beats
application, application beats the general profile; the order in the file breaks
ties.

A manual choice (`profile-activate` from the CLI/tray/shortcut) is pinned in
`state.pinned_profile`. Recalculation on every focused-window change keeps the
pinned profile, so the choice does not fall back to the first profile in the
file; a profile with `match_apps` still wins over the pinned one, and when you
leave its application the daemon returns the pinned profile. Pinning survives a
daemon restart and is cleared if the profile is removed from the file.

## Buttons and macros

The G102 has no divert feature (`REPROG_CONTROLS_V4`), so the only working path
is evdev. The daemon finds the mouse event device, performs `EVIOCGRAB` and
relays movement through uinput, substituting only the buttons that are part of
the profile. A macro is TOML data plus an interpreter on the host: pressing the
button again interrupts the current macro.

Remapping applies only to buttons from the profile, so `EVIOCGRAB` does not
affect normal mouse operation.

Two consequences of `EVIOCGRAB` that are important not to forget:

* After grabbing, **all** events must be relayed, including unbound buttons
  (`EV_KEY`): otherwise the left and right buttons are lost and the mouse
  "freezes".
* The virtual device `mousecfg virtual input` must be accessible to the session
  user. A uinput node has no `ID_SEAT`, so the generic `uaccess` mechanism does
  not pick it up — a separate rule `data/71-mousecfg-virtual.rules` is needed,
  with a number **before** `73-seat-late.rules`.
* Rules tagged `uaccess` must match on all actions except `remove`
  (`ACTION!="remove"`), not only on `add`. With systemd v258 the ACL is applied
  by `systemd-udevd`, and logind only sends `change` events; a rule limited to
  `add` fires when the device appears but stops matching on any subsequent
  `change` — and the ACL on `/dev/input/event*` disappears (buttons and macros
  "fall off" after a reboot). This is checked by `tests/test_udev_rules.py`.
* Events are read through `select` with a timeout, not through a blocking
  `read_loop()`: otherwise interception cannot be turned off until the next
  event arrives (on a motionless mouse — never).

## Lighting

A lighting effect is as volatile a setting as DPI and report rate. `guard`
allows only the volatile write `RGB_EFFECTS 0x8071/0x10` and the volatile
`setSwControl` (`0x8071/0x50`); the persistent NvConfig write (`0x8071/0x30`) is
denied, as is everything that touches non-volatile memory. Lighting is stored in
the profile (`[profile.lighting]`) and applied by the same
`engine.apply_profile()`.

Before writing an effect, `setSwControl` must be sent with the payload
`01 03 04` (`subfn=set`, `mode=3` — full SW control, `flags=0x04`): otherwise
the LIGHTSYNC firmware keeps drawing the colour of the **onboard profile** and
ignores `setZoneEffect` — from the outside this looks like "lighting does not
work". Solaar does the same (`rgb_power.SW_ACTIVE`) before its writes. The
command is volatile: it lives in RAM until the mouse is disconnected and writes
nothing to mouse memory. Opening control on this series does not require the
persistent command `ONBOARD_PROFILES 0x10 [02]` — verified on hardware.

A colour in a profile is an integer `0xRRGGBB`, but you can write `#rrggbb`,
`rrggbb` or a name (`red`, `blue`). Brightness (0..100) is converted into the
`intensity` byte if the effect knows it; for `static` brightness darkens the
colour itself, because it has no separate intensity parameter.

## Tray and global shortcuts

The tray is a separate GTK3 + AppIndicator3 process (`mousecfg-tray`), because
AppIndicator3 is incompatible with GTK4/libadwaita in one process. It does not
open the device: all actions go through the same JSON-RPC.

The tray autostarts through `~/.config/autostart` and
`systemd-xdg-autostart-generator`. A subtlety: the generator silently drops a
`.desktop` file if the executable from `Exec=` is not found in the systemd
manager's `PATH`, and `~/.local/bin` is not part of that `PATH`. With
`Exec=mousecfg-tray` the tray would therefore not start at all (the unit is not
generated). The installer substitutes the absolute path to `mousecfg-tray` into
`Exec=` — otherwise there is no icon in the panel.

Profile switching via a global shortcut is implemented through the
`org.freedesktop.portal.GlobalShortcuts` portal. The portal has a hard
requirement: the app id is derived from the systemd unit name (cgroup), of the
form `app-<app-id>-<suffix>.scope`. That is why on startup the tray enters the
scope `app-io.github.mousecfg.MouseCfg-tray.scope` itself via `systemd-run
--user --scope`. Portal replies are asynchronous (`Response`), and presses
arrive as the `Activated` signal.

The portal is not available everywhere. On niri and other wlroots compositors
the only backend that announces `GlobalShortcuts` is GNOME; under niri it does
not open the service Wayland channel and instead of a dialog shows
never-closing "Add Keyboard Shortcuts" windows. That is why
`hotkeys.portal_supported()` recognises such a compositor and the tray does not
touch the portal. The compositor then binds the shortcut itself: `mousecfg
keybinds` prints a ready-made `binds` block for niri (and can append it to the
config with a backup), and the action calls `mousecfg profile next`/`next
--prev` — the same RPCs as the portal.
