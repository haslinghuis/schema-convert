# Gaps and further work

Findings from working real vendor submissions through the pipeline. Ordered by
what would bite first, not by effort.

Percentages are the share of the 619 configs in the Betaflight config repo that
use a given define — a proxy for how often a gap will actually be hit.

## The corpus is the instrument

Everything below §1.4 was found by running **168 vendor schematics** end to end
rather than by reading the code. That is worth stating plainly, because none of
those defects were visible on the three boards the tool had been developed
against, and several were invisible *by construction*: they dropped input
silently while every diagnostic the tool prints stayed clean.

The measurements that matter are aggregate, and they are what a change should
be judged against:

| | before the corpus | now |
|---|---|---|
| schematics that yield a pin map | 104 / 168 | 104 / 168 |
| MCU pins read | 3090 | 3615 |
| nets checked against firmware | 1637 | 1733 |
| of those, agreeing | 1601 | 1691 |
| UART pins emitted | 299 | 576 |
| boards with any UART pin | 34 | 70 |
| boards at 100% agreement | 86 | 89 |
| SPI devices needing a hand-set instance | 77 | 27 |
| boards whose SPI section is complete | 60 | 83 |
| boards with a computed `DEFAULT_VOLTAGE_METER_SCALE` | 0 | 11 |

Run it before and after any change to extraction or classification. A change
that improves one board and quietly costs three is the normal failure mode
here, and only the aggregate shows it. Two of the three fixes below looked
correct on their motivating board and were caught this way.

## What is still open

Sizes are measured against the 168-schematic corpus, not estimated. The detail
is in each section; this is the index.

| | | boards |
|---|---|---|
| §2.4 | **Second I2C bus** — `genconfig` holds one `{scl, sda}`, so the second overwrites the first. No board emits two. Small, mechanical. | 29 show the collision |
| §3.3 | Chip-select-only devices whose CS is never drawn away from the MCU — the genuine wire-tracing cases | 27 devices |
| §3.5 | Sheets that never name their MCU. Not broken: 100% agreement given `--target` | 34 |
| §3.4 | **AT32** peripheral tables are not harvested, so those boards cannot be converted at all | 17 |
| §2.3 | SD card over **SDIO/SDMMC** — only the SPI form is implemented | 12 |
| §4.5 | `config.c` is neither emitted nor detected as needed | — |

Not worth prioritising, and the reason matters:

- **§2.1 burst DShot.** `TIMUPn_DMA_OPT` is never emitted, but **no** corpus
  board puts all four motors on one timer, which is the case burst exists for.
- **§3.2 remainder.** PINIO polarity and `DEFAULT_CURRENT_METER_SCALE` are not
  derivable from a schematic *at all* — closed as impossible, not open. Both are
  flagged for the vendor rather than guessed.
- **21 sheets print no crystal frequency anywhere.** Not recoverable from the
  PDF; `--hse-mhz` or the vendor.

---

## 1. Defects

### 1.1 Multi-page schematics flattened into one coordinate space — FIXED

*Resolved. Extraction is now page-aware; the record below is kept because the
failure mode is instructive and the second bug it exposed is worth remembering.*

`netmap.extract_words()` used to regex every `<word>` element out of the
`pdftotext -bbox-layout` output without tracking which `<page>` it came from.
Every page of an A-series sheet shares the same coordinate range, so words from
page 1 and page 4 collided:

```
pages: 5
  page 1: 275 words,   0 pin-name tokens, y range 29-561
  page 2: 214 words,   0 pin-name tokens, y range 29-561
  page 3: 429 words,   0 pin-name tokens, y range 29-561
  page 4: 514 words,  36 pin-name tokens, y range 29-561
  page 5: 508 words,   0 pin-name tokens, y range 29-561
```

One of the boards processed is a 5-page schematic. It still scored 18/18,
because the MCU symbol dominates its own column and the firmware check rejects
nonsense — but that was luck, not design. Words from four unrelated pages were
competing to be its net-label column, and unexplained unclassified nets on that
board were the visible symptom.

**Fixed by:** parsing per `<page>`, running symbol detection on each, ranking by
distinct GPIO pins, and locking net labels to the symbol's own sheet. A split
MCU merges only on strong evidence (disjoint pin sets, comparable size, matching
pitch); a rival page that fails that test is reported, never silently dropped.
`--page N` overrides, and the header now reads `31 pins on page 4 of 5`.

Result on the 5-page board: the offset moved from +2.99pt to +0.60pt — matching
every other board's convention, which is strong evidence the old fit was being
dragged by foreign text. Three unclassified nets (`HOLD`, `COU42`, `VTX`) turned
out to be labels from other sheets, each of which had been *stealing a pin from a
legitimate page-4 net*. The three single-page boards are byte-identical.

#### The second bug this exposed: phantom symbol edge

Isolating one page revealed a latent defect that had been masked. On a
**single-column** symbol, right-aligned names of unequal length share an `x1` but
not an `x0`, so the x0 clustering produced a phantom second "edge" holding just
the short names about 8pt from the real one. The overlap guard only discarded the
duplicate when the two clusters were *identical*.

That board's "50 pins" were really **31 pins with 19 counted twice**, and the
phantom left edge was pairing against whatever text happened to sit to its left
(`PIC1301 → PB3`, `P → PB4`). The guard now removes items the larger edge already
claimed.

Worth noting how this hid: the generated `config.h` was byte-identical before and
after, because none of the bogus nets matched a `ROLE_RULES` entry and so never
reached the output. A silent internal corruption that happened not to surface —
exactly the class of thing §4.1's invariant tests exist to catch.

