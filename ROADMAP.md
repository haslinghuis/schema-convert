# What is still open

Ordered by what would bite first, not by effort. Percentages are the share of
the 619 configs in the Betaflight config repo that use a given define — a proxy
for how often a gap will actually be hit.

**Everything already fixed lives in [`FINDINGS.md`](FINDINGS.md)**, along with
the seven shapes those defects keep taking. The two files share one numbering,
so a `§1.12` reference resolves there and a `§3.4` resolves here. Read the
shapes before adding to either: most of what has gone wrong here has gone wrong
more than once.

## The corpus is the instrument

Nearly every defect in [`FINDINGS.md`](FINDINGS.md) after §1.4 was found by
running **168 vendor schematics** end to end rather than by reading the code.
That is worth stating plainly, because none of those defects were visible on the three boards the tool had been developed
against, and several were invisible *by construction*: they dropped input
silently while every diagnostic the tool prints stayed clean.

The measurements that matter are aggregate, and they are what a change should
be judged against:

| | before the corpus | now |
|---|---|---|
| schematics that yield a pin map | 104 / 168 | 116 / 168 |
| MCU pins read | 3090 | 3865 |
| nets checked against firmware | 1637 | 1920 |
| of those, agreeing | 1601 | 1878 |
| UART pins emitted | 299 | 576 |
| boards with any UART pin | 34 | 70 |
| boards at 100% agreement | 86 | 94 |
| SPI devices needing a hand-set instance | 77 | 4 |
| boards whose SPI section is complete | 60 | 83 |
| boards with a computed `DEFAULT_VOLTAGE_METER_SCALE` | 0 | 11 |
| defines emitted across the corpus | — | 6480 |

The 12 the corpus gained are AT32 (§3.4), and they moved every total above with
them - which is the reason to re-run the whole pass after adding a platform
rather than trusting the per-board numbers it was developed against.

Run it before and after any change to extraction or classification. A change
that improves one board and quietly costs three is the normal failure mode
here, and only the aggregate shows it. Two of the three fixes below looked
correct on their motivating board and were caught this way.

## The index

Sizes are measured against the 168-schematic corpus, not estimated. The detail
is in each section below.

| | | boards |
|---|---|---|
| §3.3 | Chip-select-only devices — the device is known, the bus is not | 33 on 19 |
| §3.3b | Bus-named selects: 24 of 41 now identified from the peripheral's pin names; 17 have nothing identifying drawn at the far end | 17 left |
| §3.5 | Sheets that never name their MCU. Not broken: 100% agreement given `--target` | 34 |
| §3.4 | **AT32** peripheral tables are not harvested, so those boards cannot be converted at all | 17 |
| §1.13 | SPI buses still refused for a missing line | 17 buses on 13 |
| §1.20 | Net labels genuinely lost, once echoes of mapped names stop being counted (§1.24) | 51 |
| §1.14 | The VBAT divider drawn as one horizontal and one vertical resistor is not read | 1+ |
| §3.8 | No golden board for a refused bus, and none at all for C5, N6 or AT32 | — |
| §3.7 | Wire tracing prototyped: settles local structure, does **not** yield a netlist on name-connected sheets | — |
| §3.5 | Three sheets from one vendor name a part that does not exist (`STM32F743`, a typo for H743); detection correctly fails and the harness then forces the wrong family | 3 |
| §4.8 | Timer rate clashes: reported and avoided where a legal alternative exists; 5 boards have none | 5 |
| §4.9 | Pin-editor suggestions: 136 offered on 59 boards; the rest have nothing on the sheet to offer | — |
| §4.10 | No statement of what a complete target contains: three `DEFAULT_*` defaults mean *nothing works* when absent, and absence is silent | 31 boards |
| §4.11 | DMA contention against DShot bitbang: investigated, modelled, and dropped - the model flagged 151 of 467 shipped configs (FINDINGS §4.11) | — |
| §4.12 | The beeper's `TIMER_PIN_MAP` row is never emitted, so a passive buzzer cannot be enabled by CLI at all — DONE (and a claimed 16 broken targets was my own miscount) | 26 boards |
| §4.13 | A sheet that names its ADC instance is not read, and a function the board does not fit cannot be left out — DONE | 1 board |
| §4.14 | (FINDINGS) A scan matched `TIMER_PIN_MAP` rows by macro name where firmware matches by pin tag, inventing 16 defects | — |
| §1.25 | (FINDINGS) A one-column part's side was an artefact of which cluster won; one board read at 40% — DONE | 1 board |
| §1.26 | (FINDINGS) `net_requirement` knew fewer spellings than `classify`, starving the offset scorer — DONE | 2 boards |
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

---

## 2. Coverage — what the sheets carry and the tool did not emit

### 2.1 Burst DShot is not supported

`TIMUPn_DMA_OPT` is never emitted, so a DMAMUX board wanting `DSHOT_DMAR_ON`
needs hand editing. The generator always emits `DSHOT_BITBANG_ON`, which is a
safe default but not always the right one — four motors on one timer is the case
burst exists for, and the tool already detects exactly that when choosing a
shared timer.

