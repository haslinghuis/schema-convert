# Gaps and further work

Findings from working real vendor submissions through the pipeline. Ordered by
what would bite first, not by effort.

Percentages are the share of the 619 configs in the Betaflight config repo that
use a given define — a proxy for how often a gap will actually be hit.

---

## 1. Defects — wrong today

### 1.1 Multi-page schematics are flattened into one coordinate space

`netmap.extract_words()` regexes every `<word>` element out of the
`pdftotext -bbox-layout` output without tracking which `<page>` it came from.
Every page of an A-series sheet shares the same coordinate range, so words from
page 1 and page 4 collide:

```
pages: 5
  page 1: 275 words,   0 pin-name tokens, y range 29-561
  page 2: 214 words,   0 pin-name tokens, y range 29-561
  page 3: 429 words,   0 pin-name tokens, y range 29-561
  page 4: 514 words,  36 pin-name tokens, y range 29-561
  page 5: 508 words,   0 pin-name tokens, y range 29-561
```

One of the boards processed so far is a 5-page schematic. It still scored 18/18,
because the MCU symbol dominates its own column and the firmware check rejects
nonsense — but that is luck, not design. Words from four unrelated pages are
competing to be its net-label column, and unexplained unclassified nets on that
board are the visible symptom.

**Fix:** parse per `<page>`, run symbol detection on each, and take the page with
the strongest MCU symbol (or merge, if a design splits the MCU across sheets —
large boards do). Cheap, and it removes a whole class of silent misreads.

**Until then:** treat any result from a multi-page PDF as suspect regardless of
the agreement score.

### 1.2 `SYSTEM_HSE_MHZ` is never emitted — wrong for F4 and G4

Used by **41%** of boards. `config.c` marks it *"Only used for F4 and G4
targets"*, and it was deliberately dropped after an F7 conversion where it is
genuinely inert. That reasoning does not carry to F4/G4, where omitting it means
the wrong crystal frequency and a mis-derived clock tree.

The value is recoverable: crystal parts carry their frequency in the silkscreen
(`3X2.1X1_8MHZ`, `2016-27Mhz`), which the component scan already sees.

**Fix:** detect the crystal near the MCU, emit `SYSTEM_HSE_MHZ` for F4/G4
targets only, and warn if no crystal is found on a target that needs one.

---

## 2. Coverage — defines never emitted

Measured by generating configs for three boards and diffing the emitted define
set against the corpus.

| Define | Boards | Note |
|---|---|---|
| `DEFAULT_CURRENT_METER_SCALE` | 61% | ESC-dependent; see §3.2 |
| `RX_PPM_PIN` | 54% | no rule in `ROLE_RULES` at all |
| `DEFAULT_DSHOT_BURST` | 51% | never emitted; see §2.1 |
| `SYSTEM_HSE_MHZ` | 41% | defect, see §1.2 |
| `GYRO_2_*` (`CS`/`EXTI`/`SPI_INSTANCE`) | 25% | dual-gyro boards unhandled |
| `ESCSERIAL_PIN` | 18% | no rule |
| `DEFAULT_VOLTAGE_METER_SCALE` | 15% | computable; see §3.2 |
| `USE_SDCARD` | 15% | `sdcard_cs` role exists but the feature define is not emitted |

`MOTOR5-8`, `UART6+`, `SPI4`, `ADC_RSSI_PIN` show up as absent only because the
three test boards lack them — those are built generically and do work.

### 2.1 Burst DShot is not supported

`TIMUPn_DMA_OPT` is never emitted, so a DMAMUX board wanting `DSHOT_DMAR_ON`
needs hand editing. The generator always emits `DSHOT_BITBANG_ON`, which is a
safe default but not always the right one — four motors on one timer is the case
burst exists for, and the tool already detects exactly that when choosing a
shared timer.

### 2.2 Dual-gyro boards

25% of boards have a second gyro. `netmap` would map `GYRO2-CS` fine; the
classifier has no rule and the SPI solver has no concept of a second IMU.

---

## 3. Capability — needs analysis the tool cannot yet do

### 3.1 Datasheet alternate-function tables are not ingested

The biggest structural gap. When the firmware rejects a pin, the tool falls back
to the schematic symbol's own AF list as a second opinion — but a symbol can be
wrong. A real submission claimed `UART7_TX` on a pin whose datasheet AF row is
empty, while another pin it used really did have `USART3_RX` and the firmware
table was the thing at fault. Only the datasheet distinguishes these, and the
tool cannot read one.