### 1.2 `SYSTEM_HSE_MHZ` was never emitted — FIXED, and it was worse than described

Used by **41%** of boards. This entry originally said "wrong for F4 and G4",
reasoning from the comment in `src/main/config/config.c`:

```c
.hseMhz = SYSTEM_HSE_MHZ,  // Only used for F4 and G4 targets
```

**That comment describes only one of two paths, and taking it at face value was
wrong.** The define has a second, entirely separate compile-time effect:

- **Runtime path** — `config.c` seeds `systemConfig()->hseMhz`, which `fc/init.c`
  feeds to `systemClockSetHSEValue()`. Gated by `PLATFORM_TRAIT_CONFIG_HSE`,
  defined only for F4/G4. This is what the comment means.
- **Compile-time path** — `mk/config.mk` preprocesses the value out of `config.h`
  and exports it as `-DHSE_VALUE`, which the platform clock init consumes
  directly. On H5: `#if HSE_VALUE == 8000000 … #elif == 25000000 … #else #error`,
  selecting the PLL M/N ratios. H7, C5 and N6 have equivalents.

The trap is `Makefile:184`:

```make
HSE_VALUE ?= 8000000
```

**Omitting the define is not "no HSE" — it silently compiles as 8 MHz.** So an H5
board with a 25 MHz crystal builds perfectly and asks PLL1 for M=2/N=125: a
12.5 MHz PFD outside the VCIRANGE band the code declares, and a 1.56 GHz VCO
instead of 500 MHz. It would not run.

A config generated by this tool before the fix had exactly that defect. It built
clean at 23.42% flash — because 8 MHz is a *valid* branch of that `#if`. **The
successful build proved nothing**, which is the lesson worth keeping: a green
build says the constants were self-consistent, not that they match the hardware.

Affected families are therefore `{F4, G4, H5, H7, C5, N6}`. F7 really is
cosmetic — `system_stm32f7xx.c` hardcodes `PLL_M 8` and `HSE_VALUE` only feeds
clock reporting — so dropping it there was correct.

**Fixed by** associating a crystal with the MCU two ways: a shared `OSC*` net
label between the crystal and the MCU's OSC pins (which resolves a G4 board whose
crystal is 154 pt away), falling back to proximity only when decisive — within
`30 × symbol pitch`, and at least 3× nearer than any rival. Boards carry several
crystals: the H562 sheets have both a 25 MHz MCU crystal and a 27 MHz OSD clock,
and picking the wrong one is as bad as picking none. Ferrite-bead impedance specs
(`/200R@100MHZ`) are excluded. Values outside the corpus set are emitted with a
warning; an unassociable crystal emits nothing and warns rather than guessing.
`--hse-mhz N` overrides.

Verified against hand-written references: G4 → 8 (matches `SDMODELG473`), F4 → 8
(matches `JHEF405PRO`), H5 → 25 (matches `NUCLEOH563ZI`), F7 → unchanged.

#### Round two: it was still picking the wrong crystal, not just missing one

Running the corpus put 41 boards in the "no crystal found" column, and looking
at them found two ways of being *wrong* rather than silent.

**Distance was measured across sheets.** Pages share a coordinate space (§1.1),
so a crystal on page 2 and OSC pins on page 1 produce a number that means
nothing. One H562 was emitting `SYSTEM_HSE_MHZ 27` from an OSD crystal drawn on
another sheet — and `system_stm32h5xx.c` accepts only 8 or 25, so that config
would have stopped at its own `#error`. A G4 was emitting 26 from an RF module's
crystal on page 4 where its hand-written config says 8.

**Frequency was never weighed, only proximity.** 27 MHz is the MAX7456's part
and 40 MHz an RF module's. Across the 619 hand-written configs neither is the
`SYSTEM_HSE_MHZ` of a single board — they are 8 MHz on 245, then 16, 48, 25 and
24. So neither may be claimed on proximity alone. A shared OSC net label is the
sheet stating the connection outright and still wins for any frequency, but the
result is flagged; two boards reach 27 MHz that way.

**The window was 30 × the symbol's row pitch**, which is not a sheet scale.
Pitch runs from 1.4pt to 18pt across this corpus, so the window ranged from 43pt
to 540pt — rejecting an 8 MHz crystal 214pt from the OSC pins on a fine-pitch
sheet while accepting almost anything on a coarse one. The symbol's own height
and width track the drawing scale instead.

44 → 46 boards emit it: four gained, two withdrawn because they were wrong. The
58 that still omit are 19 F7 (deliberate — the PLL input is hardcoded), 21
sheets that print no frequency anywhere, 9 carrying only OSD or RF crystals, 5
whose symbol never names its OSC pins, and 4 with the crystal on another sheet.
Each says which, and points at `--hse-mhz`.

The lesson is the same one as §1.7: **a rule that only counts evidence *for* a
candidate will pick a bad one when nothing better is near.** Proximity said
"this is the closest crystal"; nothing asked whether it could be an HSE at all.

### 1.3 Fixed-mapping timer DMA options are never de-duplicated — ADDRESSED

On DMAMUX parts every timer gets a distinct `dmaopt` (§ below). On **fixed-mapping**
parts (F4, F7) nothing checks the timers against each other. One F7 board has
MOTOR1/2/3 on `TIM8_CH1/CH2/CH3` all at `dmaopt 0`, which resolves to
`DMA2_S2_C0` for all three — three motors on one stream.

Latent, because `DSHOT_BITBANG_ON` is always emitted and bitbang does not use the
timer's own DMA; the hand-written configs in the config repo do the same thing.
It becomes real the moment a board wants `DSHOT_DMAR_OFF` without bitbang. The
ADC allocation already dodges streams the timers claimed — the timers just do not
dodge each other.