---

## 3. Capability — what needed analysis the tool could not yet do

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

#### Round four: distance was still the wrong measure for a wide part

The "every line nearer than the next bus" condition holds only while the sheet
is drawn loosely. A revision of one F722 sheet put the flash's CS and SDI at the
left of the symbol and its SCK and SDO **206pt away** at the right — and the OSD,
on another row entirely, has its bus 198pt from that same CS. This part's own far
side is *further* than the neighbouring part's lines, so both tests fail and a
board whose sheet states the bus three times over gets left to a reviewer.

What separates them is not distance but **the row**. A symbol's pins fan out left
and right along its own rows, so every label belonging to this part shares the
chip-select's band and the neighbouring part's are two hundred points up the page.
`trace_cs_bus()` now counts a line as clustered when it is within the radius *or*
on the chip-select's row with no other bus's labels in the span between them — the
clear-span test being what stops a row that happens to run past a second part from
collecting it.

| | before | after |
|---|---|---|
| CS-only devices left to a human | 34 | **29** |
| `*_SPI_INSTANCE` emitted | 186 | **191** |
| `DEFAULT_BLACKBOX_DEVICE` emitted | 43 | **47** |

Four of the five gained boards get their blackbox default for free, since §4.10
already ties that to a device the config can actually reach.

#### Round five: the tail was never a tracing problem

29 chip selects were still unresolved, and asking *which* refusal they hit gave
the answer at once: **none of them.** Every one reported "only a CS net on this
sheet", the message for a chip select that is never drawn a second time — and on
the boards themselves it plainly was. One airbot H7 draws `FLASHCS` twice, the
second time 22pt from `SPI1MISO` and 29pt from `SPI1CLK`, which is as clear as
the evidence ever gets.

The tracer could not see those labels. It matched bus lines with a regex of its
own — `SPI(\d)[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)` — which wants a separator and has
never heard of `SPI1CLK`, `SCK3` or `SPI_SCK2`, all spellings `classify()`
learned in earlier rounds of this same section. So a board whose *MCU side*
resolved four buses could still report that its flash had no bus to join, from
the same labels at the other end of the same wires.

**§1.18 exactly**, a third time: the vocabulary that collects drifting from the
one that classifies. The fix is the one that stays fixed — the tracer now calls
`classify()`, the regex is deleted, and a test asserts that every spelling
`classify` reads as an SPI line is one the tracer can trace.

| | before | after |
|---|---|---|
| CS-only devices left to a human | 29 | **5** |
| `*_SPI_INSTANCE` emitted | 191 | **217** |
| `DEFAULT_BLACKBOX_DEVICE` emitted | 47 | **54** |
| warnings, corpus-wide | 978 | **945** |

Checked for the failure mode that matters, since a wrong bus is worse than
none: comparing every emitted instance value across the corpus before and
after, **26 added, 0 changed, 0 removed.** Recognising more labels moved no
board's existing decision.

#### Round six: the last four are refusals worth keeping

Of the five left, one was resolvable and not by tracing. A G473 sheet names its
flash bus after the device — `FLASH_CLK`, `FLASH_DO`, `FLASH_DI` on PA5/6/7 —
and `classify()` knew `FLASH_SCK`/`FLASH_MISO`/`FLASH_MOSI` but neither the bare
`CLK` nor a chip's own `DO`/`DI`. The three nets fell out as "no config.h role",
so the flash had nothing but a chip select and the *gyro* took SPI1 by
elimination.

Widening the device-named rules to `CLK`, `DO`/`DOUT`, `DI`/`DIN` reads them.
`DO` is the device's output and so the MCU's input, which is a direction worth
getting right and not worth trusting: the pin still has to support the role in
the firmware map, the same guard that stops a vendor's `SPI1_MOSI` label putting
two SDIs on one bus.

Deliberately **not** on the SD card. `SD_CLK` is the SDMMC card clock, and a
card is the one device on these sheets commonly wired either way — the existing
test for that caught the widened alternation the moment it was made.

That board now emits `FLASH_SPI_INSTANCE SPI1`, and its gyro moves to **SPI3**,
where its own `Gyro_SCK`/`Gyro_MISO`/`Gyro_MOSI` on PB3/4/5 have been all along.
It is the one changed instance in the corpus and it is a correction.

The remaining **4** are refusals that should stay refusals. Their device end
names the net differently from the MCU end — `MPU_CS` at the pin and `MPU_CS1`
/ `MPU_CS2` at two IMUs, or `IMU1_CS` against `IMU_CS` — so there is no
identity to follow, only a resemblance. One is a baro whose chip select is drawn
exactly once on the whole sheet. Guessing there would buy four boards and cost
the property that makes the other 220 instances worth having.

| | round three | now |
|---|---|---|
| CS-only devices left to a human | 77 | **4** |
| `*_SPI_INSTANCE` emitted | 69 | **221** |
| `DEFAULT_BLACKBOX_DEVICE` emitted | — | **57** |

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

