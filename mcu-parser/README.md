# mcu-parser

Turn a vendor's schematic PDF into a Betaflight `config.h`.

Three tools, run in this order:

| Tool | Input | Output |
|------|-------|--------|
| `seed_firmware.py` | a Betaflight source tree | `data/firmware.json` — what each MCU pin can do |
| `netmap.py` | schematic PDF | which net sits on which MCU pin |
| `genconfig.py` | schematic PDF | a complete `config.h` |

`extract.py` is the older component lister (regex over `pdftotext -layout`,
inspired by [rohansat/SchematicParser](https://github.com/rohansat/SchematicParser)).
It still works and is useful for a quick "what parts are on this board", but it
does no pin mapping.

## Quick start

```bash
# Once, and again whenever the firmware moves:
python3 mcu-parser/seed_firmware.py

# Then per board:
python3 mcu-parser/genconfig.py board.pdf \
    --board EXAMPLEF722 --manufacturer CUST -o out/
# -> out/configs/CUST/EXAMPLEF722/config.h  (out/ works as CONFIG_DIR)
```

Inspect just the pin map, without generating anything:

```bash
python3 mcu-parser/netmap.py board.pdf
```

Needs `pdftotext` from poppler (`sudo apt install poppler-utils`, or
`brew install poppler`).

## Why it is built this way

**Text extraction uses `pdftotext -bbox-layout`, not pdfplumber.** On these Altium
plots pdfplumber returns *none* of the MCU symbol's pin-name strings and merges
neighbouring labels into nonsense (`04-1K0/56%03LED-RED`). poppler returns every
one cleanly, with coordinates.

**The pin↔net offset is derived, not hardcoded.** Altium centres pin names on the
wire but draws net labels above it, and the gap scales with the sheet's font size.
`netmap.py` sweeps candidate offsets and keeps whichever the firmware agrees with
most.

**The firmware is the source of truth for pin capability.** Some symbols spell out
alternate functions (`PA0/TIM2-CH1/TIM5-CH1/UART4-TX/ADC123-IN0`), many just say
`PA0`. `seed_firmware.py` parses the tables under `src/platform/` so the pipeline
never depends on the PDF being generous:

* `fullTimerHardware` → the 1-based `TIMER_PIN_MAP` occurrence index
* `dmaTimerMapping` / `dmaPeripheralMapping` → valid DMA options per resource
* `uartHardware`, `spiHardware`, `i2cHardware`, `adcTagMap` → pin capability
* `USE_*` driver defines → part number to driver mapping

**Geometry proposes, firmware validates.** Every net whose name implies a function
is checked against that pin's real capability, and the agreement score is
reported. A net called `TX4` must land on a pin that genuinely has UART4 TX. An
off-by-one-row error scores near zero and cannot survive:

```
symbol: 50 pins, pitch 3.65pt, AF lists present
offset +0.30pt  agreement 29/29 (100%)
```

Treat anything below 100% as a result to inspect, not to ship.

## What is inferred, and how

* **SPI bus per device** — solved for all devices at once, not per device. On an
  F7, `PB3/PB4/PB5` are valid SPI1 *and* SPI3 pins, so a flash chip wired there
  looks like SPI1 in isolation and would steal the bus from a gyro on
  `PA5/PA6/PA7` that has nowhere else to go. Devices with identical data pins share
  a bus (an OSD and a baro on one SPI2 is normal); devices with different data pins
  cannot.
* **Timers** — schematic annotations win. If the sheet says `MOTOR1-TIM8 CH1`, that
  is used and reported as `schematic`; otherwise a timer is chosen that all motors
  share (so burst DShot stays available), preferring advanced-control timers, and
  reported as `inferred`.
* **DMA options** — the numbering means two different things, and the tool follows
  whichever applies:
  * *fixed mapping* (F4, F7) — `dmaopt` indexes that timer channel's own short
    list of possible streams, so several peripherals can share opt 0 and still
    land on different streams. The ADC option is then chosen to dodge the streams
    the timers occupy.
  * *DMAMUX / GPDMA* (G4, H5, H7, C5, N6) — any request routes to any channel, so
    `dmaopt` is a direct index into one shared channel table. Every user needs its
    own number; four motors on opt 0 would all claim the same channel. Timers are
    numbered 0, 1, 2… and the ADC continues the sequence, which is what the
    hand-written G4 and H7 configs do.
* **UART roles** — read off connector silkscreen (`J5 GPS`, `J7 Receiver`,
  `J2 ESC`, `J4 DJI O4`). A header showing a UART in both directions beats one that
  appears one way only, so a DJI header's MSP link wins over its SBUS output.
* **`BEEPER_INVERTED`** — assumed, because a transistor low-side driver is near
  universal. A bare open-drain buzzer would need it removed.
* **PINIO polarity** — a net switching a regulator enable is emitted as `129` (boot
  high), since such rails are usually held up by their own divider and the box acts
  as an off switch. Flagged for vendor confirmation every time.

## Diagnostics

A net that cannot be placed is **never dropped in silence** — a silently omitted
net just looks like an incomplete config later, with no clue why. Every one is
reported, and excluded from the file rather than emitted as a define that cannot
work:

* **Net on a pin the firmware does not support for that function.** The symbol's
  own AF list is consulted as a second opinion, which splits this into two very
  different cases:
  * *the symbol agrees* — likely a gap in Betaflight's pin tables. An `RX3` net on
    `PC4` is rejected because the H5 USART3 table lists only PB11/PC11/PD9, yet
    the H562 datasheet does give PC4 `USART3_RX` on AF7. That is a firmware fix.
  * *the symbol disagrees too* — almost certainly a schematic error.

  Symbols are corroboration, not proof: the same sheet claimed `UART7_TX` on
  `PA10`, where the datasheet shows AF11 empty and no UART7 function at all. Always
  confirm in the datasheet AF table before changing firmware. `--trust-symbol`
  emits the first case anyway, for when you are going to add the pin option.
* **Net on a supply or system pin.** `I2C1-SDA` landing on a row the symbol names
  `VCAP` is a schematic problem, not a parsing one, and is called out as such. A
  `BOOT` net on `BOOT0` is recognised as correct and stays quiet.
* **Net label matching no pin row at all** — listed verbatim.
* **Half-configured buses** — a UART with only TX, or an I2C with only SCL. Usually
  the downstream symptom of one of the above, and shown next to it.

Multi-part pin names are reassembled before any of this happens. Long alternate
function lists come back from the extractor in pieces
(`PC6/TIM3_CH1/TIM8_CH1/` + `USART6_TX`), and since right-hand names are aligned on
the symbol edge, the piece holding the `PXn` token misses that edge — which used to
drop the whole row, pin and net together.

## Alternate footprints

Vendors put unfitted alternates on the same sheet so one PCB can be built several
ways (`MPU-6000` fitted beside `ICM42688(NC)` and `SPA06-003(NC)`). Every variant is
detected, `(NC)`/`(DNP)`/`(NF)` markings are recognised, and drivers for all of them
are enabled — which is what the alternate footprint is for. Which parts were marked
unfitted is reported. If a category has *only* unfitted candidates, that is a
warning rather than a note.

## What it will not guess

Reported on stderr as `WARN`, never silently filled in:

* **`GYRO_1_ALIGN`** — orientation is a board-layout property; a schematic cannot
  express it. Pass `--gyro-align` once the vendor confirms.
* **`DEFAULT_CURRENT_METER_SCALE`** — set by the ESC's shunt, not the FC.
* **Any device whose only net is a CS line** — the sheet does not say which bus it
  shares, so its `*_SPI_INSTANCE` is left to a human.

## Net naming

Both common conventions are handled: device-named (`GYRO-SCK`, `ADC-BATT`,
`MOTOR1`) and bus-named (`SPI1_SCK`, `VBAT_ADC`, `S1`). Nets matching nothing are
listed as a note rather than dropped in silence — that is the signal to add a rule
to `ROLE_RULES` in `genconfig.py`.

## Keeping in sync with firmware

`data/firmware.json` records the git rev it came from:

```json
"firmware": { "rev": "81da7c596", "branch": "master", "date": "2026-07-24" }
```

Re-run `seed_firmware.py` after firmware changes. This matters more than it
sounds: PR #15470 renamed the `STM32F7X2` target to `STM32F722` and `STM32G47X` to
`STM32G474`, so a stale data file emits an `FC_TARGET_MCU` the build system no
longer recognises. Auto-detection prefers the newest `master` worktree for the same
reason; pass `--firmware PATH` to pin it.

`data/aliases.json` is the one hand-maintained file — silkscreen markings that
differ from Betaflight's driver names (`AT7456E` → `MAX7456`, `W25Q128JVEIQ` →
`W25Q128FV`, `SPA06-003` → `SPA06_003`). Anything matching a driver name directly
needs no entry.

Currently harvested: all 19 STM32 targets (F4, F7, G4, H5, H7, C5, N6). The AT32,
APM32 and PICO platforms use differently-shaped tables and are not covered yet.

## Verification

Checked against a config derived by hand from a real vendor schematic: the
generator reproduces every pin, bus, timer index, feature define and default, and
the result builds against Betaflight master (77.9% flash on an F722). A second
board on a DMAMUX part independently reproduces the DMA numbering of a
hand-written config already in the config repo.