Found by the regression suite, which asserted the *designed* property rather than
the behaviour of the day.

**Fixed by** giving each timer row a stream nobody else holds, and saying why in
the report rather than silently renumbering:

```
MOTOR2_PIN takes DMA option 1 (DMA2_S3_C7): option 0 is DMA2_S2_C0,
and DMA2_S2 is taken by MOTOR1_PIN
```

That F7 board now lands on DMA2_S2 / S3 / S4 / S7 instead of three motors on
DMA2_S2. This diverges from the hand-written configs, which all use option 0 —
inert while `DSHOT_BITBANG_ON` is emitted, correct the moment it is not.

Burst DShot is *detected* but deliberately not enabled: all four motors being on
one timer is the case for it, yet nothing here can check that timer's update-DMA
stream is free, so it says so and leaves the choice to a human.

### 1.4 `seed_firmware._eval` mishandles arithmetic guards — FIXED

The comment says arithmetic guards such as `TARGET_FLASH_SIZE > 512` are treated
as "no obstacle", and the fallback that would do so only fires when `eval`
*raises*. It does not raise: the identifier resolves to `False`, so
`False > 512` evaluates to `False` and silently gates the line off.

**Fixed** by making a macro name answer only the question the harvest can
answer. In a boolean context it still reports whether the macro is defined, which
is what `#ifdef`-style guards ask; a comparison or arithmetic use asks for its
*value*, which is never harvested, so it yields an unknown that the guard solver
tries both ways rather than resolving to `False` by accident. The test that
pinned the old behaviour was updated in the same change.

### 1.5 Only one box of a split MCU symbol was read — FIXED

A large package is routinely plotted as **several boxes on one sheet**, one per
port group. Detection returned a single box per page, so the rest of the
package was dropped — and dropped invisibly: every pin it did read was correct
and agreement stayed at 100%. One H743 board read **43 of its 100 pins** and
reported nothing wrong. §1.1 had fixed the same failure *across* sheets; this
was the same thing within one.

Four causes, and only the first is the one you would guess:

- Only the strongest aligned column per page was kept. Boxes are now peeled off
  one at a time and offered to the existing split-symbol test.
- **Altium stamps an invisible annotation token beside everything it draws** —
  `PIU1014` (pin 14 of U10), `COU8`, `NLTX2` — placed closer than the gap that
  separates the pieces of a split pin name. Absorbing one turned `PC0` into
  `PC0PIU108`, which no longer reads as a pin. This is what actually hid the
  right-hand column on most of these sheets, and it had already been seen
  without being recognised: the `PIC1301 → PB3` pairing in §1.1 is the same
  token.
- Labels were matched to a part through a dict **keyed by page**, so two boxes
  on one sheet collapsed to whichever was stored last. Ownership is now
  geometric.
- A box only has a gutter on a side it has pin names on. Searching the empty
  side reached across the gap and collected the neighbouring box's labels.

Also: ST's own dash qualifier is now accepted in a pin name (`PA0-WKUP`,
`PC14-OSC32_IN`, and `PB8-BOOT0`, which is where G4 keeps BOOT0), and a part
number with a wildcarded digit (`STM32F7X2RXT`) resolves when exactly one
seeded target fits.

### 1.6 The label gutter is not always one column — FIXED

Net labels were assumed to form one aligned column per side. Where a sheet
draws each label against its own wire the gutter is several near-parallel
columns a few points apart, and only the strongest was kept. One board lost its
**entire I2C2 bus** that way while reporting nothing amiss.

Neighbouring columns are now taken too, but only the words in them that read
like net names on their own — a BGA sheet fills that same space with ball
coordinates (`E10`, `B10`), and those would otherwise bind to whatever row they
sit level with. The strongest column is still trusted wholesale, having already
proved itself as a column.

### 1.7 A firmware contradiction did not count against an offset — FIXED

Widening the gutter made one board *worse*, which exposed an older bug.

Offsets were scored on agreements only; a pin the firmware says **cannot** do
the job counted for nothing. So two fits with equal agreement were separated on
raw coverage, and the fit that paired more labels won even when its extra pairs
were contradictions. That board chose an offset three quarters of a row out,
sliding `SWDIO` onto its neighbour's pin and eight nets onto supply rows.

Contradictions now rank directly, then supply-row landings — below
contradictions, since a sheet can legitimately run a net past a supply pin, but
no schematic wires `FLASH_CS` to `VBAT`. The board reads 19/19 where it read
16/16 before.

The general lesson: **the scorer only counted evidence for, never evidence
against.** Anywhere a fit is chosen by search, check that both are counted.

### 1.8 Half the UART spellings were unclassified — FIXED

Counting what came out of the generator as "no config.h role" across the corpus
made this one impossible to miss: `UART4_TX`/`UART4_RX` unclassified on **21
boards**, `UART1` on 14, `UART3` on 14, `UART2` and `UART5` on 13 each.

Only one of the two orderings was matched. `TX4` and `UART-TX4` put the index
last and were recognised; `UART4_TX` and `USART3_RX` put it first and were not.
Both are ordinary. **Two thirds of the corpus generated a config with no serial
ports in it at all** — while netmap had validated every one of those nets
against the firmware tables and reported them fine. The two halves of the tool
disagreed about what a UART net looks like and nothing compared them.

Smaller cases of the same shape: I2C nets that carry no bus number (`SCL`,
`SDA1` — the bus comes from the pins, so the number was never needed), `LED2`
having no rule at all, `LED-1`/`LED-2` spelled with a dash, and `BEEPER_PIN`
losing its role to a suffix.

---