#### Round three, and the tail is mostly not a tracer problem

Chasing the remainder found two more coverage gaps and one thing not to do.

**A third index position.** `SPI2_SCK` and `SCK2` were matched; `SPI_SCK2` was
not, and neither was `GYRO_MISO1` — the device index *after* the role, which the
chip-select rule has accepted on both sides all along. Boards written that way
resolved **no SPI bus at all**, so every device on them was unplaceable. This is
the third time assuming one index position has cost coverage (§1.8 `TX4` vs
`UART4_TX`, then `SCK3` vs `SPI3_SCK`). **Assume both, for any indexed net.**

**The exporter's own net tokens.** Altium stamps one for every net it draws — NL
for a net label, PO for a port, CO/PI for the component and pin it lands on —
carrying the net's name with separators replaced by `0`, so `RX8_1` becomes
`CORX801` and `TX3` on pin 01 becomes `PITX301`. They read like plausible net
names and were being collected as labels; on one board four of them landed on a
single pin row. They cannot be spotted by prefix, because `PINIO1` is a real net
that starts with `PI` — what identifies one is that its remainder names a net
drawn elsewhere *on the same sheet*. 119 across 21 boards, gone.

**What not to do.** Twelve of the remaining cases are boards where exactly one
bus is resolved, which looks like a sound inference: the device has nowhere else
to go. It is not. On the one such board with a hand-written config to check
against, that config has **three** buses with the gyro on SPI1, while the tool
recognises only SPI2 — so the inference would have put it on the wrong bus. One
bus is resolved because one was *recognised*, not because the board has one.
The corpus falsified it before it was written, which is the whole point of
having the corpus.

#### The recognition half, closed

The single-bus shape was indeed a recognition gap, and naming the spellings it
was missing closed most of it.

Vendors copy **Betaflight's own define names** onto their nets, which puts the
index between separators: `GYRO_1_CS`, `GYRO_1_CLKIN`, `GYRO_1_EXTI`,
`MAX7456_SPI_CS`, `FLASH_SPI_CS`. The patterns allowed `GYRO1-CS` but not
`GYRO_1_CS`, so a board written that way lost every device it named.

Two more bus spellings, both of which left a board with **no SPI bus at all**
and therefore nothing placeable on one: `SPI1CLK`, with no separator between
bus and role, and `1-SCK`, where the bus digit stands for the whole name. `CLK`
there is the *SPI* clock, not the SDMMC one, so the normalisation is now
role-aware — one rule for both would have put a card's clock on a bus.

One board went from zero buses to all three, with all nine bus pins and all
three chip-select pins matching its hand-written config exactly. Another
resolved its gyro to SPI1 and its OSD to SPI2 where neither was placed before.

Buses resolved 188 → 196, `*_CS_PIN` 150 → 160, `*_SPI_INSTANCE` 134 → 144,
`GYRO_2_*` 58 → 66, over ten boards with none losing anything.

**What is left: 26 cases**, and the number staying still is worth reading
correctly — the newly recognised boards gain their device chip-selects as well
as their buses, so resolving some and discovering others cancels out on that one
count while the configs get substantially more complete. What remains is boards
whose chip-select is never drawn away from the MCU, which genuinely needs wire
tracing, and a handful whose bus nets are on sheets the symbol is not on.

A related defect, measured and left: on **2 boards (10 pins)** a pin carries two
classified nets, because widening the label gutter (§1.6) lets several columns
compete for one row. The firmware check already discards the wrong half wherever
the net is checkable, which is why this has not surfaced as a wrong pin. Fixing
it properly means matching labels to rows as an assignment problem rather than
independently, which is not worth it for two boards.

### 3.4 Only STM32 families were harvested — AT32 is now DONE

AT32, APM32, PICO and X32 keep their peripheral tables in a different shape.
`seed_firmware.py` skipped them, so those boards could not be converted at all.

**AT32F435 is 14 of the 168 schematics** — the third-largest family in the
corpus after H7 (64) and F7 (20), ahead of G4 and F4. It is the only non-STM32
part that appears at all, so the whole of §3.4 was really just AT32.

"A different shape" turned out to be wrong, which is why this was cheaper than
it looked. AT32's port kept Betaflight's own table layouts: `fullTimerHardware[]`
with `DEF_TIM`, `.rxPins`/`.txPins` of `DEFIO_TAG_E`, `adcTagMap[]`,
`i2cHardware[]` with `I2CPINDEF` — and its SPI pins are in the *same file* the
seeder already read, behind `#ifdef AT32F4`. What differs is the directory, the
file names (`at32f43x` rather than `stm32f7xx`) and the timer's spelling: AT32
calls them `TMR2`, not `TIM2`. So the parsers did not need rewriting, only
pointing.

Kept as written rather than normalised to `TIM`. The name only labels the
channel here; what a `config.h` carries is the occurrence index.

The array limits are per platform and had to become so: `MAX_TIMER_DMA_OPTIONS`
is **22** on AT32 against 3 on F7, so reading STM32's copy would have bounded the
tables by a number that part never had.

