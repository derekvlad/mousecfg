# Changelog

Format: significant changes per version. Protocol details live in
`docs/en/device-matrix.md`, writing to memory in `docs/en/onboard.md`.

## Unreleased

### Added

- Link for optional project support (Boosty) — in `README.md`,
  `README.en.md` and a "Sponsor" button via `.github/FUNDING.yml`. Features
  stay free; donating is entirely optional.

## 0.3.0

Mice without HID++ (AJAZZ 2.4G on the YJX controller, `a8a5:2255`).

### Added

- **Two-pane interface**: mice listed on the left grouped by vendor (Logitech,
  AJAZZ, …) with collapsible groups, the card and settings of the selected one
  on the right.
- **Discovery of mice without HID++** (`discovery`): the interfaces of one
  device are grouped, the protocol is decided by the interface, and the evdev
  node is found via `capabilities`. Touchpads and keyboards with a "mouse" node
  are not treated as mice.
- **Profiles, buttons and macros for evdev mice**: the daemon manages them via
  evdev/uinput; `set-dpi`/`set-rate`/`lighting` clearly say the model cannot do
  that. The GUI respects device capabilities.
- **YJX vendor channel (read-only)**: `mousecfg vendor` shows the model,
  firmware build dates, DPI levels, active DPI, report rate, battery and factory
  codes. The replies `03/00`, `0e/a5`, `30/a5`, `08/a5` are decoded.
- **Battery in the tray and window**: the daemon reads it once a minute
  (read-only), smooths it with a median and shows it in `status`, the tray and
  the GUI; the tray tooltip is `mousecfg · profile · battery N% · device`.
- **Writing to the mouse memory** — `mousecfg onboard show|backup|apply|restore`
  and the "Mouse memory" group in the GUI: DPI, report rate, lighting, LOD,
  sleep, response. Only with `--allow-onboard-write` (in the GUI, with a warning
  to confirm), **with a mandatory snapshot before every write** and rollback.
- **An AJAZZ 2.4G profile** (example): side buttons back/forward and a browser
  profile with tab switching.

### Changed

- **`install.sh` updates the directory**: before installing it runs
  `git pull --ff-only` (only when the tree is clean; `--no-pull` disables it).
  Re-running `./install.sh` now installs the latest version from GitHub.
- Project version: `0.3.0`.
- udev rule fix: the interface number now comes from
  `ENV{ID_USB_INTERFACE_NUM}` (a condition with `ATTRS{bInterfaceNumber}`
  together with `ATTRS{idVendor}` never matched); this also fixes the Logitech
  HID++ rule.
- `vendor/guard.py` policy: read-only by default; writing is allowed only for
  the explicit `onboard` operation and is listed in `mousecfg judge`.

### Safety

- Command `0d/00` (clears a range of memory when the body is non-empty) is never
  sent.
- A value is written on top of a live read: service and unknown bytes are not
  overwritten.
- The button mapping of this mouse is not readable, so our `restore` does not
  restore it — rollback is only via the web configurator / vendor software.

## 0.2.0

- An explicit HID++ feature allow-list, a journal and `mousecfg judge`.
- GUI, tray, per-application profiles, macros, hotkeys.

## 0.1.0

- First release: reading and volatile settings for Logitech HID++ 2.0.