## 2. Coverage — defines never emitted

Measured by generating configs for three boards and diffing the emitted define
set against the corpus.

| Define | Boards | Note |
|---|---|---|
| `DEFAULT_CURRENT_METER_SCALE` | 61% | ESC-dependent; see §3.2 |
| ~~`RX_PPM_PIN`~~ | 54% | done — no corpus board wires one; see §2.3 |
| ~~`DEFAULT_DSHOT_BURST`~~ | 51% | done, with `TIMUPn_DMA_OPT`; never triggered in the corpus |
| ~~`GYRO_2_*`~~ | 25% | done and **proven**; see §2.2 |
| ~~`ESCSERIAL_PIN`~~ | 18% | done — no corpus board wires one; see §2.3 |
| `DEFAULT_VOLTAGE_METER_SCALE` | 15% | computable; see §3.2 |
| `USE_SDCARD` | 15% | SPI form done; boards use **SDIO/SDMMC**, unimplemented — §2.3 |

`MOTOR5-8`, `UART6+`, `SPI4`, `ADC_RSSI_PIN` are built generically and do work;
across the corpus 18 boards emit `MOTOR5-8` and 16 emit `SPI4`+.

### 2.1 Burst DShot is not supported

`TIMUPn_DMA_OPT` is never emitted, so a DMAMUX board wanting `DSHOT_DMAR_ON`
needs hand editing. The generator always emits `DSHOT_BITBANG_ON`, which is a
safe default but not always the right one — four motors on one timer is the case
burst exists for, and the tool already detects exactly that when choosing a
shared timer.

### 2.2 Dual-gyro boards — DONE, and now proven on hardware sheets

Three corpus boards emit a complete `GYRO_2_*` set (`SPI_INSTANCE`, `CS_PIN`,
`EXTI_PIN`, `ALIGN`, and `CLKIN_PIN` on two of them). This was the roadmap's
single highest-value unknown and it is now closed.

One of the three is a **shipping board whose hand-written config is in the
config repo**, which makes it the strongest verification available anywhere in
this project — the same board, converted independently by a human. Of the
defines both files carry, **52 agree exactly and 3 differ**:

| | hand-written | generated | verdict |
|---|---|---|---|
| `ADC1_DMA_OPT` | 8 | 10 | both free channels; benign |
| `BARO_I2C_INSTANCE` | `I2CDEV_2` | `I2CDEV_1` | generator emits only one I2C bus — see §2.4 |
| `PINIO1_PIN` | *BEC switch* | *camera switch* | generator emits only one PINIO — see §2.5 |

`GYRO_1_*` matches exactly. `GYRO_2_*` is emitted by the generator and **absent
from the hand-written config**, though the sheet carries a full second bus,
chip-select and interrupt for it. Either the shipped config is missing a gyro
the board has, or this sheet is a later revision than the config was written
from. Worth resolving with the vendor; it is not a generator defect.

The refusal path is exercised too. A different dual-gyro board gives its second
IMU only a chip-select, and the tool declines to emit `GYRO_2_*` at all —
correctly, since `GYRO_2_CS_PIN` alone would raise `GYRO_COUNT` to 2 with no bus
behind it — and says exactly which three defines a human must add.

### 2.3 Still unproven after 168 schematics

`USE_SDCARD`, `RX_PPM_PIN`, `ESCSERIAL_PIN` and burst DShot are implemented and
unit-tested, and **not one board in the corpus emits any of them**. That is a
much stronger statement than the old "no board in the corpus exercises them",
and it changes what the gap is:

- **SD card.** 22 boards mention a card, but the ones that wire it do so over
  **SDIO/SDMMC**, not SPI — nets like `SDMMC1-CK`, `SDMMC1-CMD`, `SDMMC1-D0..D3`.
  Only the SPI form is implemented, so `SDIO_CK_PIN` and friends are never
  emitted. This is a missing feature, not an unproven one.
- **PPM and escserial** genuinely do not appear. Both are legacy; the corpus
  suggests they are no longer worth chasing.

### 2.4 Only one I2C bus is emitted — OPEN, and larger than it first looked

A board with I2C1 *and* I2C2 gets one of them, and any device on the other is
attributed to the wrong bus — the shipping-config comparison in §2.2 lands the
baro on `I2CDEV_1` where the hand-written config has `I2CDEV_2`. `netmap`
resolves both buses correctly and firmware-validates every pin; `genconfig`
collapses them, because it keeps a single `i2c` dict of `{scl, sda}` and the
second bus simply overwrites the first.

Measured: **no board in the corpus emits more than one I2C bus** — it is not
rare, it is structurally impossible. 69 emit exactly one and 35 none. The
visible symptom is the note *"net names say I2C2 but the pins are I2C1"*, which
fires on **29 boards**: that is the surviving pins of one bus wearing the
surviving name of the other.

This is now the second-largest coverage gap after the §3.3 tail, and unlike
that one it is a small mechanical change — the same shape as the PINIO fix in
§2.5, which is what it should be modelled on.

### 2.5 Two PINIOs — DONE

Boards routinely have two switched rails (a BEC/VTX switch and a camera
switch). Both are emitted now; 19 corpus boards carry a `PINIO2_PIN`, and on
the one board with a hand-written config to compare, both pins match it.

Two things had to change. A board writing `BEC-SWITCH` had that net dropped
entirely — the rule matched a trailing `_SW`/`_EN` but not the word spelled out
— so only its *camera* PINIO was emitted. And the polarity is not derivable at
all; see §3.2, which records why 129 is emitted as a *default* rather than read
from the sheet, and why `drivers/pinio.h`'s cap of 4 is now enforced.

---