**Detection needed one rule.** Betaflight names its AT32 targets after the flash
size, not the package: `AT32F435CGU7` (48-pin, 1024K), `AT32F435RGT7` (64-pin,
1024K) and `AT32F435ZMT7` (144-pin, 4032K) build as `AT32F435G`, `AT32F435G` and
`AT32F435M`. So the letter that selects the target is the *second* after the
family, and the first says only how many pins are bonded out - which the sheet
settles by what it draws.

| | |
|---|---|
| boards that produced nothing and now convert | **12** |
| their agreement | 90–100% |
| defines each | 31–71 |
| built against betaflight master | **3 of 3** (`AT32F435G` ×2, `AT32F435M`) |

No STM32 target's tables changed - the seed diff is two added targets and
nothing else - and the corpus is unmoved at 5841 defines and 919 warnings.

**Verified, and one thing not verified.** The tables were spot-checked against
the source (`PA0` → `TMR2_CH1`/`TMR5_CH1`, `PB0` → `TMR1_CH2N`/`TMR3_CH3`/
`TMR8_CH2N`, `PA9` → UART1 tx *and* I2C1 scl), and the DMA harvest matches the
STM32 DMAMUX families exactly: no per-resource tables, a shared channel list of
**14** - `DMA1` channels 1-7 and `DMA2` 1-7 - against H743's 16. Note that
`MAX_TIMER_DMA_OPTIONS` claims 22 there while the array it indexes holds 14, so
the lower ceiling binds; the generator already takes the minimum and says which
one ran out.

What cannot be verified is the table itself. There is no Artery datasheet in
`../manufacturers/datasheets/`, so `afaudit.py` has nothing to check AT32
against and the datasheet → audit → firmware PR → reseed loop does not close for
it. Every STM32 family here has been audited to 0 defects; AT32 is trusted
exactly as far as Betaflight's own port is, and that difference is worth
remembering before filing anything upstream from it.

APM32, PICO and X32 remain unharvested, and none of them appears in the corpus.

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

**The near-miss half is now decided by evidence, and the no-marking half is
not - which the evidence itself settled.** Where the sheet names a part one
character from a seeded one, each candidate is resolved and the message carries
how much of the wiring it accounts for:

> The sheet names STM32F743 ... this is how much of the wiring each one accounts
> for: **STM32H743 22/22, STM32F745 20/22**.

Still reported rather than chosen: H743 and H750 are pin-compatible and fit
identically while differing in flash size, and picking on a tie would be
alphabetical order dressed as evidence.

Ranking *all* seeded targets for the 32 sheets that name nothing was tried and
does not work. It reaches a strong fit on 12 of them - and with **9 to 19
candidates tied at the same score**, because the STM32 families share pin
functions extensively. `AXISFLYINGF745AIO` fits F722 at 45/45, along with ten
others. That narrows the answer to a family group and cannot pick within it, so
nothing is offered: a short list would be honest, a choice would not.

### 3.7 Reading the drawn wires — PROTOTYPED, and it does not do what I claimed

The standing weakness is that connectivity is inferred from proximity: "GYRO_1_CS
is drawn among SPI1's lines, 3pt to the nearest, so the device shares that bus."
The wires themselves are in the PDF. `pdftocairo -svg` — same poppler package,
no new dependency — returns them as stroked paths, and a prototype
(`wires.py`/`trace.py`, kept out of the repo) turns them into a net graph.

It works. On one H7 sheet: 2459 stroked straight segments; dropping the 15
longest removes the page frame and title-block dividers, which otherwise touch
everything and fuse 263 nodes into one net. What is left is 528 nets, largest 89.
Of 46 label-to-pin bindings that could be checked, 20 sit on the pin's own net
and 20 are separated by a component drawn in that row — a 100R series resistor,
which is a real electrical fact, not a parse failure. Six are unexplained.

It settles §1.14's pull-up case structurally rather than by distance:
`GYRO_1_EXTI` and `MAX7456_SPI_CS` are on the *same* net as their pins while
`3V3_MCU` is on a different one, across the 10k. That is the discrimination the
nearest-label rule has to approximate, and it is exactly the evidence the
`ADC1_8`-versus-`RSSI` case needs.

**But it does not close §3.3, which is why the prototype was worth running.**
Every signal net name on that sheet appears exactly twice — once at the MCU, once
at the peripheral. `SDCARD_SPI_CS` at (240,154) and again at (516,216), beside
`SPI3_SCK` at (516,219). The vendor connects blocks by *net name*, not by a drawn
wire, so there is no wire from the MCU to the SD socket to trace. Tracing the
SPI3 pins reaches `SPI3_SCK` and `SPI3_SDO` and stops. What actually answers
"which bus" on this board is the peripheral-end labels being drawn together —
which is what `trace_cs_bus()` already reads.

