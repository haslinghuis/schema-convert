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
| MCU pins read | 3090 | 3865 |
| nets checked against firmware | 1637 | 1920 |
| of those, agreeing | 1601 | 1878 |
| UART pins emitted | 299 | 576 |
| boards with any UART pin | 34 | 70 |
| boards at 100% agreement | 86 | 91 |
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
| §3.3 | Chip-select-only devices — half are really unrecognised net spellings; the rest need the peripheral end read, not wire tracing (§3.7) | 26 devices |
| §3.5 | Sheets that never name their MCU. Not broken: 100% agreement given `--target` | 34 |
| §3.4 | **AT32** peripheral tables are not harvested, so those boards cannot be converted at all | 17 |
| §1.13 | SPI buses still refused for a missing line — every one is an unbound net label, the §1.12/§1.13 class | 25 buses on 20 |
| §1.14 | A label naming the pin's own capability outranks the net behind it | 1 |
| §1.14 | The VBAT divider drawn as one horizontal and one vertical resistor is not read | 1+ |
| §3.8 | No golden board for a refused bus, and none at all for C5, N6 or AT32 | — |
| §3.7 | Wire tracing prototyped: settles local structure, does **not** yield a netlist on name-connected sheets | — |
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

### 1.9 Only two of a symbol's four edges were read — FIXED

A submitted F405 came back with 29 of its 50 pins. Symbol detection looked for
pin names sharing an *x* — the columns down the left and right of a box — and
nothing else. A four-sided QFP symbol also runs names across its top and bottom,
and those were invisible. **20 of the 125 corpus boards draw one**, and on the
largest 41 of its 82 pins are on horizontal edges; several were already filed
under §3.3 as "no SPI bus resolved", because the bus nets were on the two sides
nobody read.

An edge is the same search on a different coordinate, so the fix was to stop
assuming which. A row carries where it sits *along its own edge*; `ALONG` says
which axis that is; the gutter, the ownership test and the pairing all ask
before measuring.

Three things this turned up, each of which had been latent:

- **A pin name is never the continuation of another pin name.** That sheet sets
  its names 1.15pt apart horizontally — closer than the gap that rejoins one
  split name — so the assembler fused `PA4+PA5+…+PB2` into a single token and
  dropped nine pins. Supply names end a name too: `PB8`+`BOOT0` became
  `PB8BOOT0` and took the board's I2C SCL with it.
- **The offset must be swept per axis.** A label sits above the horizontal wire
  of a left-hand pin and beside the vertical wire of a top one - different
  distances between different anchors. One offset for both put a bottom edge two
  pins out, and it still read 100%, because the pins it landed on could do the
  job.
- **An axis has to earn its bindings.** Names and labels on horizontal edges are
  drawn rotated, and on one board that alignment cannot be confirmed at all —
  the best any offset reaches is 2 of its 3 checkable nets. A contradicted axis
  now keeps its rows, so those pins are still reported as unconnected, and loses
  its links.

Also: ST writes more than one dash qualifier (`PC13-TAMPER-RTC`,
`PC14-OSC32-IN`) where the pattern allowed one, and a trailing `_PIN` is
Betaflight's own define name copied onto the sheet — 91 nets across the corpus
end that way and 87 classify once it is stripped.

### 1.11 A pin name can be written last, and a net can be named after its part — FIXED

Two more submissions, two more spellings, and between them the clearest
statement of what this class of bug costs.

**The pin name at the end.** One H743's symbol names its left column pin-first
(`PC14/OSC32_IN`, as everything else does) and its right column the other way
round — `USART3_RX/PD9`, `SPI2_SCK/PB13`, `TIM3_CH3(M7)/PB0`, and
`TIM4_CH3(M11)PD14` with no separator at all. `PIN_RE` anchors at the start, so
half the package was invisible: 62 pins of ~97, with 20 net labels binding to
nothing. Matching a pin name at the *tail* as well takes it to 97 pins at 27/27
agreement with one label unbound. It also reads through `SPI1_MISO/IPA6`, which
is a typo for PA6.

Deliberately used only where a pin name is being *recognised*, never where
`PIN_RE` excludes a word from being a net label — a label that merely ends in
something pin-shaped is still a label.

**The net named after the part.** The same sheet names every device net after
the part on the other end of it, with a dot: `ICM42688.CS`, `AT7456.MOSI`,
`W25Q128.CS`, `BMP280.SCK`. The dot was not a separator and the part numbers
are not the family words the rules match on, so none of it classified. Both are
normalised now — the dot to an underscore, and a leading part number back to
the family the rules already know — and only where a separator follows, so a
bare `ICM42688` stays a part marking for `detect_parts` to read.