## 3. Capability — needs analysis the tool cannot yet do

### 3.1 Datasheet verification of the firmware tables

**Firmware is the single source of truth.** The generator may only emit what the
build actually honours: a pin Betaflight's tables do not list cannot be used,
whatever the silicon supports. Datasheet data must therefore never enter the
generator's runtime path — doing so would emit configs months before the firmware
could run them, which is precisely the case that came up when a board used a pin
the H5 USART3 table lacked. The right fix there was a firmware PR, not teaching
the generator to bypass firmware.

So the datasheet is a **verification instrument, not an input**. Its output is a
firmware bug report; once merged, `seed_firmware.py` picks the fix up
automatically. One-way loop, no second source of truth:

```
datasheet ──audit──> firmware tables ──seed──> generator ──> config.h
              │                ▲
              └── bug report ──┘
```

Today the tool has no datasheet access, so when firmware rejects a pin it falls
back on the schematic symbol's own AF list as a second opinion — and a symbol can
be wrong. One submission claimed `UART7_TX` on a pin whose datasheet AF row is
empty, while a different pin on the same sheet really did have `USART3_RX` and
the *firmware* was at fault. Only a datasheet separates those two cases.

A working prototype exists (built while auditing the H5 tables): parse the AF
tables out of an ST datasheet by column geometry into `pin -> {function: AF}`.
Validated against 16 pins firmware already had — 14 exact, and the 2 mismatches
were genuine firmware bugs.

**Value beyond this repo:** run across the H5 UART and I2C tables it found three
real defects in 70 pairs — a wrong AF number that made USART10 unusable on both
its pins, and an I2C4 pin with no I2C function at all, copy-pasted from the H7
block. Every other MCU family is unaudited.

#### Revision drift is real, and cheap to re-check

The N6 datasheet moved from DS14791 Rev 6 to Rev 9 mid-flight, after a PR had
already been raised against the older one. Re-running the audit took seconds and
answered it precisely: same seven findings on unmodified master, still clean on
the branch, and the entire delta between revisions was 11 `LCD_*` functions
moving from AF15 to AF14 — nothing touching timers, UART, SPI or I2C.

Two things that made that quick rather than anxious. `--dump-af` writes the
extracted map to JSON, so two revisions can be diffed directly instead of
re-read by eye. And a PR body should cite the revision it was verified against,
because a reviewer downloading "the datasheet" gets whatever is current — the
citation is what makes the claim checkable later.

#### Make it repeatable, not one-shot

It is tempting to treat this as a sweep that ends: audit every family once, fix
what it finds, done. That is not reachable as a steady state.

- The `I2C4_SCL PF14` bug was **introduced** by a later copy-paste, not an
  original error. Any future edit can reintroduce that class of mistake.
- New families keep landing — H5, C5 and N6 are all recent, and each arrives
  hand-written and unaudited.
- Datasheet revisions and errata do occasionally change AF tables.

So it wants to be a cheap re-runnable check with a non-zero exit code, suitable
for CI on any commit touching `src/platform/*/`. The marginal cost over a
one-shot tool is close to nil.

#### Verified findings — G4 and N6

Checked by rendering the datasheet AF tables to images and reading them, not by
re-running the parser. Two scripts sharing the same fragile column geometry
cannot verify each other: a quick independent extraction written for this check
*also* dropped cells (it missed `I2C1_SCL` on a pin whose cell wraps across two
lines), which is exactly why the visual read was necessary.

**STM32G4** — `stm32g474cb.pdf`, Table 13, all five confirmed:

| Firmware | Datasheet | Verdict |
|---|---|---|
| `I2C4_SDA PB7` AF4 | PB7 AF3 = `I2C4_SDA`, **AF4 = `I2C1_SDA`** | wrong AF |
| `I2C1_SCL PB6` AF4 | PB6 has no I2C function at all | wrong pin |
| `I2C4_SCL PB6` AF3 | PB6 has no I2C function at all | wrong pin |
| `I2C3_SCL PA10` AF2 | PA10 AF2 empty; `I2C3_SCL` is **PA8** AF2 | wrong pin |
| `I2C2_SDA PF6` AF4 | PF6 AF4 = `I2C2_`**`SCL`**; `I2C2_SDA` is PF0 AF4 | wrong role |

The PB7 one is the most dangerous of the set. AF4 on that pin *is* a valid I2C
function, just the wrong peripheral — selecting I2C4 SDA there silently routes
I2C1 SDA instead. The others fail to work at all, which is louder.

**STM32N6** — `stm32n657a0.pdf`, Tables 19 and 20, confirmed:

| Firmware | Datasheet | Verdict |
|---|---|---|
| `USART7_TX PG12` AF10 | PG12 **AF8 = `UART7_TX`**; AF10 is empty | wrong AF |
| `TIM15_CH1 PE5` AF4 | PE5 AF4 = `I2C1_SCL` | wrong pin |
| `TIM15_CH2 PE6` AF4 | PE6 AF4 = `I2C1_SDA` | wrong pin |
| `TIM15_CH1N PE4` AF4 | PE4 AF4 empty | wrong pin |

`UART7_RX` sits on PG11 AF8, so the working pair is PG11/PG12 at AF8.

**STM32H5** — `TIM13_CH1N PF8` / `TIM14_CH1N PF9` not yet read visually, but TIM13
and TIM14 are single-channel timers with no complementary output at all, so a
`CH1N` cannot exist on them. The same two rows appear in the H7 block they were
copied from.

**Raised and merged upstream** as #15506 (H5) and #15508 (G4/N6). The H5 `TIM13_CH1N`/`TIM14_CH1N` pair is still only reasoned, not read, so it was not included in either.