**Landing it against §1.14's `ADC1_8` case was then tried, and it failed too** -
which is the whole value of having run it. On that board the MCU symbol has no
drawn stub at its edge at all: the nearest segment is 70pt away, so "which
label is on the pin's net" has no answer. The fallback test - a net label is
drawn on its wire, an annotation is not - does not separate them either:
`ADC1_8` is 1.95pt from a wire and `RSSI` is 2.03pt. Both are on wires.

What did settle it needed no geometry: the firmware tables give `PC5` as
`devices: '12', channel: '8'`, and `ADC1_8` says exactly that, so the label
restates the pin and cannot be its net. See §1.14. **Two failed geometric
attempts pointed straight at a non-geometric answer**, which is the argument for
running the experiment rather than reasoning about it.

The attempt also turned up a defect in the measuring instrument. That board is
converted by the corpus harness as an **STM32F722**. Its sheet reads
`STM32F743VIH6` - a part that does not exist, a vendor typo for STM32**H**743 -
so detection fails and the harness's family guess forces F7. Every corpus number
recorded for it, including the "lost ADC_RSSI_PIN" that started this, was
measured against the wrong MCU. Two of the 104 readable boards are forced this
way; both are that vendor's, both the same typo.

So: a corroborating signal for *local* structure, not a replacement for the
label pipeline, and not a route to a netlist on sheets drawn this way. Two prototype details to carry over: the text-to-net probe radius
must be smaller than the row pitch (2.88pt on this board, and a 6pt probe put
`SWCLK` on `PA15`), and junction dots are not yet told from plain crossings —
the endpoint-on-segment rule does not merge crossings, but it will also miss a
dotted one.

### 3.8 The golden boards did not cover the families the corpus does — PART DONE

A device was emitted pointing at an SPI bus the generator had, in the same run,
refused to emit and said so. The invariant was not missed: `analysis.instances_
without_pins` has been checked on every fixture board since the suite was
written. It passed — because none of the five boards under test had a refused
bus, while six instances dangled across five corpus boards.

This is CLAUDE.md §3 again, one level up. §3 says to build every board in the
corpus rather than the one being delivered; the same applies to the fixtures.
Five boards covering F4, F7, G4 and H5 is a thin sample of 104 readable ones,
and it was thinnest exactly where the corpus is thickest — there was no H7
fixture at all, so §1.12, a defect affecting 21 H7 boards, could not have been
caught by the suite either.

Done: an H7 and a second F4 added (`h7-dual-pad`, `f4-column-drift`), each
verified to fail six tests when its fix is reverted and to leave every other
board untouched. `FROZEN_TARGETS` grew an H743, and the test asserting which
families the frozen firmware carries now derives the list from the boards
instead of repeating it by hand.

Still open: no fixture exercises a *refused* bus, so the guard added with §1.12
is covered only by the corpus. Adding a board that legitimately cannot resolve
one would pin it. Beyond that the sample is still five families out of the
eight the corpus contains — C5, N6 and AT32 have no golden board.

The general point, which is cheap and worth repeating: **an invariant that has
never been observed to fail on the fixtures is not being tested by them.** Both
of these were found by running the corpus, not the suite.

---

---

## 4. Engineering

### 4.5 `config.c` is not supported

The config repo allows an optional `config.c` next to `config.h` for
board-specific init. Not emitted, and not detected when one would be needed.

### 4.8 Timer allocation is not verified against what each function needs — DONE

`timerConfigure(timHw, period, hz)` sets the period and prescaler of a **whole
TIM unit**, not of one channel. So two functions on one unit must want the same
rate, and they do not:

| function | rate | where |
|---|---|---|
| LED strip | 800 kHz carrier | `WS2811_CARRIER_HZ`, `light_ws2811strip.h` |
| servo | 50 Hz default, 50–498 range | `servos.c`, `servo_pwm_rate` |
| motor (DShot DMA) | protocol rate | `dshot_dpwm.c` |

**Firmware does not catch it.** `timerAllocate()` refuses only when *that pin's*
entry already has an owner; ownership is per pin, not per TIM unit. Two pins on
one timer both allocate, and whichever configures last wins the period. This is
the §2 shape exactly: it builds, and it does not work.

**It is latent rather than always broken**, which is why it has survived. On
everything but F4, `DEFAULT_DSHOT_BITBANG` defaults to `DSHOT_BITBANG_AUTO`, and
`isDshotBitbangActive()` makes AUTO mean *on* unless the protocol is PROSHOT1000
— bitbang drives GPIO from DMA and never touches the motor timers, so a motor
sharing a unit with the LED strip costs nothing. On **F4 and APM32F4** the same
AUTO means bitbang only when DShot telemetry is on. So the clash bites on F4
without telemetry, and on any board where the user sets `dshot_bitbang = OFF`.

**How often it happens** — measured, both ways:

| | boards | with two rate classes on one TIM |
|---|---|---|
| generated here | 104 | **12 (11.5%)** |
| hand-written, seeded MCUs | 373 | 6 (1.6%) |