That settled something the tests had given up on. `MPU6000-CS` and
`ICM42688-CS` were asserted to classify as *nothing*, with a comment explaining
why: a part number is not a device index, and reading the digits would invent a
sixth or fourth IMU. Folding the part number to its family recognises the
device and yields no index at all — the right answer rather than the safe one.
The invariant those cases were protecting is now checked directly instead of
being sidestepped.

Over the corpus these two took total defines **4586 → 5109**, buses resolved
196 → 237, chip-select pins 160 → 182 and SPI instances 144 → 160, over 29
boards with none losing anything.

### 1.12 A pin name can carry ST's dual-pad suffix — FIXED

An H7 board came in "missing SPI 2 defines". It was missing one *row*: the
left edge had a two-pitch gap where a name should be, and the name in it was
`PC2_C/SPI2_MISO`.

`PC2_C` is ST's own spelling. On the H7 parts that carry an analog switch,
`PA0`/`PA1`/`PC2`/`PC3` each have a second pad, named with an underscore and a
`C`. `PIN_RE` allowed ST's *dash* qualifiers (`PC13-TAMPER-RTC`, §1.11) but not
this one, so the row was not a row, the label bound to nothing, and SPI2 came
out with SCK and SDO and no SDI — which is refused, because a partial bus does
not compile. One character of spelling cost the bus, the OSD on it, and the ADC
instance, since those same pads are ADC3's inputs.

Written out as `(?:_C)?` rather than allowing any underscore tail, because
`PIN_RE` also decides what is *not* a net label and a net called `PC13_LED`
must stay one.

Corpus: 21 boards, all H7 as expected. Unbound labels **179 → 140**, nets
checked 1920 → 1950 with agreement holding, defines 5109 → 5158. Ten boards
gained a complete SPI2; four more gained `ADC_INSTANCE`/`ADC3_DMA_OPT`, because
VBAT and current sit on those pads and ADC3 is the only ADC that reaches them.
Verified against a hand-written config for one of them: `SPI2` `PB13`/`PC2`/`PC3`
and the ADC pins match exactly.

### 1.13 A name can sit just outside its own column — FIXED

Same board class, different cause, found by asking why §1.12's sibling boards
still refused a bus. On one F4 the name `PA6/SPI1_MISO` is reported by
`pdftotext` at x0 169.1 while the rest of its column is at 167.6. The edge
cluster is 1pt wide, so it was not on the edge, and its pin did not exist:
SPI1 lost its SDI, the gyro lost its bus, and `GYRO_1_SPI_INSTANCE` went with
it. The only symptom was one net label that matched no row.

Widening the tolerance is not available — this corpus draws real, distinct
columns 3.5pt apart, and merging them would be far worse than the bug. What
separates the two cases is the *run*: an edge is names at a regular pitch, so a
gap of two pitches is a row that must exist and does not. Only such a gap is
filled, and only from a name less than one character's width off the edge, with
the character width taken from the names themselves rather than a constant.
A genuine neighbouring column has no hole to fall into.

Two boards, three pins, and with them two complete SPI1 buses, a restored
`GYRO_1_SPI_INSTANCE` and a UART pin; nothing else in the corpus moved.

Both are now golden fixtures — `h7-dual-pad` and `f4-column-drift` — because
the check that should have caught the second one already existed
(`instances_without_pins`) and passed on all five boards under test while six
instances dangled in the corpus. See §3.8.

### 1.14 A net label need not be in a column — FIXED, with one case still wrong

The first board to arrive with a **vendor-written config beside it**, which is
the only ground truth this project has had. 81 defines to compare against.

The generator agreed on 54 and missed 25. Almost all of the 25 came from one
cause: this sheet draws each net name against its own wire, so how far it sits
from the symbol is set by whatever components share that row. A row reads

```
3V3_MCU   R36   10k   GYRO_1_EXTI   D8   PD0
```

— a 10k pull-up, with the real net between the resistor and the pin. The next
row has a series resistor instead and its net sits where `3V3_MCU` is here. No
three labels line up, so the `len(col) >= 3` column test discarded every one of
them, and the *supply* feeding the pull-up — which is in a column, because every
pull-up on the sheet shares it — bound to the pin instead. `GYRO_1_EXTI` and
`MAX7456_SPI_CS` became `3V3_MCU`; `LED0`, `ADC_VBAT` and `MOTOR6` vanished into
"unconnected pins", which reads like a board that does not use those pins.

Two changes, both bounded:

- **Labels outside any column are collected**, but only between the symbol and
  the strongest column — which is what establishes where this sheet's labels
  live — and only if they match `NET_VOCAB`. That filter is what keeps out ball
  coordinates (`D8`, `H8`, `F3`), designators (`R36`, `C55`) and values (`10k`,
  `100R`, `1%`); it is the same one the neighbouring-column pass already trusts.