Category-2 findings on H7, F7 and C5 are mostly peripherals a *sibling* part has
(UART9/USART10 on H723, I2C4 on F76x, USART6/7 on C591); the report names the
excluded siblings so these are not misread as defects.

#### Datasheet coverage is now complete for every family that is harvested

Locally available under `manufacturers/datasheets/`: **F4** (`stm32f405-407.pdf`,
DS8626), C5, F7, G4, H5, H7, N6. F4 was the blocking gap — roughly 250 of the
619 boards — and is closed; its audit reads 119/120 firmware pairs out of the AF
table and comes back clean apart from `TIM1_CH1N` on `PA11`, which the silicon
has as `TIM1_CH4`, raised as #15510 and still open.

What remains uncovered is AT32 and APM32, and that is not a datasheet problem:
`seed_firmware.py` does not harvest their tables at all (§3.4), so there is
nothing to audit *against*. Getting their datasheets is worth nothing until that
lands.

The principle stands either way: "everything has been verified" cannot be
claimed for a family whose datasheet is missing, and the tool should say so
rather than reporting a clean run.

### 3.2 Circuit-level analysis — the VBAT divider is DONE

Everything used to be net-label and component matching. The highest-value piece
of circuit reading is now implemented, and it did not need a general netlist
either — the same "find the net away from the MCU and look at what is drawn
around it" that solved §3.3.

**`DEFAULT_VOLTAGE_METER_SCALE` — done.** The scale is not a free parameter.
`voltage.c` computes

```
volts = adc * vbatscale * Vref / 10 / (0xFFF * vbatresdivval)
```

which with the shipped `vbatresdivval` of 10 puts full scale at
`0.33 * vbatscale` volts, so for a divider of ratio *r*

```
vbatscale = 10 * (Rtop + Rbottom) / Rbottom
```

exactly. `100K/10K` gives 110 — the firmware default, which is why so many
boards need no define and why any other divider silently misreports the battery.
Read from the code, not from the comment beside it, which describes only the
10k:1k case.

Read **structurally**, not by taking the two nearest values: the ADC net sits at
the midpoint of one vertical leg with a resistor either side. Requiring that
shared leg is what separates the divider from everything else nearby — on two
boards a third resistor from a neighbouring circuit sat 34pt the other side of
the node, and on one the two nearest values would have given 223 instead of 110.
Orientation is anchored by a supply above or ground below (either alone: one
board draws ground as a bare symbol with no text), and `vbatscale` is a
`uint8_t`, so anything over 255 is refused.

Verified against hand-written configs, and the ones that matter are the
non-default boards the roadmap insisted on: `20K/1K` → **210** against a config
that says 210, and `150K/10K` → **160** against a config that says 160. A third
computes 110 from `100K/10K` against a config that leaves it unset. No board
gets a scale that disagrees with its config.

11 boards now carry a computed scale — 8 at 110, 2 at 210, 1 at 160. Widening
the sense-net vocabulary at the same time (`VBAT-ADC1`, `VBATT-ADC`,
`BATT_VOLTAGE`, `BAT1_V`) took `ADC_VBAT_PIN` from 48 boards to 63.

**The limit is the sheets, not the method.** Of the boards with a VBAT node, 23
give the resistor *designators* with no values anywhere — one names the divider
only as an annotated "20：1" — so there is nothing to read. That is worth
stating plainly: this will never reach every board, and where the values are
absent it says so rather than falling back on 110.

- **`BEEPER_INVERTED`** — corroborated, not decided. 554 of the 582 corpus
  configs that drive a beeper set it, so it stays the default; on 10 boards the
  sheet shows the transistor and the note now names it. Its absence is
  deliberately not treated as evidence the other way — a sheet that does not
  draw a transistor has not shown there is none, and a wrong flip is a beeper
  that never sounds.
- **PINIO polarity — not derivable, and the old rule was wrong.** This entry
  used to say the real answer is whether the regulator enable is held up by its
  own divider. That understates it: a PINIO is just a pin the user can toggle,
  vendors assign the indices however they like, shipped configs use both 129 and
  1 across every category of rail, and at least one board inverts in *hardware*
  as well. The net name carries no information about any of it.

  The generator used to infer 129 from the name mentioning VTX and 1 otherwise,
  which was wrong in the common case — of the corpus configs whose PINIO is a
  BEC or power rail, 93% use 129 and that rule gave them 1.

  A value must still be emitted: `drivers/pinio.c` switches only on the mode
  bit, so omitting `PINIOn_CONFIG` never configures the pin as an output and the
  box does nothing. So 129 is emitted on the basis of *how the two fail*, not of
  what the sheet says — `pinioInit` drives an inverted pin high at boot and a
  plain one low, so a switched rail comes up powered with 129 and dead with 1,
  and dead reads as broken hardware rather than an inverted switch. It is
  flagged as a default rather than a reading, on every PINIO.

  Separately, a board writing `BEC-SWITCH` had that net dropped entirely, so
  only one of its two PINIOs was emitted — the camera one. Both are emitted now
  and both match the hand-written config.
- **`DEFAULT_CURRENT_METER_SCALE`** — genuinely ESC-dependent when the FC just
  filters a sense line, but a board with an on-board shunt could be computed.

### 3.3 CS-only devices — mostly SOLVED, and it did not need a netlist

The original entry said this was fixable only by tracing wires, needing the same
netlist work as §3.2. That turned out to be the wrong diagnosis, and it was the
measurement that showed it: 77 cases across 43 boards, but only 13 of them were
on boards with a single bus. The rest had two to four buses and looked genuinely
ambiguous *from the MCU side*.