Ours: `ledstrip+motor` ×8, `camera+clkin+ledstrip` ×3, `clkin+motor` ×2,
`camera+ledstrip` ×1. The convention is real — 98.4% of shipped targets keep the
classes apart — and we break it seven times as often, which says most of ours
are a choice the tool made rather than how the board is wired: where a pin
carries several timer channels, the picker takes one without considering what
else landed on that unit.

**Both parts are done.**

*Report it.* `timer_rate_clashes()` groups the picks by TIM unit and warns when
one carries more than one class, naming the functions, what each wants of the
timer, and the bitbang caveat where a motor is involved. The caveat is
family-aware, because the same clash is latent on H7 and live on F4 without
DShot telemetry, and a warning that cries wolf on the common case stops being
read. `analysis.timer_rate_class_clashes` records it per board, resolving each
occurrence back through the firmware table rather than trusting genconfig's
bookkeeping. It found one on a golden board immediately - `h5-rev-b` had its LED
strip and its gyro CLKIN on TIM3.

*Avoid it*, scoped by what the report then measured. The pin is fixed by the
sheet, so the only freedom is which of that pin's channels to use, and that
decides the unit. `avoid_rate_clashes()` re-picks one row at a time and **never
a motor**: motors are grouped onto one timer deliberately, so moving one to
dodge a clash would break the grouping to fix a symptom. What landed on top of
them moves instead.

| | |
|---|---|
| rate clashes | **16 → 5** |
| rows moved | 14, all LED strip or camera control |
| defines | unchanged - a re-pick, not an addition |

Two things it fixed that were not the target. **Same-channel collisions went
12 → 2** - two pins on one compare register is usually a board's LED strip
sitting on a motor's channel, so moving one fixes both. And DMA contention
*improved*, "every option already taken" going 12 → 9, because the strip leaving
the motors' unit freed the stream it was contending for. DMA was the risk I
flagged in moving rows on fixed-mapping parts; it went the other way.

The 5 that remain are the structurally stuck boards the count identified, where
no legal assignment exists - a board whose only LED-strip pin shares a unit with
its only motor pin has no fix, and the warning is the honest output.
`h5-rev-b` now records `[]`.

### 4.9 The pin editor has no suggestions — DONE

§4.7 gives a box per absent function. It cannot say what to put in it, and the
data to narrow that is already in hand.

**What the capability map can do.** On one H743 board with 15 unclassified nets,
filtering by what the firmware says each pin supports:

| function | candidates from the sheet's own nets |
|---|---|
| `ADC_VBAT`, `ADC_CURR` | 7 of 15 |
| `LED_STRIP` (needs a timer) | 7 of 15 |
| `LED0`, `GYRO_1_CS`, `GYRO_1_EXTI` | **15 of 15 — no filter at all** |

That last row is the honest limit: a chip select, an interrupt and an LED are
any GPIO, so capability filtering says nothing about them. Advertising a
suggestion there would be a list of every free pin wearing a confident hat.

**The better signal is the sheet's own unclassified nets**, ranked by name.
The same board offers `LED_TRIP(PD12)` for `LED_STRIP` — one character out, and
`PD12` is `TIM4_CH1` — `CURR_DET(PC1)` for `ADC_CURR`, and `ST_LED(PC2)` for
`LED0`. Those are the answers, and they are already printed in the "nets with no
config.h role" line; nothing joins them up. Suggesting from what the board
actually draws is also the only kind of suggestion that is not an invention:
of 41 pins on that MCU that could carry `MOTOR6`, 31 carry no net on the sheet
at all, and offering one of those is proposing hardware that is not there.

**Shape:**

- Hard filter by the firmware map, exactly as `--set` already validates. A
  suggestion that would be refused on submission is worse than none.
- Rank by token overlap between the net name and the function name, so
  `LED_TRIP`/`LED_STRIP` and `CURR_DET`/`ADC_CURR` come first.
- Mark the provenance in the UI: *"`PD12` carries `LED_TRIP`, which this tool
  did not recognise"* is a suggestion; *"`PH13` is free and can do TIM8_CH1"* is
  a different and much weaker claim, and the two must not look alike.
- Reject a suggestion that would create a §4.8 clash, which is what ties the two
  together: a `LED_STRIP` candidate on a unit already carrying motors is not a
  good suggestion however well its name matches.

Worth doing in that order: §4.8's grouping is what §4.9 needs to rank safely.

#### Done, in that order

`suggest_for_absent()` applies two filters and a ranking, in that order of
authority. **Capability** is a hard filter from the firmware map, exactly as
`--set` is validated - a suggestion that would be refused on submission is worse
than none. **Only the sheet's own nets** are candidates: of 41 pins that could
carry `MOTOR6` on one H743, 31 carried no net at all, and offering those is
proposing hardware that is not there. **The name** ranks what survives, on
shared words plus character similarity.

The three answers this section predicted all come out on the board it was
written from: `LED_STRIP` ← `PD12 LED_TRIP` (one character out, and `PD12` is
`TIM4_CH1`), `ADC_CURR` ← `PC1 CURR_DET`, `LED0` ← `PC2 ST_LED`.