- **One row carries one net.** Where two labels are level with a pin, the nearer
  wins, because the other is on the far side of a component. Distance is rounded
  to the point and alignment breaks the tie: below a point this is extraction
  noise, and comparing raw floats let a stray `N` take a row from the `I2C1_SCL`
  beside it by one thousandth of a point.

Corpus: **+57 defines**, 63 gained against 6 lost, warnings down 6. Four boards
gained a complete SPI bus, four the ADC3 current sense. Four of the six losses
are corrections — before this, three boards emitted a pin under two different
roles (`FLASH_CS_PIN` *and* `SPI3_SDO_PIN` on one pin; `GYRO_1_CS_PIN` *and*
`ADC_VBAT_PIN` on another), which cannot be built. That count is now zero.

On the board itself, exact agreement with the vendor went **54 → 66 of 81**.

**The case still wrong.** One sheet prints a pin's own ADC channel (`ADC1_8`)
between the symbol and the wire, nearer than the net; `RSSI` is behind it and
now loses the row. Ranking by alignment first fixes that board — the annotation
is 2.5pt off the row and `RSSI` is 0.3pt off it — but costs three F7 boards a
whole SPI3 bus, 16 defines against 6. So distance-first stays, and one board
loses `ADC_RSSI_PIN` and `ADC1_DMA_OPT`. The real discriminator is that a label
naming the pin's own capability is not a net name at all, which is a different
fix from ranking.

### 1.15 Two more spellings, from the same board

`SDCARD_SPI_CS` and `CLKIN`, both read correctly and both classified as nothing.
The first wanted the `_SPI` infix that the `FLASH`, `OSD` and `BARO` patterns
already allow; the second is a bare `CLKIN` where the pattern demanded a
`GYRO`/`IMU` prefix. The MCU's own clock input is not a rival for that spelling —
it arrives on `OSC_IN`, which the symbol names as a system pin and which never
reaches classification.

§1.8–§1.11 again, and worth noting how it was found: not by reading code, but by
having a config to compare against.

#### What the vendor config settled, and what it did not

Two differences turned out to be neither side's defect, and both took tracing to
firmware rather than reasoning about it:

- **`ADC_INSTANCE ADC3`**, which the vendor omits. Firmware's `adcTagMap` has
  `PC2`/`PC3` as `ADC_DEVICES_3` on H743, and `ADC_INSTANCE` defaults to `ADC1`,
  so the vendor's config looks broken. It is not: `adc_stm32h7xx.c` falls back to
  any *activated* device that can reach the pin, and their `ADC3_DMA_OPT` is what
  activates it. Both work; ours reaches ADC3 directly instead of via the fallback.
- **`ADC3_DMA_OPT` 9 against their 10.** On a DMAMUX part the option is an index
  into one shared channel table, so any free channel is as good as another.

Still genuinely missing, and derivable in principle: `DEFAULT_VOLTAGE_METER_SCALE`.
The divider *is* on the sheet — `R33 150k` to `BAT`, `R34 10k` to `GND`, giving
exactly the vendor's 160 — but it is drawn with the top resistor horizontal and
the bottom one vertical, where §3.2's reader looks for two resistors on one
vertical leg either side of the node. A layout it does not know, not a bug.

#### The pattern behind §1.8 through §1.11

Four entries now, and they are all the same defect: **a rule that describes one
spelling of a thing, written from the boards that happened to be on hand.**
`TX4` but not `UART4_TX`. `SPI3_SCK` but not `SCK3` or `SPI_SCK2` or `SPI1CLK`.
`GYRO1-CS` but not `GYRO_1_CS`. Pin names at the head but not the tail. `I2C`
but not `IIC`. A separator that is a dash or an underscore but never a dot.

None of it was findable by reading the code, and each one was silent: the nets
went out as "no config.h role" while every diagnostic the tool prints stayed
clean. What finds them is counting what came out the far end unclassified,
across every board available — which is a five-minute check and should be run
whenever a new submission arrives, before anything else is investigated.

### 1.10 The row pitch was measured across both columns — FIXED

Pitch is the smallest gap between adjacent names, and it was taken over *all*
rows. The two columns of a symbol are interleaved along the same axis: a left
row and the right row opposite it sit a fraction apart while the real step is to
the next pair. One board's columns are each a clean 7.2pt apart and their union
alternates 0.72 and 6.48, so its pitch came out **ten times too small**.

It survived for as long as it did because nothing had ever shifted the minimum.
Recognising two more pin names did, and the board went from 100% agreement to
binding nothing at all — every tolerance downstream is scaled by pitch.

