# schema-convert

Convert a flight-controller schematic PDF into a Betaflight `config.h`.

Vendors submit board designs as Altium PDF plots. This turns one into a
buildable target: MCU pin map, peripheral instances, timer indices, DMA options
and feature defines — with every inference checked against Betaflight's own
hardware tables rather than guessed.

```bash
python3 mcu-parser/seed_firmware.py                     # once, and after firmware moves
python3 mcu-parser/genconfig.py board.pdf \
    --board EXAMPLEH562 --manufacturer CUST -o out/
# -> out/configs/CUST/EXAMPLEH562/config.h  (out/ works as CONFIG_DIR)
```

Then verify it builds:

```bash
cd /path/to/betaflight && make CONFIG=EXAMPLEH562 CONFIG_DIR=/path/to/out
```

## The tools

| Tool | Input | Output |
|------|-------|--------|
| `mcu-parser/seed_firmware.py` | a Betaflight source tree | `data/firmware.json` — what each MCU pin can do |
| `mcu-parser/netmap.py` | schematic PDF | which net sits on which MCU pin |
| `mcu-parser/genconfig.py` | schematic PDF | a complete `config.h` |
| `mcu-parser/extract.py` | schematic PDF | quick component listing (no pin mapping) |

See [`mcu-parser/README.md`](mcu-parser/README.md) for how the pin map is
recovered, what is inferred, what is deliberately left to a human, and how to
keep the firmware data in sync.

## The idea

**Geometry proposes, firmware validates.** Pin↔net association is recovered from
the PDF's text coordinates, then every net whose name implies a function is
checked against that pin's real capability, harvested from the firmware's own
`src/platform/` tables. A net called `TX4` has to land on a pin that genuinely
has a UART4 TX function, so an off-by-one-row error cannot survive. The agreement
score is printed; anything below 100% wants inspecting.

Where the two disagree, the schematic symbol's own alternate-function list acts
as a tiebreaker — which separates "Betaflight is missing a pin option" from "this
schematic is wrong". Both happen.

## Requirements

`pdftotext` from poppler, and Python 3.12+. No Python packages — the pipeline is
standard library only.

```bash
./install.sh          # checks both, and the firmware data
```

pdfplumber is deliberately not used: on these Altium plots it returns none of the
MCU symbol's pin-name strings and merges adjacent labels into nonsense
(`04-1K0/56%03LED-RED`). poppler returns every one cleanly, with coordinates.

## Not in this repo

Vendor schematics and the `config.h` files generated from them are excluded by
`.gitignore` and never committed. They are confidential submissions describing
unreleased hardware. `mcu-parser/data/firmware.json` is also excluded — it is
generated from a local Betaflight checkout and records which one.

## Limitations

- STM32 only so far; AT32, APM32 and PICO peripheral tables have a different
  shape and are not harvested yet
- Gyro orientation and the current-meter scale cannot be derived from a schematic
  and are always flagged for the vendor
- A device whose only net is a chip-select cannot have its SPI instance inferred
- `TIMUPn_DMA_OPT` is never emitted, so a DMAMUX board wanting burst DShot
  (`DSHOT_DMAR_ON`) needs that added by hand
- The generated file is a strong first draft, not a substitute for review: check
  the agreement score and every `WARN` before shipping a target

[`ROADMAP.md`](ROADMAP.md) has the full gap analysis: known defects, which
`config.h` defines are never emitted (with how many real boards use them), and
what would need circuit-level analysis rather than net-label matching.