Corpus: **136 suggestions across 59 boards**, defines unchanged - they are notes
and `meta.suggestions`, never an emission. Some catch vendor typos outright:
`GYRO_1_EXTI` ← `PE9 SPI1_EXIT`.

The editor shows them as clickable pins under each empty box, labelled with the
*net* - `PD12 (LED_TRIP)`. That distinction is the point: "PD12 carries
LED_TRIP" is a suggestion, "PD12 is free" is a much weaker claim wearing the
same clothes, and the UI must not let them look alike.

---

### 4.10 There is no statement of what a complete target contains — DONE

A generated target is judged by what it *has*. Nothing states what a normal one
carries, so a define that is simply absent reads the same as a define that does
not apply — and for three of them the difference is whether the board works.

| define | emitted here | hand-written | firmware default when absent |
|---|---|---|---|
| `DEFAULT_VOLTAGE_METER_SOURCE` | 73 of 104 (70%) | 583 of 619 (94%) | `VOLTAGE_METER_NONE` |
| `DEFAULT_CURRENT_METER_SOURCE` | 59 (57%) | 561 (90%) | `VIRTUAL` / `MSP` / `NONE`, by build options |
| `DEFAULT_BLACKBOX_DEVICE` | 79 (76%) | 537 (86%) | `BLACKBOX_DEVICE_NONE` |

`battery.c` and `blackbox.c` fall back to *nothing*, not to something sensible.
So a board whose divider the reader could not follow does not get a slightly
worse config — it gets one with no battery voltage, no low-voltage warning, and
no logging, and nothing about it looks wrong.

**The conditional emission is correct and should stay.** Each of these is tied
to having found the pin or the device, and that is the right coupling:
`VOLTAGE_METER_ADC` with no `ADC_VBAT_PIN` leaves `adcConfig->vbat.enabled`
false, so the meter reads zero rather than reporting nothing. Asserting the
source without the pin would be the "looks finished" failure. The gap is not the
rule, it is the 31 boards where the pin was not found.

**Done, as a checklist rather than a template of values.** Three parts:

- `CONSEQUENCE` gives the cost of each absence, printed under the list of what
  the sheet did not produce - *"without ADC_VBAT: no battery voltage and no
  low-voltage warning"*. Grouped by consequence, because four missing motors is
  one fact and four near-identical lines is how a warning stops being read: 278
  lines became 156, about two a board.
- The absent set is emitted as a **commented block at the end of the file**, so
  the gap travels with the config rather than only in a report. Commented,
  because every one is a value the sheet did not yield and filling it in would
  be the invention this tool exists to avoid. Defines are unchanged at 5704 -
  checked, since a comment block that alters output would be a bug.
- `analysis.defaults_without_backing` asserts the reverse on every fixture: a
  `DEFAULT_*` naming a peripheral that was never configured.

#### The check was too weak, and the answer was 44 boards

`BLACKBOX_DEVICE_FLASH` required only `USE_FLASH`, which says the driver is
compiled in and nothing about the chip being reachable. Found by reviewing a PR
generated by this tool: it shipped `FLASH_CS_PIN`, `USE_FLASH` and
`BLACKBOX_DEVICE_FLASH` with no instance, so `pg/flash.c` left it NULL and
logging was dead on arrival.

Strengthened to require an instance too, and it reported **44 of 104 boards** in
that state - in three degrees:

| | |
|---|---|
| only the chip was detected - no CS, no bus | 24 |
| CS known, bus unresolved | 13 |
| SD card with no instance | 7 |

The emission keyed on the *part marking*, so a `W25Q` silkscreen was enough to
default blackbox to a chip that might not even be fitted. It now requires a
resolved bus, and says so when it refuses:

```
WARN: DEFAULT_BLACKBOX_DEVICE is not set: a flash chip is on the sheet but
      nothing here can reach it - no bus instance was resolved, so the device
      would never open.
```

**-36 defines on 36 boards, and the unbacked count is now zero.** Firmware's own
fallback is `BLACKBOX_DEVICE_NONE`, so the outcome is identical and the config
stops asserting a feature it cannot deliver. The 8 remaining are SDIO cards,
which need no SPI instance and are correctly kept.

The vendor config that prompted this had `DEFAULT_BLACKBOX_DEVICE
BLACKBOX_DEVICE_SDCARD` where the tool produced nothing, because the card's chip
select was named after its bus (§3.3b) - so the device was never placed and the
default silently became `NONE`. That is the shape of it: an upstream miss, and
then a functional loss with no warning attached.

### 4.12 The beeper row is the half that cannot be added later — DONE

`BEEPER_PWM_HZ` is a CLI-settable value; the `TIMER_PIN_MAP` row is not. And
`beeperPwmInit()` calls `timerAllocate()`, which searches **only**
`timerIOConfig` — the mapping compiled into the target. So a config that omits
the row has decided permanently that the board cannot drive a passive buzzer:
setting the frequency then gets no timer, and because `beeperInit()` takes the
GPIO path only while the frequency is zero, the beeper stops working altogether
rather than falling back to it.