A working prototype exists (built ad hoc while auditing an H5 UART table): parse
the AF tables out of an ST datasheet PDF by column geometry, yielding
`pin -> {function: AF number}`. Validated against 16 pins the firmware already
had — 14 exact, and the 2 mismatches were genuine firmware bugs.

**Value beyond this tool:** the same data audits Betaflight's own pin tables. Run
across the H5 UART and I2C tables it found three real defects in 70 pairs. Every
other MCU family is unaudited.

**Effort:** moderate. The parser exists; productising means handling per-family
table layouts and sourcing datasheets.

### 3.2 No circuit-level analysis

Everything today is net-label and component matching. Several values sitting in
plain sight need following a two-resistor network:

- **`DEFAULT_VOLTAGE_METER_SCALE`** — the VBAT divider. One board's `100K/10K`
  gives exactly the default 110, which was confirmed by hand; a board with a
  different divider silently gets the wrong scale.
- **`BEEPER_INVERTED`** — currently *assumed*. Determined by hand from
  `BEEPER → 1K → NPN base, collector → BUZZER-`. A bare open-drain buzzer would
  need it removed, and the tool would not notice.
- **PINIO polarity** — `129` vs `1` is assumed from the net name. The real answer
  is whether the regulator enable is held up by its own divider.
- **`DEFAULT_CURRENT_METER_SCALE`** — genuinely ESC-dependent when the FC just
  filters a sense line, but a board with an on-board shunt could be computed.

**Effort:** high. Needs component pins associated to nets by geometry, i.e. a
partial netlist, not just labels. Highest-value single piece is the VBAT divider.

### 3.3 CS-only devices cannot be placed on a bus

If a sheet gives a device only a chip-select and its data lines are drawn
elsewhere, the SPI instance is left to a human. Fixable by tracing wires, which
needs the same netlist work as §3.2.

### 3.4 Only STM32 families are harvested

AT32, APM32, PICO and X32 keep their peripheral tables in a different shape.
`seed_firmware.py` skips them, so those boards cannot be converted at all.

---

## 4. Engineering

### 4.1 No test suite

Every change so far has been verified by manually regenerating boards and
eyeballing diffs. That caught two real regressions, but it does not scale and
will not survive a refactor.

**Fix:** golden-file tests — commit the expected `config.h` for a few boards
(they are gitignored here, so a fixture directory outside the repo or sanitised
fixtures) plus the netmap JSON, and assert byte-equality. The pipeline is
deterministic, which makes this straightforward. Also worth asserting the
invariants that already exist informally: agreement is 100%, no net is dropped
silently, every emitted pin is firmware-validated.

### 4.2 `MANUFACTURER_ID` is not validated

The ID must appear in the config repo's `Manufacturers.md`. This was checked by
hand for both boards converted so far. The file is a parseable markdown table —
validate against it and fail loudly on an unregistered ID.

### 4.3 Firmware array limits are invisible

`UARTHARDWARE_MAX_PINS` is 5 on H5 and 4 on F4/F7. A pin sweep hit that ceiling
and the generator has no idea the limit exists — it would happily reference a
pin the firmware table has no room for. Seed the limits and check against them.

### 4.4 `config.c` is not supported

The config repo allows an optional `config.c` next to `config.h` for
board-specific init. Not emitted, and not detected when one would be needed.

### 4.5 Provenance is weak

The schematic sha256 is recorded, but nothing ties a generated config to the
firmware revision it was validated against. A config generated against a patched
tree looks identical to one generated against release firmware — which already
mattered once, when a board depended on an unmerged pin-table fix. Record the
seeder's firmware rev in the generated header.

---

## Suggested order

1. **§1.1 multi-page** — silent wrong answers, cheap fix
2. **§1.2 `SYSTEM_HSE_MHZ`** — wrong output for 41% of boards, cheap fix
3. **§4.1 tests** — everything after this is safer with them
4. **§4.2 / §4.3 / §4.5** — small, high signal-to-noise
5. **§2 coverage** — mechanical, mostly new `ROLE_RULES` entries
6. **§3.1 datasheet AF** — highest value, moderate effort, and pays off across
   Betaflight itself rather than just here
7. **§3.2 circuit analysis** — highest effort; start with the VBAT divider alone