They are not ambiguous on the sheet. **The bus was never unknown, only written
down somewhere else.** Where a sheet names its buses generically — `SPI2-SCK`
rather than `OSD-SCK` — the association is drawn at the *device*, where the same
net labels appear a second time: the part's SCK/SDI/SDO carry the bus names and
its CS carries the chip-select name, all within a few points of each other. On
one four-bus board each chip-select sits 9–32pt from its own bus's lines and
211pt from the next.

So it is decidable by proximity, with no netlist. `trace_cs_bus()` finds the
chip-select away from the MCU and reads which bus's lines it is sitting among.
Deliberately conservative — the nearest bus must be at least three times nearer
than the runner-up and have two of its three lines in the same cluster — and
where it declines it says which test failed. Checked against hand-written
configs for the three corpus boards that have one: **5 instances resolved, all 5
agree, 4 declined rather than guessed, none wrong.**

Asking why the remainder still failed found the rest of it. 24 sat on boards
where *no* bus had been resolved at all, because two more namings were read as
nothing: `SPI3-FLASH_SCK` / `SPI1-ICM1_MOSI`, which state bus and device at
once and so matched neither rule, and `SCK3` / `MISO3`, the plain bus form with
the index on the other side. That last shape keeps recurring — it is the same
thing as `TX4` versus `UART4_TX` (§1.8), and it is worth assuming both orders
exist for any indexed net.

Crucially, none of this lets a label overrule the firmware. The tracer returns
a bus *label*; the traced device is handed that bus's data pins and
`assign_spi_buses` names the instance from the pins, exactly as for a device
whose own nets were labelled. A sheet that mislabels its own bus still cannot
talk the generator past the map — the same rule that stopped SPI roles being
taken from net names.

A second shape turned up in the tail and is handled too. The rule wanted two of
a bus's three lines clustered round the chip-select, which is what a small part
looks like — but a flash chip is drawn as a large symbol with its CS and one
data line together and the other two coming off the far side, 130pt away. That
is still a third of the way to the next bus, so a second sufficient condition
now applies: *every* one of the winning bus's lines is nearer than anything
belonging to another bus. Where a rival's lines interleave it still declines.

| | before | after |
|---|---|---|
| CS-only devices left to a human | 77 | **27** |
| second-IMU refusals | 16 | **4** |
| `*_SPI_INSTANCE` emitted | 69 | **132** |
| `GYRO_2_*` emitted | 14 | **55** |
| boards whose SPI section needs no hand-editing | 60 | **83** / 104 |

Against the three corpus boards with an exactly-matching hand-written config,
**9 instances agree and none disagree**; the rest are declined with a reason
rather than guessed.

**What is left: 27 cases**, on boards where no bus is resolved for other reasons
or whose chip-select never appears away from the MCU at all, so there is no
second occurrence to read. Those genuinely need wire tracing.

### 3.4 Only STM32 families are harvested — 14 boards blocked

AT32, APM32, PICO and X32 keep their peripheral tables in a different shape.
`seed_firmware.py` skips them, so those boards cannot be converted at all.

**AT32F435 is 14 of the 168 schematics** — the third-largest family in the
corpus after H7 (64) and F7 (20), ahead of G4 and F4. It is the only non-STM32
part that appears at all, so the whole of §3.4 is really just AT32.

### 3.5 The MCU is often not named on the sheet

27 schematics carry no part number anywhere in their text — not written
differently, simply absent. Auto-detection has nothing to match, so the run
stops before the symbol is ever looked at.

This is worth separating from "cannot be converted", because those boards are
otherwise fine. Given `--target` by hand they read **98 pins at 27/27, 96 at
42/42, 62 at 13/13** — all at 100% agreement. The whole loss is the one line of
provenance nobody wrote on the drawing.

Inferring the part from the pin evidence would violate §1 (firmware is the
source of truth, and the MCU identity is what selects the tables), so the
options are to take a hint from the filename or to keep asking for `--target`.
Whichever, the error should say what it looked for.

### 3.6 Unreadable documents are now named, and one class was invisible — DONE

A submission arrived that cannot be converted at all, and the tool blamed the
geometry: first "could not detect FC_TARGET_MCU", then, once told the target,
"no aligned pin-name column". Neither is the problem.

Its fonts are **Type 3 with a custom encoding, no embedded font file and no
ToUnicode map**, and the `/Differences` array names the glyphs `/0 /1 /2 …
/141` — sequential numbers carrying no character information at all, with the
shapes drawn by `CharProcs`. Every glyph is a drawing procedure. `pdftotext`
recovers the internal byte codes instead of the letters, so *Interface Type*
comes back as `RNLC@S?ICÿTFCA`. 1904 words over seven pages, not one of them a
pin name. Only OCR could read it, and that is a dependency this repo will not
take.

The lesson is the reclassification. Naming the two non-parse failures — no text
layer at all, and a text layer that carries no characters — split the 64
schematics that yield nothing into:

| | |
|---|---|
| never name their MCU (read fine with `--target`, §3.5) | 32 |
| scans, no text layer | 16 |
| **text present, fonts unmappable** | **12** |
| text fine, symbol genuinely not found | 3 |
| `pdftotext` crash | 1 |

**Those 12 were already in the corpus and had been counted as geometry
failures.** A whole class of defect was invisible because the diagnostic
described the last thing that failed rather than the first. That is the same
shape as §1.5 and §1.8 — the tool knew, and did not say.

A handful of corpus files are also not schematics at all — wiring diagrams, a
datasheet, calibration notes, one PID paper. They fall into the "never names
its MCU" bucket, which is harmless but not precise.

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