~~**16 shipped targets are in exactly that state**~~ — **wrong, and worth
recording.** Sixteen targets do set `BEEPER_PWM_HZ`, and my scan found no
`TIMER_PIN_MAP` row for `BEEPER_PIN` in them, so it looked like sixteen silent
beepers to report. Both halves of that were a matching error:

- 14 of them **do** have the row. They write the pin directly —
  `TIMER_PIN_MAP( 7, PB4, 1, -1)` — and the scan matched only the macro name.
  Firmware matches by `ioTag`, so both spellings work and only one of them was
  being counted. The corrected figures: **50** of 592 targets with a beeper
  carry the row, 16 set the frequency, **14 have both**, 36 have a row and no
  frequency.
- The remaining 2 are RP2350B, and `pwm_beeper_pico.c` does not call
  `timerAllocate()` at all — it takes a PWM slice straight from the GPIO
  number. The row is an STM32 requirement and those targets are correct
  without it.

So the defect count is **zero** and nothing was filed. The firmware fact the
section rests on is unchanged — on STM32 `timerAllocate()` searches only
`timerIOConfig`, so the row cannot be added later by CLI — but "16 boards are
broken" was a claim about the world, and the world was not asked properly. Same
shape as FINDINGS §4.11, one file earlier in the same session.

So the row is emitted wherever the beeper pin has a timer: 26 of the corpus's
boards. It costs nothing on the active buzzer nearly every board fits, since an
unallocated channel takes no period from anyone — which is also why the rate
clash and channel collision checks now ignore it unless `BEEPER_PWM_HZ` is
present. Reporting a clash that exists only after a CLI change would put a
warning on most configs and teach a reader to skip them.

10 boards get **no** row, because every timer channel their beeper pin has is
already driving something else. There the note says so: an inert row that would
collide the moment it were used is worse than no row, since a reader cannot see
that it is inert.

**The exemption belongs in one of the two checks, not both.** Skipping the inert
row in `avoid_rate_clashes()` as well left camera control and the beeper sharing
TIM4 on the board this came from, and a reviewer sent that back on the rule the
occurrence column exists for: each function gets a timer of its own. Warning
about a clash that appears only after someone sets `BEEPER_PWM_HZ` is noise on
nearly every config; *moving* a function off it costs nothing. So the row counts
for the mover and not for the report - 3 more moves across the corpus, no new
warnings, and camera control back on TIM10 where the hand-written config puts
it.

### 4.13 The sheet can name the ADC instance, and a drawn net can be wrong — DONE

Two halves of one board's problem, and neither is a parse failure.

**The instance.** `PC0` and `PC1` can be sampled by any of ADC1/2/3, so the pin
map does not prefer one and `choose_adc()` takes the lowest. A vendor sheet
revised during review said `ADC Voltage (VBAT)  ADC3  PC1` in its summary table
— an intent the pins cannot express. `read_adc_instance()` reads it, validated
like any other vendor annotation: the instance has to be one *every* emitted ADC
pin can be read by, because `adcInit()` uses a single device and
`adcVerifyPin()` silently drops the pins it cannot reach. **None** of the 104
readable corpus sheets carries such an annotation; this one acquired it in a
revision, which is the argument for reading it rather than for expecting it.

**The net the board does not fit.** That same sheet still draws `ADC_RSSI` on
`PC4`, wire and all, unchanged from the previous revision — while the board has
dropped RSSI. Nothing distinguishes a live net from a stale one: a label with a
wire under it is all the evidence there is either way. `--drop NAME` states it,
routed through `classify` like `--set` so both understand the same spellings.

The two interact, which is why they are one entry. `PC4` is the one ADC pin on
that MCU that ADC3 cannot read, so keeping a function nobody fits forced the
whole ADC onto ADC1 and made the sheet's own annotation unhonourable. With
`--drop ADC_RSSI` the annotation validates and `ADC_INSTANCE ADC3` follows.

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
6. ~~**§2.4 second I2C bus**~~ — done, both halves: every bus is emitted, and
   each device is attributed to its own by the nets drawn around it.
7. ~~**§3.5 / §3.6 say what actually went wrong.**~~ — done, and it uncovered a
   defect class nobody knew was there: 12 corpus schematics whose fonts carry no
   character mapping had been counted as geometry failures. §3.5 remains as a
   *capability* gap — 32 sheets never name their MCU, and read at 100%
   agreement the moment you pass `--target`.
8. **§3.4 AT32** — 14 boards, the whole of the non-STM32 gap.
9. ~~**§2.3 SDIO**~~ — done; the seeder had to learn which targets honour
   SDIO_*_PIN at all, because most do not.
10. **§4.5 `config.c`** — still unsupported.
11. ~~**§3.2 circuit analysis**~~ — the VBAT divider is done and checked on two
    boards whose ratio is *not* 100K/10K, which was the condition this entry
    set. What remains is PINIO polarity and the current-meter scale, both
    smaller and neither blocking.