Two lessons worth more than the fix. **A constant tuned against a broken value
is broken too**: the merge test's adjacency reach of six pitches was harmless
while the pitch was under-measured and far too generous once it was right, and
correcting one without the other ballooned a symbol to 198 rows. And **a number
that has never moved is not the same as a number that is right** — this one had
been wrong on that board since the day it was written.

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

### 2.3 Still unproven after 168 schematics — and SDIO is now DONE

`RX_PPM_PIN`, `ESCSERIAL_PIN` and burst DShot are implemented and unit-tested,
and **not one board in the corpus emits any of them**. That is a stronger
statement than "no board exercises them" and it retires them as priorities:
both are legacy, and the corpus says they are gone.

**`USE_SDCARD` was the wrong shape, and is now fixed.** 22 boards mention a
card and the ones that wire it use the MCU's **SDMMC controller**, not a SPI
bus — so the SPI-only implementation gave none of them a card and the nets came
out as "no config.h role". Six spellings occur and none was matched:
`SDMMC1-CK`, `SDMMC2_D3`, `SDIO_CMD`, `SDIO-D2`, `SD_SDIO_CK`, `SD_CLK`. The
digit is the *controller* (`SDMMC2` → `SDIO_DEVICE SDIODEV_2`), not a line
number.

The interesting part was deciding where it may be emitted at all. `pg/sdio.c`
registers the pin config behind `#if ENABLE_SDIO_PIN_CONFIG`, which
`common_post.h` defaults to **0** — and only a target's own `target.h` turns it
on, so it is per *target*, not per family: H743 sets it, H750 does not, and both
are STM32H7. On a target that leaves it off, `SDIO_CK_PIN` and the rest compile
fine and are never read. That is the §1 case exactly, so the seeder now records
`pin_config` and whether the family has an sdio driver, and the generator
declines with the reason instead of emitting inert defines.

`USE_SDCARD_SDIO` is deliberately *not* emitted — every `target.h` with an SDMMC
controller defines it inside `#ifdef USE_SDCARD`, and the hand-written SDIO
configs set neither. `SDIO_DEVICE` and `SDIO_USE_4BIT` are, because `pg/sdio.c`
defaults them to `SDIOINVALID` and `false`: omitting them selects no controller
and runs the card one-bit.

Verified against a second board with a hand-written config, and every SDIO
define matches it exactly — CK, CMD, D0–D3, DEVICE, 4BIT, the detect pin and its
inversion. That board now agrees on **73 of 93** defines overall.

Seven boards emit a complete SDIO block where none did; three more are refused
because their CMD or D0 net is not drawn, and say so.

### 2.4 Second I2C bus — DONE

`genconfig` kept a single `i2c` dict of `{scl, sda}`, so a board with I2C1 *and*
I2C2 lost whichever was read first and the survivor wore the other's name. It
was not rare, it was structurally impossible: **no board in the corpus emitted
more than one I2C bus.** The visible symptom was the note *"net names say I2C2
but the pins are I2C1"*, on 29 boards.

Fixed by keying the collected nets on the index in their name, so each bus is
its own group — the same shape as `spi_named`. The *instance* still comes from
the pins through the firmware map; the index only says which nets belong
together, and `infer_i2c_bus` still reports it when the two disagree.

That left the second half: **which bus each device is on**. An I2C part has no
chip select, so the trick that settles SPI (§3.3) does not apply — there is no
per-device net to follow. The part itself is the anchor instead: a baro marked
`DPS310` is drawn with its SCL and SDA beside it. `i2c_bus_for()` reads those,
with the same discipline as the SPI tracer — nearest bus must be 3× nearer than
the next, MCU-side labels excluded, and it declines rather than guesses.

| | before | after |
|---|---|---|
| I2C SCL/SDA pins emitted | 138 | **202** |
| boards emitting two I2C buses | 0 | **27** |
| boards showing the name/pin collision | 29 | **0** |

17 baro/mag parts are traced to a bus by their own nets and 3 refused as
ambiguous. 27 boards gain defines, none lose any, and all four golden fixtures
are byte-identical.

On the one board with a hand-written config to check against, the whole I2C
block now matches it exactly — including `BARO_I2C_INSTANCE I2CDEV_2`, which
was `_1` before, off a `DPS310` traced 23pt from I2C2's nets against 423pt to
the next. That board is now down to **a single difference** across 62 agreeing
defines: `ADC1_DMA_OPT` 8 against 10, two equally free DMAMUX channels.

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

So: a corroborating signal for *local* structure, not a replacement for the
label pipeline, and not a route to a netlist on sheets drawn this way. The
bounded use worth landing is deciding which of two labels level with a pin is
on its net. Two prototype details to carry over: the text-to-net probe radius
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