### 4.2 `MANUFACTURER_ID` is not validated — FIXED

The ID must appear in the config repo's `Manufacturers.md`. This was checked by
hand for both boards converted so far. The file is a parseable markdown table —
validate against it and fail loudly on an unregistered ID.

### 4.3 Firmware array limits are invisible — FIXED

`UARTHARDWARE_MAX_PINS` is 5 on H5 and 4 on F4/F7. A pin sweep hit that ceiling
and the generator has no idea the limit exists — it would happily reference a
pin the firmware table has no room for. Seed the limits and check against them.

### 4.4 `genconfig` does not surface which page it read — FIXED

`netmap` now reports `31 pins on page 4 of 5` and exposes `sym.page`,
`sym.pages`, `sym.split`, `sym.ignored_pages` and `describe_pages()`, but
`genconfig` calls `find_symbol()` itself and prints its own header. On a
multi-page submission the generated config gives no indication which sheet was
read, and a rival symbol on another page is never mentioned. Small wiring job.

### 4.5 `config.c` is not supported

The config repo allows an optional `config.c` next to `config.h` for
board-specific init. Not emitted, and not detected when one would be needed.

### 4.6 Provenance is weak — FIXED

The schematic sha256 is recorded, but nothing ties a generated config to the
firmware revision it was validated against. A config generated against a patched
tree looks identical to one generated against release firmware — which already
mattered once, when a board depended on an unmerged pin-table fix. Record the
seeder's firmware rev in the generated header.

---

## Suggested order

1. ~~§1.1 multi-page~~, ~~§1.2 `SYSTEM_HSE_MHZ`~~, ~~§4.1 tests~~ — done
2. ~~§4.2 / §4.3 / §4.4 / §4.6~~, ~~§1.3 / §1.4~~, ~~§2 coverage~~ — done
3. ~~**Validate what is unproven.**~~ — done, by a 168-schematic corpus.
   `GYRO_2_*` is proven three times over and checked against a shipping board's
   hand-written config (§2.2). `RX_PPM`, `ESCSERIAL` and burst DShot are
   confirmed absent from every board in the corpus, which retires them as
   priorities. `USE_SDCARD` turned out to be the wrong shape: boards wire the
   card over SDIO, which is not implemented (§2.3).

   The corpus also found four extraction defects that three boards could never
   have shown (§1.5–§1.8), the largest of which was losing half the pins of
   every split symbol without any diagnostic changing.
4. ~~**§3.1 audit the remaining families**~~ — done, and every family with a
   datasheet on hand now audits clean: F4, F7, G4, H5, H7, C5, N6. The F7/H7/C5 findings
   turned out not to be defects at all — `I2C4` occurs zero times in the F722
   datasheet, `UART9`/`USART10` zero times in H743, `USART7` zero times in C562,
   against 16–53 occurrences of their control peripherals. Those parts simply do
   not have them; the entries are right for the family's other members. The
   audit now says that once per peripheral instead of once per pin.

   **F4 is now covered too** (DS8626 Rev 12, filed as `stm32f405-407.pdf`).
   Its UART/I2C/SPI tables came back clean at 100% extraction, and the one timer
   finding was real: `PA11` was declared `TIM1_CH1N` on both F4 and F7 where the
   silicon has `TIM1_CH4`. Raised as #15510.

   The timer findings have now been read against the rendered tables too.
   H5 and H7 `TIM13_CH1N`/`TIM14_CH1N` on `PF8`/`PF9` were real — those timers
   have one channel and no complementary output, and both datasheets put
   `TIM13_CH1`/`TIM14_CH1` at AF9 there. Raised as #15511. H5 turned out to be
   half-fixed already: its table used `CH1` with a `WAS TIM13_CH1N` note, but the
   superseded AF macros were left behind.

   **N6 is done too, and it was smaller than it looked.** I called it a rewrite;
   it was not. Seven entries named a channel the silicon does not have on that
   pin, and every affected pin already carried its correct entries alongside —
   so it was a removal. `TIM15` in particular is right on ten other pins; the
   errors clustered on four. Raised as #15512, and the N6 audit is now clean at
   239/239 pairs.

   Worth keeping as a lesson: the "shape of a copied block" read came from the
   finding list, not from the table. Looking at the table first would have
   scoped it correctly and sooner.
5. ~~**§3.3 CS-only devices**~~ — largely done, and without the netlist it was
   assumed to need: 77 cases down to 32, and boards whose SPI section needs no
   hand-editing up from 60 to 78 of 104. The bus is read at the device end,
   where the sheet already states it. The 32 that remain are the real §3.2
   cases.
6. **§2.4 second I2C bus** — §2.5 is done; this is the half that is left, and
   it is bigger than it looked: no board emits two I2C buses because the
   generator cannot hold two, and 29 boards visibly show the collision. Small,
   mechanical, and modelled on the PINIO fix.
7. ~~**§3.5 / §3.6 say what actually went wrong.**~~ — done, and it uncovered a
   defect class nobody knew was there: 12 corpus schematics whose fonts carry no
   character mapping had been counted as geometry failures. §3.5 remains as a
   *capability* gap — 32 sheets never name their MCU, and read at 100%
   agreement the moment you pass `--target`.
8. **§3.4 AT32** — 14 boards, the whole of the non-STM32 gap.
9. **§2.3 SDIO** for the SD card, which is how boards actually wire it.
10. **§4.5 `config.c`** — still unsupported.
11. ~~**§3.2 circuit analysis**~~ — the VBAT divider is done and checked on two
    boards whose ratio is *not* 100K/10K, which was the condition this entry
    set. What remains is PINIO polarity and the current-meter scale, both
    smaller and neither blocking.
