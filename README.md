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

## Testing

```bash
python3 -m unittest discover tests
```

Standard library only, a few seconds, three layers:

- **Unit and property tests** against a synthetic MCU and a synthetic sheet —
  net classification, the SPI bus solver, timer occurrence indices, DMA
  numbering, preprocessor guard evaluation, and the label→row offset being
  recovered rather than assumed. These need nothing but the repo.
- **Invariants over real conversions** — no net dropped in silence, every
  emitted pin firmware-validated, every `TIMER_PIN_MAP` label defined, SPI
  assignment conflict-free, DMA options distinct on DMAMUX parts.
- **Golden digests** of the recovered pin map and of the generated `config.h`.

Vendor schematics are confidential and not in the repository, so the tests that
need one skip with a message saying where they looked (`SCHEMA_CONVERT_PDF_DIRS`
overrides the search path; `SCHEMA_CONVERT_FIRMWARE` points at the Betaflight
tree used to re-run the seeder). Nothing sensitive is committed either: a board
is identified by the sha256 of its PDF and its output recorded as counts and
one-way digests, and on a mismatch the actual output is written to the
gitignored `tests/.actual/` for a local diff. The firmware capability data is
pinned as a fixture too, so re-seeding from a moving Betaflight tree cannot move
a golden.

Re-record after an intended change, having read the printed diff:

```bash
python3 tests/update_golden.py
```

Behaviour that is wrong but currently tolerated is listed per board under
`known_defects` and asserted exactly — a new one fails, and so does fixing a
recorded one.

## Not in this repo

Vendor schematics and the `config.h` files generated from them are excluded by
`.gitignore` and never committed. They are confidential submissions describing
unreleased hardware.

`mcu-parser/data/firmware.json` **is** committed, so a clone converts something
without a Betaflight checkout first. Every byte of it is harvested from
Betaflight's own GPL sources. It records the rev, branch and date it came from
and deliberately not the path — the risk in generated data is rarely the data,
it is the provenance stapled to it, and a checkout path can name the submission
it was cut for. `tests/check_seed_drift.py` runs in CI to catch it going stale.

## Limitations

- STM32 and AT32; APM32 and PICO are not harvested yet. The AT32 tables have no
  datasheet to audit against, so unlike every STM32 family they are trusted
  exactly as far as Betaflight's own port is
- Gyro orientation, the current-meter scale and PINIO polarity cannot be derived
  from a schematic at all, and are always flagged for the vendor
- A device whose only net is a chip-select is usually traced to its bus at the
  device end, but not always — where the evidence is not decisive it says so and
  leaves the instance to a reviewer
- `TIMUPn_DMA_OPT` is never emitted, so a DMAMUX board wanting burst DShot
  (`DSHOT_DMAR_ON`) needs that added by hand
- Some PDFs cannot be read by anything here: scans have no text layer, and some
  CAD exporters write every glyph as a numbered drawing procedure with no
  character mapping. Both are reported as such rather than as a parse failure
- The generated file is a strong first draft, not a substitute for review: check
  the agreement score and every `WARN` before shipping a target

Where a net cannot be followed at all — a page that was not supplied, a drawing
convention nothing here reads — the report names what a target normally has and
this sheet did not produce, and you can supply it:

```bash
python3 mcu-parser/genconfig.py board.pdf --board NAME --manufacturer ID \
        --set MOTOR6=PE11 --set ADC_RSSI=PC5
```

The reverse happens too — a revision moves a function and leaves the old label
drawn, or the sheet shows a net the assembled board does not fit — and nothing
on the drawing tells that from a live one, so it can be stated:

```bash
python3 mcu-parser/genconfig.py board.pdf --board NAME --manufacturer ID \
        --drop ADC_RSSI
```

`NAME` is the function as the sheet would name it — the `_PIN` belongs to
`config.h` and is added on the way out, though typing it does no harm. It goes
through the same reader the sheet's own nets do, so every spelling it
understands works here. A supplied value is still checked against the firmware
tables and **refused** if the pin cannot do the job; those tables are audited
against ST's datasheets by `afaudit.py`, so a refusal means the pin is wrong. It
replaces anything read for the same role, and both facts are recorded in the
report, because a config that mixes what was read with what was asserted and
does not say which is the one failure mode worth avoiding.

The desktop app does the same thing with a box per function.

[`ROADMAP.md`](ROADMAP.md) is what is still open: which `config.h` defines are
never emitted (with how many real boards use them), and what would need
circuit-level analysis rather than net-label matching.
[`FINDINGS.md`](FINDINGS.md) is every defect the tool has had and how it was
found — worth reading before changing the parser, since most of what has gone
wrong here has gone wrong more than once.

## License

GNU General Public License v3.0 or later — see [`LICENSE`](LICENSE). The same
licence Betaflight uses, which matters here because this tool reads Betaflight's
hardware tables and writes files that go back into its config repo.

A generated `config.h` carries Betaflight's own GPL header, as the hand-written
targets do. The schematic it was generated from is the vendor's and is not
covered by this licence.
