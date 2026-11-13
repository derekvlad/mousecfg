# Writing to the mouse memory (onboard)

[Русский](../onboard.md) · **English**

A mouse whose profiles live on the host (see `docs/architecture.md`) does not
write to the device memory by default. But some mice (AJAZZ 2.4G on the YJX
controller) have their own memory where settings can be saved so that they
survive disconnection and work on any computer without the daemon.

For that there are dedicated `mousecfg onboard` commands. **This is the only
place where the project changes the mouse non-volatile memory.** Daemon
profiles never touch it.

## In short: what is possible

```sh
mousecfg onboard show                     # read the current settings
mousecfg onboard backup                   # snapshot into ~/.config/mousecfg/backups
mousecfg onboard apply --dpi 1600 --allow-onboard-write
mousecfg onboard apply --rate 1000Hz --allow-onboard-write
mousecfg onboard restore --allow-onboard-write    # restore the latest snapshot
```

The GUI does the same — the **"Mouse memory"** group: pick a DPI level, set the
value, "Snapshot", "Restore snapshot", "Write DPI to the mouse".

## What exactly is stored in the mouse

For the AJAZZ 2.4G (`a8a5:2255`, `mkespn` dialect):

| Field | How to set it |
|---|---|
| DPI levels (up to 6) and the active level | `onboard apply --dpi N --dpi-level K` (K = 0..5) |
| Report rate | `onboard apply --rate 1000Hz` (or `1ms`, or code 1..4) |
| Lighting mode | `onboard apply --light-mode N` |
| LOD, sleep, response, high-speed | `--lod`, `--sleep-light`, `--key-respond`, `--highspeed` |

A value is written **on top of a live read**: service and unknown bytes
(including the one that changes by itself) are left as they are. The button
mapping write (`09/a5`) also exists in the code but is not wired to the CLI/GUI
yet: this mouse does **not** expose the mapping for reading, so our `restore`
cannot roll it back — only the web configurator can. That is why we do not do
it by default.

## Why it is safe

1. **Snapshot before the write.** Before every write a full configuration
   snapshot is saved into `~/.config/mousecfg/backups/` (a JSON with the full
   frame and a sha256 fingerprint). Roll back with `mousecfg onboard restore`.
2. **Consent.** Without `--allow-onboard-write` (in the GUI, without confirming
   the warning) writing never happens. On the first attempt a warning with the
   risks is shown; consent is remembered per device
   (`~/.config/mousecfg/onboard-consent.json`).
3. **Narrow allow-list.** Only `0f/ae` (configuration) and `09/a5` (buttons, if
   enabled) frames are sent. Command `0d/00`, which with a non-empty body
   **clears** a range of memory, is never sent (`mousecfg judge` lists the
   policy).
4. **The daemon never writes.** Profiles stay volatile; writing is available
   only via an explicit command. The daemon just serves the GUI with the same
   RPC methods.

## If something goes wrong

Recovery order:

1. `mousecfg onboard restore --allow-onboard-write` — write the latest snapshot.
2. The AJAZZ web configurator (**Onboard configuration** tab) → **Import** the
   snapshot file (`~/.config/mousecfg/backups/…json`) or **Reset to default**.
3. The vendor software / web configurator with "Reset to default".

The button mapping is not restored by our `restore` (see above) — only steps
2–3.

## What remains unknown

The protocol was reverse-engineered by the community and the AJAZZ web tool,
not documented by the vendor. Not decoded: the configuration tail bytes
(LOD/sensor — written as-is, not interpreted), the factory code meaning, the
full model catalogue. The field offsets were verified against a live AJAZZ 2.4G
— on other models of the family they may differ, so take a snapshot first.
