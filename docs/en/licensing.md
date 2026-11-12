# Licences and code provenance

[Русский](../licensing.md) · **English**

## Project licence

GPL-3.0-or-later (see `LICENSE`).

## Third-party code in the project

None. All code in `src/mousecfg` was written for this project.

## How the installed Solaar was used

The development machine has `solaar 1.1.20` installed
(`/usr/lib/python3.14/site-packages/logitech_receiver/`). It was read as a
**protocol reference**: facts were taken from it, not expressions.

Facts (not protected by copyright):

* HID++ 2.0 feature identifiers (`0x2201` DPI, `0x8060` report rate, `0x8100`
  onboard, `0x8071` lighting, `0x8110` button spy, etc.);
* function numbers inside features (`0x10` read, `0x20` write, `0x50` read a
  sector) and mode values (`0x01` On-Board, `0x02` Host);
* Logitech button codes (`0x0050` left, `0x0051` right, `0x0052` middle,
  `0x0053` Back, `0x0056` Forward, `0x00C4` DPI);
* transport behaviour: `0x10`/`0x11`, device index `0xFF` for a wired
  connection, Software ID as the low nibble of the function byte.

Additionally, the licences of dependencies the project does not link against in
code (they are used as separate programs and are not embedded):

| Program | Licence | How it is used |
|---|---|---|
| Solaar | GPL-2.0 | only as a protocol reference and as an "oracle" during verification |
| libratbag / ratbagd | GPL-2.0 | behaviour comparison only (`ratbagctl info`) |
| Piper | GPL-3.0 | a source of UX ideas, no code taken |
| OpenRazer | GPL-2.0 | a plan for the future for Razer devices |

If their code ever needs to be embedded, the project licence is re-checked:
GPL-2.0-only code cannot be distributed as part of a GPL-3.0 project.
